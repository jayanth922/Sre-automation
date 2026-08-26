#!/usr/bin/env python3
"""Scenario-owned recovery evidence for the live SRE benchmark.

This module is deliberately outside the agent runtime. It reads raw Prometheus
signals selected by the benchmark scenario, not incident status, graph output,
or an agent-authored verification summary.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

OracleOperator = Literal["lt", "lte", "gt", "gte", "eq"]
ObservationState = Literal["passing", "failing", "unknown"]
ObservationPhase = Literal["baseline", "recovery"]
OracleStatus = Literal["VERIFIED_RECOVERED", "UNRESOLVED", "INVALID_SCENARIO"]

_OPERATORS = {
    "lt": lambda value, threshold: value < threshold,
    "lte": lambda value, threshold: value <= threshold,
    "gt": lambda value, threshold: value > threshold,
    "gte": lambda value, threshold: value >= threshold,
    "eq": lambda value, threshold: value == threshold,
}


class OracleQueryError(RuntimeError):
    """The independent signal could not produce one trustworthy scalar."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


@dataclass(frozen=True)
class RecoveryProbe:
    """A benchmark-owned, deterministic definition of healthy recovery."""

    name: str
    query: str
    operator: OracleOperator
    threshold: float
    unit: str
    required_consecutive_passes: int = 2
    require_failure_observation: bool = True

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("probe name must not be empty")
        if not self.query.strip():
            raise ValueError("probe query must not be empty")
        if self.operator not in _OPERATORS:
            raise ValueError(f"unsupported oracle operator: {self.operator}")
        if not math.isfinite(self.threshold):
            raise ValueError("probe threshold must be finite")
        if self.required_consecutive_passes < 1:
            raise ValueError("required_consecutive_passes must be at least 1")

    def passes(self, value: float) -> bool:
        if not math.isfinite(value):
            return False
        return bool(_OPERATORS[self.operator](value, self.threshold))

    @property
    def definition_sha256(self) -> str:
        payload = {
            "name": self.name,
            "query": self.query,
            "operator": self.operator,
            "threshold": self.threshold,
            "unit": self.unit,
            "required_consecutive_passes": self.required_consecutive_passes,
            "require_failure_observation": self.require_failure_observation,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "definition_sha256": self.definition_sha256}


@dataclass(frozen=True)
class OracleObservation:
    observed_at: datetime
    value: Optional[float]
    state: ObservationState
    phase: ObservationPhase
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_at": _iso(self.observed_at),
            "value": self.value,
            "state": self.state,
            "phase": self.phase,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class OracleRunResult:
    scenario: str
    incident_id: Optional[str]
    application_status: str
    status: OracleStatus
    started_at: datetime
    recovered_at: Optional[datetime]
    mttr_seconds: Optional[float]
    baseline_healthy: Optional[bool]
    failure_observed: bool
    false_resolved: bool
    probe: RecoveryProbe
    observations: tuple[OracleObservation, ...]
    dataset_version: str = "legacy"
    scenario_version: str = "unversioned"
    dataset_split: str = "unspecified"
    dataset_sha256: str = ""
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scenario": self.scenario,
            "dataset_version": self.dataset_version,
            "scenario_version": self.scenario_version,
            "dataset_split": self.dataset_split,
            "dataset_sha256": self.dataset_sha256,
            "incident_id": self.incident_id,
            "application_status": self.application_status,
            "status": self.status,
            "started_at": _iso(self.started_at),
            "recovered_at": _iso(self.recovered_at),
            "mttr_seconds": self.mttr_seconds,
            "baseline_healthy": self.baseline_healthy,
            "failure_observed": self.failure_observed,
            "false_resolved": self.false_resolved,
            "probe": self.probe.to_dict(),
            "observations": [item.to_dict() for item in self.observations],
        }


@dataclass
class RecoveryOracleTracker:
    """Accumulates raw observations and decides when recovery is verified."""

    probe: RecoveryProbe
    started_at: datetime
    observations: list[OracleObservation] = field(default_factory=list)
    baseline_healthy: Optional[bool] = None
    failure_observed: bool = False
    consecutive_passes: int = 0
    recovered_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.started_at.tzinfo is None:
            raise ValueError("started_at must be timezone-aware")

    def begin(self, started_at: datetime) -> None:
        """Set the exact scenario-stimulus time after the baseline query."""
        if started_at.tzinfo is None:
            raise ValueError("started_at must be timezone-aware")
        if any(item.phase == "recovery" for item in self.observations):
            raise RuntimeError("cannot move started_at after recovery polling began")
        self.started_at = started_at

    def establish_baseline(
        self,
        value: Optional[float],
        *,
        observed_at: Optional[datetime] = None,
        error: Optional[str] = None,
    ) -> OracleObservation:
        """Require a healthy pre-stimulus signal so ambient faults cannot score."""
        if self.baseline_healthy is not None or any(
            item.phase == "baseline" for item in self.observations
        ):
            raise RuntimeError("oracle baseline was already established")
        timestamp = observed_at or _utc_now()
        if timestamp.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")

        if error is not None or value is None or not math.isfinite(value):
            state: ObservationState = "unknown"
            self.baseline_healthy = None
            detail = error or "baseline returned no finite scalar"
        elif self.probe.passes(value):
            state = "passing"
            self.baseline_healthy = True
            detail = "healthy baseline established"
        else:
            state = "failing"
            self.baseline_healthy = False
            detail = "baseline was already unhealthy before the scenario stimulus"

        observation = OracleObservation(timestamp, value, state, "baseline", detail)
        self.observations.append(observation)
        return observation

    def observe(
        self,
        value: Optional[float],
        *,
        observed_at: Optional[datetime] = None,
        error: Optional[str] = None,
    ) -> OracleObservation:
        timestamp = observed_at or _utc_now()
        if timestamp.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")

        if self.baseline_healthy is not True:
            if error is not None or value is None or not math.isfinite(value):
                state = "unknown"
            else:
                state = "passing" if self.probe.passes(value) else "failing"
            detail = "recovery observation ignored because baseline was not healthy"
            self.consecutive_passes = 0
        elif error is not None or value is None or not math.isfinite(value):
            state: ObservationState = "unknown"
            detail = error or "oracle returned no finite scalar"
            self.consecutive_passes = 0
        elif self.probe.passes(value):
            state = "passing"
            if self.probe.require_failure_observation and not self.failure_observed:
                detail = (
                    "healthy signal observed before the oracle was armed by a fault"
                )
                self.consecutive_passes = 0
            else:
                self.consecutive_passes += 1
                detail = (
                    f"pass {self.consecutive_passes}/"
                    f"{self.probe.required_consecutive_passes}"
                )
                if (
                    self.recovered_at is None
                    and self.consecutive_passes
                    >= self.probe.required_consecutive_passes
                ):
                    self.recovered_at = timestamp
        else:
            state = "failing"
            detail = "signal remains outside the recovery boundary"
            self.failure_observed = True
            self.consecutive_passes = 0

        observation = OracleObservation(timestamp, value, state, "recovery", detail)
        self.observations.append(observation)
        return observation

    def result(
        self,
        *,
        scenario: str,
        incident_id: Optional[str],
        application_status: str,
        dataset_version: str = "legacy",
        scenario_version: str = "unversioned",
        dataset_split: str = "unspecified",
        dataset_sha256: str = "",
    ) -> OracleRunResult:
        if self.baseline_healthy is not True:
            status: OracleStatus = "INVALID_SCENARIO"
            mttr = None
        elif self.recovered_at is not None:
            status: OracleStatus = "VERIFIED_RECOVERED"
            mttr = (self.recovered_at - self.started_at).total_seconds()
        elif self.probe.require_failure_observation and not self.failure_observed:
            status = "INVALID_SCENARIO"
            mttr = None
        else:
            status = "UNRESOLVED"
            mttr = None

        return OracleRunResult(
            scenario=scenario,
            incident_id=incident_id,
            application_status=application_status,
            status=status,
            started_at=self.started_at,
            recovered_at=self.recovered_at,
            mttr_seconds=mttr,
            baseline_healthy=self.baseline_healthy,
            failure_observed=self.failure_observed,
            false_resolved=(
                application_status.lower() == "resolved"
                and status != "VERIFIED_RECOVERED"
            ),
            probe=self.probe,
            observations=tuple(self.observations),
            dataset_version=dataset_version,
            scenario_version=scenario_version,
            dataset_split=dataset_split,
            dataset_sha256=dataset_sha256,
        )


def parse_prometheus_value(payload: Any) -> Optional[float]:
    """Parse one scalar from a Prometheus HTTP API instant-query response."""
    if not isinstance(payload, dict):
        raise OracleQueryError("Prometheus response is not an object")
    if payload.get("status") != "success":
        raise OracleQueryError(str(payload.get("error") or "Prometheus query failed"))

    data = payload.get("data")
    if not isinstance(data, dict):
        raise OracleQueryError("Prometheus response has no data object")
    result_type = data.get("resultType")
    result = data.get("result")

    raw_value: Any
    if result_type == "scalar":
        if not isinstance(result, list) or len(result) < 2:
            raise OracleQueryError("Prometheus scalar response is malformed")
        raw_value = result[1]
    elif result_type == "vector":
        if not isinstance(result, list):
            raise OracleQueryError("Prometheus vector response is malformed")
        if not result:
            return None
        if len(result) != 1:
            raise OracleQueryError(
                "oracle query must return exactly one series; aggregate the probe"
            )
        sample = result[0]
        if not isinstance(sample, dict):
            raise OracleQueryError("Prometheus vector sample is malformed")
        value = sample.get("value")
        if not isinstance(value, list) or len(value) < 2:
            raise OracleQueryError("Prometheus vector value is malformed")
        raw_value = value[1]
    else:
        raise OracleQueryError(f"unsupported Prometheus result type: {result_type!r}")

    try:
        parsed = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise OracleQueryError("Prometheus scalar is not numeric") from exc
    if not math.isfinite(parsed):
        raise OracleQueryError("Prometheus scalar is not finite")
    return parsed


class PrometheusOracleClient:
    """Minimal direct Prometheus client, independent of Sentinel and MCP."""

    def __init__(
        self,
        base_url: str,
        *,
        bearer_token: Optional[str] = None,
        timeout_seconds: float = 6.0,
    ) -> None:
        if not base_url.strip():
            raise ValueError("Prometheus base URL must not be empty")
        self._base_url = base_url.rstrip("/")
        self._headers = (
            {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}
        )
        self._timeout_seconds = timeout_seconds

    async def query(self, client: Any, probe: RecoveryProbe) -> Optional[float]:
        response = await client.get(
            f"{self._base_url}/api/v1/query",
            params={"query": probe.query},
            headers=self._headers,
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        return parse_prometheus_value(response.json())


def append_oracle_result(path: Path, result: OracleRunResult) -> None:
    """Append immutable evaluator evidence outside application-owned records."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(result.to_dict(), sort_keys=True, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{line}\n")
