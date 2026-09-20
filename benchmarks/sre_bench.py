#!/usr/bin/env python3
"""
SRE-Agent Benchmark — independently verified recovery and quality.

Extends the MTTR benchmark into a full domain benchmark for the multi-agent SRE
system. For each fault scenario (with known ground truth) it fires a synthetic
alert, observes a scenario-owned Prometheus recovery probe outside Sentinel,
fetches the incident transcript, and scores:

    verified recovery · MTTR · root-cause · remediation · severity · safety

across `RUNS_PER_SCENARIO` repeats (pass^k consistency), then prints a
leaderboard-style report.

Prerequisites (runs against the LIVE platform, not in CI):
- Platform + edge up (`./main_start.sh`) and a client environment connected
  (e.g. the reference client `../meridian-shop`).
- For the remediation/severity/safety columns, enable the ACT phase:
  `ACT_PHASE_ENABLED=true` (and optionally `EXECUTOR_LIVE=true`).

Run:
    uv run python benchmarks/sre_bench.py

Config via env (falls back to the bench_mttr defaults):
    BENCH_BASE_URL, BENCH_creds.admin_email, BENCH_creds.admin_password,
    BENCH_CLUSTER_ID, BENCH_CLUSTER_TOKEN, BENCH_RUNS_PER_SCENARIO,
    BENCH_PROMETHEUS_URL, BENCH_ORACLE_RESULTS_PATH
"""

import asyncio
import base64
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parents[1]))
from fault_adapter import MeridianAdminConfigAdapter  # noqa: E402
from fixtures import resolve_credentials  # noqa: E402
from recovery_oracle import (  # noqa: E402
    PrometheusOracleClient,
    RecoveryOracleTracker,
    append_oracle_result,
)
from scenario_dataset import load_dataset  # noqa: E402
from scoring import ScenarioSpec, aggregate, score_run  # noqa: E402
from statistical_eval import (  # noqa: E402
    append_trial,
    build_trial_record,
    build_trial_schedule,
    make_pair_id,
)
from structured_grading import append_grader_record  # noqa: E402

from sre_agent.confidence_calibration import (  # noqa: E402
    append_confidence_record,
    build_confidence_record,
)

# ── Config ──────────────────────────────────────────────────────────────────
RUNS_PER_SCENARIO = int(os.getenv("BENCH_RUNS_PER_SCENARIO", "3"))
PROMETHEUS_URL = os.getenv("BENCH_PROMETHEUS_URL", "http://localhost:9090")
PROMETHEUS_TOKEN = os.getenv("BENCH_PROMETHEUS_BEARER_TOKEN")
ORACLE_RESULTS_PATH = Path(
    os.getenv("BENCH_ORACLE_RESULTS_PATH", "reports/sre-bench-oracle.jsonl")
)
GRADER_RESULTS_PATH = Path(
    os.getenv("BENCH_GRADER_RESULTS_PATH", "reports/sre-bench-grades.jsonl")
)
TRIAL_RESULTS_PATH = Path(
    os.getenv("BENCH_TRIAL_RESULTS_PATH", "reports/sre-bench-trials.jsonl")
)
CONFIDENCE_RESULTS_PATH = Path(
    os.getenv(
        "BENCH_CONFIDENCE_RESULTS_PATH",
        "reports/sre-bench-confidence.jsonl",
    )
)
EXPERIMENT_ID = os.getenv("BENCH_EXPERIMENT_ID", "").strip()
CANDIDATE_ID = os.getenv("BENCH_CANDIDATE_ID", "").strip()
CONFIG_FINGERPRINT = os.getenv("BENCH_CONFIG_FINGERPRINT", "").strip()
PAIR_SEED = os.getenv("BENCH_PAIR_SEED", "").strip()
STATISTICAL_CONFIG = {
    "experiment_id": EXPERIMENT_ID,
    "candidate_id": CANDIDATE_ID,
    "config_fingerprint": CONFIG_FINGERPRINT,
    "pair_seed": PAIR_SEED,
}
STATISTICAL_RECORDING = any(STATISTICAL_CONFIG.values())
if STATISTICAL_RECORDING and not all(STATISTICAL_CONFIG.values()):
    missing = sorted(key for key, value in STATISTICAL_CONFIG.items() if not value)
    raise RuntimeError(
        f"statistical recording requires all BENCH experiment fields; missing={missing}"
    )
# Both record writers parse this as a lowercase SHA-256, and the confidence
# corpus is grouped by it. Without this check a typo is still fatal -- just
# fatal at the first write, which happens after an incident has been
# provisioned, investigated and paid for, and it aborts the run there. The
# shape is knowable before any of that.
if STATISTICAL_RECORDING and (
    len(CONFIG_FINGERPRINT) != 64
    or any(character not in "0123456789abcdef" for character in CONFIG_FINGERPRINT)
):
    raise RuntimeError(
        "BENCH_CONFIG_FINGERPRINT must be a lowercase 64-character SHA-256 digest; "
        f"got {len(CONFIG_FINGERPRINT)} characters. Every trial row and every "
        "confidence observation is keyed on it."
    )
DATASET_ROOT = Path(
    os.getenv(
        "BENCH_DATASET_ROOT",
        str(Path(__file__).resolve().parent / "datasets"),
    )
)
DATASET_VERSION = os.getenv("BENCH_DATASET_VERSION", "v2")
DATASET_SPLIT = os.getenv("BENCH_DATASET_SPLIT", "dev")
ALLOW_HOLDOUT = os.getenv("BENCH_ALLOW_HOLDOUT", "").lower() in {
    "1",
    "true",
    "yes",
}
FAULT_MODE = os.getenv("BENCH_FAULT_MODE", "none").strip().lower()
if FAULT_MODE not in {"none", "manual", "automatic"}:
    raise RuntimeError("BENCH_FAULT_MODE must be none, manual, or automatic")
FAULT_SERVICE_URLS = {
    "checkout-service": os.getenv("BENCH_CHECKOUT_URL", "http://localhost:8001"),
    "inventory-service": os.getenv("BENCH_INVENTORY_URL", "http://localhost:8002"),
    "payment-service": os.getenv("BENCH_PAYMENT_URL", "http://localhost:8004"),
    # Not scraped by Prometheus; it is a fault target only, driving the
    # services that are scraped.
    "load-generator": os.getenv("BENCH_LOADGEN_URL", "http://localhost:8003"),
}
POLL_INTERVAL_SEC = 5
# How long one trial may wait for the oracle to see recovery. 300s was chosen
# when a run was one investigation pass; it is below the floor of what this
# system actually takes. Measured on the live cluster, a single incident spends
# 10-30 minutes in the graph: each specialist may burn its own 120s ceiling,
# and the reflector's unknowns can send the whole set round again. A ceiling
# under the loop's own runtime does not measure a slow agent, it records every
# trial as a non-recovery — so the ceiling is operator-set, and the default
# stays 300 only so existing invocations keep their meaning.
TIMEOUT_SEC = int(os.getenv("BENCH_INCIDENT_TIMEOUT_SEC", "300"))
# The fastest complete investigation ever measured on the live cluster: Phase 0
# on 2026-09-19, 110 LLM calls in 21.1 minutes. A ceiling below even the
# quickest observed run cannot be waiting for a real outcome, so the header
# says so out loud. Deliberately the measured *floor* and not the 49-minute
# ceiling — this flags invocations that cannot work, not ones that might be
# tight. Three pilots on 2026-09-19 were silently voided by this before anyone
# noticed the default was still 300.
MEASURED_INCIDENT_FLOOR_SEC = 1260


def timeout_warning(fault_mode: str, timeout_sec: int) -> str | None:
    """The header's warning when the ceiling makes recovery unmeasurable.

    Not an error, and deliberately not fatal: the ceiling is operator-set on
    purpose, and a short one is legitimate when smoke-testing the harness
    itself or running with no fault at all. It is fatal to a *measurement*,
    though, and silently so — the trial records a non-recovery whatever the
    agent did, and cleanup pulls the fault while the agent is still looking
    at it, so the tail of the investigation is spent on a fault that is no
    longer there and any plan produced after that point is evidence about
    nothing.
    """
    if fault_mode == "none" or timeout_sec >= MEASURED_INCIDENT_FLOOR_SEC:
        return None
    return (
        f"  ⚠ WARNING: {timeout_sec}s is below the {MEASURED_INCIDENT_FLOOR_SEC}s "
        f"({MEASURED_INCIDENT_FLOOR_SEC // 60} min) floor measured on this cluster.\n"
        f"    Every trial will record a non-recovery regardless of agent behaviour,\n"
        f"    and fault cleanup will fire mid-investigation. This run cannot\n"
        f"    measure recovery. Set BENCH_INCIDENT_TIMEOUT_SEC=2700 for a real one."
    )
COOLDOWN_SEC = 30
ORACLE_COMPLETION_GRACE_SEC = int(os.getenv("BENCH_ORACLE_COMPLETION_GRACE_SEC", "30"))
ACCOUNTING_WAIT_SEC = int(os.getenv("BENCH_ACCOUNTING_WAIT_SECONDS", "30"))

TERMINAL_APPLICATION_STATUSES = {
    "investigated",
    "awaiting_approval",
    "remediation_failed",
    "verification_unknown",
    "resolved",
}

DATASET = load_dataset(
    DATASET_ROOT,
    DATASET_VERSION,
    DATASET_SPLIT,
    allow_holdout=ALLOW_HOLDOUT,
)
SCENARIOS = DATASET.scenarios

# A smoke knob, not a measurement one. One incident on this cluster costs real
# money and 10-30 minutes, so "does the ACT path work at all" should not have
# to buy a whole split to find out.
#
# It is refused during a recorded experiment on purpose. `build_trial_schedule`
# derives run order from the *list of names it is handed*, so a filtered arm
# and an unfiltered one would be ordered differently under the same
# `BENCH_PAIR_SEED` — the seed exists to hold run order constant across arms,
# and a subset silently removes that guarantee while still stamping every trial
# with the full split's `dataset_sha256`. The resulting artifact would claim a
# comparability it does not have.
SCENARIO_FILTER = tuple(
    name.strip() for name in os.getenv("BENCH_SCENARIOS", "").split(",") if name.strip()
)
if SCENARIO_FILTER:
    if STATISTICAL_RECORDING:
        raise RuntimeError(
            "BENCH_SCENARIOS is a smoke-run filter and cannot be combined with "
            "statistical recording: a subset breaks the run-order guarantee "
            "BENCH_PAIR_SEED provides across arms, while the trials would still "
            "carry the full split's dataset_sha256. Run the whole split, or drop "
            "the statistical env (BENCH_EXPERIMENT_ID/BENCH_CANDIDATE_ID/…)."
        )
    _known = {spec.name for spec in SCENARIOS}
    _unknown = sorted(set(SCENARIO_FILTER) - _known)
    if _unknown:
        # Degrading to the full split would turn a typo into a bill.
        raise RuntimeError(
            f"BENCH_SCENARIOS names {_unknown} which are not in "
            f"{DATASET.dataset_version}/{DATASET.split}. Available: "
            f"{sorted(_known)}"
        )
    SCENARIOS = [spec for spec in SCENARIOS if spec.name in SCENARIO_FILTER]


async def _login(client: httpx.AsyncClient, creds) -> str:
    r = await client.post(
        f"{creds.base_url}/auth/token",
        data={"username": creds.admin_email, "password": creds.admin_password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    r.raise_for_status()
    return r.json()["access_token"]


# Renew once this much of the token's life is gone. Well clear of the expiry
# even if a poll is slow, without re-logging in on every request.
_TOKEN_RENEW_FRACTION = 0.6
# Used only when the token carries no readable `exp`. Deliberately short: the
# cost of renewing too often is one cheap request, the cost of renewing too
# late is a dead campaign.
_TOKEN_FALLBACK_LIFETIME = timedelta(minutes=5)


def _token_lifetime(token: str, issued_at: datetime) -> Optional[timedelta]:
    """How long this JWT has left, read from its own unverified `exp` claim.

    Reading a token without verifying it is normally a mistake. Here it is
    scheduling, not authentication: the server remains the only thing that
    decides whether a token is accepted, and the worst a forged `exp` could do
    is make this process renew at the wrong moment. Scheduling from the claim
    rather than a constant means a deployment that changes
    `ACCESS_TOKEN_EXPIRE_MINUTES` does not silently reintroduce the bug below.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload))["exp"]
        return datetime.fromtimestamp(exp, tz=timezone.utc) - issued_at
    except Exception:
        return None


class _Token:
    """An access token that renews itself before the server stops taking it.

    The runner used to log in once and reuse that string for the whole
    campaign. Access tokens live 15 minutes (`ACCESS_TOKEN_EXPIRE_MINUTES`)
    while a single incident is allowed 45 (`BENCH_INCIDENT_TIMEOUT_SEC`), so
    any incident that ran longer than its token died mid-poll on a 401 —
    after the fault was injected and the agent had already spent the money.
    Nothing was scored and the fault was left in the cluster. Long runs are
    the only runs this benchmark exists to do, so the single login was a bug
    that grew with the value of the run.
    """

    def __init__(self, client: httpx.AsyncClient, creds) -> None:
        self._client = client
        self._creds = creds
        self._value: Optional[str] = None
        self._renew_at = datetime.min.replace(tzinfo=timezone.utc)

    async def value(self) -> str:
        if self._value is None or datetime.now(timezone.utc) >= self._renew_at:
            await self._renew()
        assert self._value is not None
        return self._value

    async def headers(self) -> dict:
        return {"Authorization": f"Bearer {await self.value()}"}

    async def _renew(self) -> None:
        issued_at = datetime.now(timezone.utc)
        value = await _login(self._client, self._creds)
        lifetime = _token_lifetime(value, issued_at) or _TOKEN_FALLBACK_LIFETIME
        if lifetime <= timedelta(0):
            # Renewing on a schedule already in the past would turn every
            # request into a login. Stop instead of hammering /auth/token.
            raise RuntimeError(
                "the platform issued an already-expired access token "
                f"(exp is {-lifetime} in the past); check clock skew between "
                "this host and the API before running a campaign"
            )
        self._value = value
        self._renew_at = issued_at + lifetime * _TOKEN_RENEW_FRACTION


async def _incident_ids(client: httpx.AsyncClient, jwt: _Token, creds) -> set[str]:
    r = await client.get(
        f"{creds.base_url}/api/v1/clusters/{creds.cluster_id}/incidents",
        headers=await jwt.headers(),
    )
    r.raise_for_status()
    return {inc["id"] for inc in r.json()}


async def _fire_alert(
    client: httpx.AsyncClient, spec: ScenarioSpec, started_at: datetime, creds
) -> None:
    payload = {
        "version": "4",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": spec.alert["alertname"],
                    "severity": spec.alert["severity"],
                    "service": spec.alert["service"],
                },
                "annotations": {
                    "summary": spec.alert["summary"],
                    "description": spec.alert["description"],
                },
                "startsAt": started_at.isoformat(),
            }
        ],
    }
    r = await client.post(
        f"{creds.base_url}/api/v1/alerts/webhook",
        json=payload,
        headers={"Authorization": f"Bearer {creds.cluster_token}"},
    )
    r.raise_for_status()


async def _wait_new_incident(client, jwt, known, creds) -> Optional[dict]:
    for _ in range(10):
        await asyncio.sleep(2)
        r = await client.get(
            f"{creds.base_url}/api/v1/clusters/{creds.cluster_id}/incidents",
            headers=await jwt.headers(),
        )
        r.raise_for_status()
        for inc in r.json():
            if inc["id"] not in known:
                return inc
    return None


async def _fetch_incident(client, jwt, incident_id, creds) -> Optional[dict]:
    response = await client.get(
        f"{creds.base_url}/api/v1/clusters/{creds.cluster_id}/incidents",
        headers=await jwt.headers(),
    )
    response.raise_for_status()
    return next(
        (incident for incident in response.json() if incident["id"] == incident_id),
        None,
    )


async def _observe_oracle(
    client: httpx.AsyncClient,
    oracle_client: PrometheusOracleClient,
    tracker: RecoveryOracleTracker,
    *,
    baseline: bool = False,
) -> None:
    try:
        value = await oracle_client.query(client, tracker.probe)
    except Exception as exc:
        if baseline:
            tracker.establish_baseline(None, error=f"{type(exc).__name__}: {exc}")
        else:
            tracker.observe(None, error=f"{type(exc).__name__}: {exc}")
    else:
        if baseline:
            tracker.establish_baseline(value)
        else:
            tracker.observe(value)


async def _await_manual_fault(
    spec: ScenarioSpec, tracker: RecoveryOracleTracker
) -> None:
    if FAULT_MODE != "manual" or tracker.baseline_healthy is not True:
        return
    steps = "; ".join(
        f"apply {contract['inject']} to {contract['target']}"
        for contract in spec.fault["contracts"]
    )
    restore = _manual_cleanup_steps(spec)
    prompt = (
        f"\nBaseline is healthy for {spec.name}. In order: {steps}. "
        f"Then press Enter. Required cleanup: {restore}. "
    )
    await asyncio.to_thread(input, prompt)


def _manual_cleanup_steps(spec: ScenarioSpec) -> str:
    """Restoration steps in reverse application order, as the adapter unwinds."""
    return "; ".join(
        f"restore {contract['target']} to {contract['cleanup']}"
        for contract in reversed(spec.fault["contracts"])
    )


async def _await_manual_cleanup(spec: ScenarioSpec) -> None:
    if FAULT_MODE != "manual":
        return
    await asyncio.to_thread(
        input,
        f"\n{_manual_cleanup_steps(spec)}, verify each, then press Enter. ",
    )


async def _wait_for_recovery(
    client: httpx.AsyncClient,
    jwt: _Token,
    incident: dict,
    oracle_client: PrometheusOracleClient,
    tracker: RecoveryOracleTracker,
    creds,
) -> dict:
    """Poll independent evidence; application status is context, never the oracle."""
    elapsed = 0
    terminal_seen_at: Optional[int] = None
    latest = incident

    while elapsed < TIMEOUT_SEC:
        await asyncio.sleep(POLL_INTERVAL_SEC)
        elapsed += POLL_INTERVAL_SEC
        await _observe_oracle(client, oracle_client, tracker)

        current = await _fetch_incident(client, jwt, incident["id"], creds)
        if current is not None:
            latest = current
        application_status = str(latest.get("status") or "").lower()

        if tracker.recovered_at is not None:
            break
        if application_status in TERMINAL_APPLICATION_STATUSES:
            if terminal_seen_at is None:
                terminal_seen_at = elapsed
            elif elapsed - terminal_seen_at >= ORACLE_COMPLETION_GRACE_SEC:
                break
        else:
            terminal_seen_at = None

    return latest


async def _fetch_transcript(client, jwt, incident_id, creds) -> dict:
    r = await client.get(
        f"{creds.base_url}/api/v1/incidents/{incident_id}/transcript",
        headers=await jwt.headers(),
    )
    r.raise_for_status()
    return r.json()


async def _fetch_trace_completeness(client, jwt, incident_id, creds) -> dict:
    deadline = time.monotonic() + ACCOUNTING_WAIT_SEC
    while True:
        response = await client.get(
            f"{creds.base_url}/api/v1/incidents/{incident_id}/agent-metrics",
            headers=await jwt.headers(),
        )
        response.raise_for_status()
        payload = response.json().get("trace_completeness")
        trace = payload if isinstance(payload, dict) else {}
        reasons = trace.get("completeness_reasons") or []
        still_running = trace.get("root_trace_id") is None or any(
            reason == "root_span_not_finalized" for reason in reasons
        )
        if not still_running or time.monotonic() >= deadline:
            return trace
        await asyncio.sleep(min(POLL_INTERVAL_SEC, 1))


def _oracle_result(
    tracker: RecoveryOracleTracker,
    spec: ScenarioSpec,
    *,
    incident_id: Optional[str],
    application_status: str,
):
    return tracker.result(
        scenario=spec.name,
        incident_id=incident_id,
        application_status=application_status,
        dataset_version=spec.dataset_version,
        scenario_version=spec.scenario_version,
        dataset_split=DATASET.split,
        dataset_sha256=DATASET.sha256,
    )


def _score_without_output(spec: ScenarioSpec, result):
    return score_run(
        spec,
        result.status,
        result.application_status,
        "",
        [],
    )


def _record_grade(
    spec: ScenarioSpec,
    result,
    summary_text: str,
    events: list[dict],
    score,
) -> None:
    append_grader_record(
        GRADER_RESULTS_PATH,
        spec=spec,
        oracle_status=result.status,
        application_status=result.application_status,
        summary_text=summary_text,
        events=events,
        score=score,
    )


def _failure_categories(score) -> tuple[str, ...]:
    categories: set[str] = set()
    if score.oracle_status == "INVALID_SCENARIO":
        categories.add("invalid_scenario")
    elif not score.resolved:
        categories.add("unresolved")
    if score.false_resolved:
        categories.add("false_resolved")
    if score.application_status in {"incident_not_created", "stimulus_failed"}:
        categories.add("platform_failure")
    if score.resolved and score.grader_status == "INCOMPLETE":
        categories.add("structured_incomplete")
    if score.resolved and score.grader_status == "FAIL":
        categories.add("structured_failure")
    if not score.safety_ok:
        categories.add("safety_failure")
    return tuple(sorted(categories))


def _record_statistical_trial(
    spec: ScenarioSpec,
    score,
    *,
    trial_index: int,
    latency_seconds: float,
    trace_completeness: Optional[dict],
) -> None:
    if not STATISTICAL_RECORDING:
        return
    pair_id = make_pair_id(
        experiment_id=EXPERIMENT_ID,
        dataset_sha256=DATASET.sha256,
        scenario=spec.name,
        scenario_version=spec.scenario_version,
        trial_index=trial_index,
        pair_seed=PAIR_SEED,
    )
    trace_complete = bool(
        isinstance(trace_completeness, dict)
        and trace_completeness.get("complete") is True
    )
    cost_usd = trace_completeness.get("cost_usd") if trace_complete else None
    failure_categories = set(_failure_categories(score))
    if not trace_complete:
        failure_categories.add("trace_incomplete")
    trial = build_trial_record(
        experiment_id=EXPERIMENT_ID,
        pair_id=pair_id,
        candidate_id=CANDIDATE_ID,
        config_fingerprint=CONFIG_FINGERPRINT,
        scenario=spec.name,
        scenario_version=spec.scenario_version,
        dataset_sha256=DATASET.sha256,
        risk_class=spec.risk_class,
        oracle_status=score.oracle_status,
        resolved=score.resolved,
        false_resolved=score.false_resolved,
        grader_status=score.grader_status,
        safety_ok=score.safety_ok,
        mttr_seconds=score.mttr_seconds,
        latency_seconds=latency_seconds,
        cost_usd=cost_usd,
        trace_complete=trace_complete,
        trace_span_count=(
            int(trace_completeness.get("spans", 0))
            if isinstance(trace_completeness, dict)
            else 0
        ),
        trace_evidence_sha256=(
            trace_completeness.get("records_sha256")
            if isinstance(trace_completeness, dict)
            else None
        ),
        trace_evidence_artifact=(
            trace_completeness.get("artifact_path")
            if isinstance(trace_completeness, dict)
            else None
        ),
        failure_categories=sorted(failure_categories),
        oracle_artifact=str(ORACLE_RESULTS_PATH),
        grader_artifact=str(GRADER_RESULTS_PATH),
    )
    append_trial(TRIAL_RESULTS_PATH, trial)


def _record_confidence_observations(
    spec: ScenarioSpec,
    score,
    *,
    trial_index: int,
) -> None:
    """Pair task-specific self-confidence with exact structured outcomes."""
    if not STATISTICAL_RECORDING:
        return
    pair_id = make_pair_id(
        experiment_id=EXPERIMENT_ID,
        dataset_sha256=DATASET.sha256,
        scenario=spec.name,
        scenario_version=spec.scenario_version,
        trial_index=trial_index,
        pair_seed=PAIR_SEED,
    )
    for task, confidence, outcome in (
        (
            "diagnosis",
            score.diagnosis_confidence,
            score.diagnosis_confidence_outcome,
        ),
        (
            "remediation",
            score.remediation_confidence,
            score.remediation_confidence_outcome,
        ),
    ):
        if confidence is None or outcome is None:
            continue
        append_confidence_record(
            CONFIDENCE_RESULTS_PATH,
            build_confidence_record(
                task=task,
                rubric_version=score.rubric_version,
                raw_confidence=confidence,
                outcome=outcome,
                scenario=spec.name,
                scenario_version=spec.scenario_version,
                dataset_sha256=DATASET.sha256,
                config_fingerprint=CONFIG_FINGERPRINT,
                pair_id=pair_id,
                observed_at=datetime.now(timezone.utc),
                # This runner is the only sanctioned producer of the evidence
                # that may unlock autonomy: a real agent against a real fault.
                evidence_source="live_benchmark",
            ),
        )


async def _run_trial(
    client: httpx.AsyncClient,
    jwt: _Token,
    oracle_client: PrometheusOracleClient,
    fault_adapter: Optional[MeridianAdminConfigAdapter],
    spec: ScenarioSpec,
    creds,
):
    known = await _incident_ids(client, jwt, creds)
    tracker = RecoveryOracleTracker(spec.recovery_probe, datetime.now(timezone.utc))
    await _observe_oracle(client, oracle_client, tracker, baseline=True)

    leases: tuple[Any, ...] = ()
    manual_fault_started = False
    try:
        if tracker.baseline_healthy is True and FAULT_MODE == "automatic":
            if fault_adapter is None:
                raise RuntimeError("automatic fault mode has no adapter")
            leases = await fault_adapter.inject(client, spec)
            # A multi-contract scenario is only fully degraded once its last
            # contract lands, so the run starts from the newest lease.
            started_at = max(lease.injected_at for lease in leases)
        else:
            await _await_manual_fault(spec, tracker)
            manual_fault_started = (
                tracker.baseline_healthy is True and FAULT_MODE == "manual"
            )
            started_at = datetime.now(timezone.utc)
        tracker.begin(started_at)

        try:
            await _fire_alert(client, spec, started_at, creds)
            await _observe_oracle(client, oracle_client, tracker)
        except Exception as exc:
            result = _oracle_result(
                tracker,
                spec,
                incident_id=None,
                application_status="stimulus_failed",
            )
            append_oracle_result(ORACLE_RESULTS_PATH, result)
            score = _score_without_output(spec, result)
            _record_grade(spec, result, "", [], score)
            return score, f"FAILED (stimulus: {exc})", None

        incident = await _wait_new_incident(client, jwt, known, creds)
        if not incident:
            result = _oracle_result(
                tracker,
                spec,
                incident_id=None,
                application_status="incident_not_created",
            )
            append_oracle_result(ORACLE_RESULTS_PATH, result)
            score = _score_without_output(spec, result)
            _record_grade(spec, result, "", [], score)
            return score, "FAILED (no incident)", None

        latest_incident = await _wait_for_recovery(
            client, jwt, incident, oracle_client, tracker, creds
        )
        transcript = await _fetch_transcript(client, jwt, incident["id"], creds)
        trace_completeness = await _fetch_trace_completeness(
            client, jwt, incident["id"], creds
        )
        summary_text = transcript.get("summary") or latest_incident.get("summary") or ""
        events = transcript.get("events", [])
        result = _oracle_result(
            tracker,
            spec,
            incident_id=incident["id"],
            application_status=str(latest_incident.get("status") or "unknown"),
        )
        append_oracle_result(ORACLE_RESULTS_PATH, result)
        score = score_run(
            spec,
            result.status,
            result.application_status,
            summary_text,
            events,
            mttr_seconds=result.mttr_seconds,
            incident_severity=latest_incident.get("severity", ""),
        )
        _record_grade(spec, result, summary_text, events, score)
        if score.resolved:
            line = (
                f"MTTR={score.mttr_seconds:.0f}s "
                f"app={score.application_status} "
                f"rc={_mark(score.root_cause_hit)} "
                f"rem={_mark(score.remediation_hit)} "
                f"sev={_mark(score.severity_hit)} "
                f"safe={_mark(score.safety_ok)}"
            )
        else:
            false_claim = " FALSE_RESOLVED" if score.false_resolved else ""
            line = (
                f"{score.oracle_status} "
                f"(app={score.application_status}){false_claim}"
            )
        return score, line, trace_completeness
    finally:
        if leases and fault_adapter is not None:
            await fault_adapter.cleanup(client, leases)
        elif manual_fault_started:
            await _await_manual_cleanup(spec)


async def run() -> None:
    print("=" * 74)
    print(
        "  SRE-Agent Benchmark  "
        "(oracle recovery · MTTR · root-cause · remediation · severity · safety)"
    )
    print(f"  {len(SCENARIOS)} scenarios × {RUNS_PER_SCENARIO} runs")
    print(
        f"  dataset: {DATASET.dataset_version}/{DATASET.split} "
        f"sha256={DATASET.sha256[:12]}…"
    )
    if SCENARIO_FILTER:
        print(
            f"  SMOKE RUN — {len(SCENARIOS)} of {len(DATASET.scenarios)} scenarios "
            f"(BENCH_SCENARIOS); not a measurement of this split"
        )
    print(f"  fault mode: {FAULT_MODE}")
    print(f"  incident timeout: {TIMEOUT_SEC}s (BENCH_INCIDENT_TIMEOUT_SEC)")
    unmeasurable = timeout_warning(FAULT_MODE, TIMEOUT_SEC)
    if unmeasurable:
        print(unmeasurable)
    print(f"  oracle evidence: {ORACLE_RESULTS_PATH}")
    print(f"  grader evidence: {GRADER_RESULTS_PATH}")
    if STATISTICAL_RECORDING:
        print(
            f"  experiment: {EXPERIMENT_ID}/{CANDIDATE_ID} "
            f"trials={TRIAL_RESULTS_PATH}"
        )
        print(f"  confidence evidence: {CONFIDENCE_RESULTS_PATH}")
    print("=" * 74)

    all_scores = []
    oracle_client = PrometheusOracleClient(
        PROMETHEUS_URL, bearer_token=PROMETHEUS_TOKEN
    )
    fault_adapter = (
        MeridianAdminConfigAdapter(FAULT_SERVICE_URLS)
        if FAULT_MODE == "automatic"
        else None
    )
    async with httpx.AsyncClient(timeout=30) as client:
        creds = await resolve_credentials(client)
        jwt = _Token(client, creds)
        print(f"  logged in as {creds.admin_email}\n")

        by_name = {spec.name: spec for spec in SCENARIOS}
        schedule = build_trial_schedule(
            list(by_name),
            runs_per_scenario=RUNS_PER_SCENARIO,
            pair_seed=PAIR_SEED or "not-recorded",
            dataset_sha256=DATASET.sha256,
            randomize=STATISTICAL_RECORDING,
        )
        for position, (scenario_name, trial_index) in enumerate(schedule, 1):
            spec = by_name[scenario_name]
            print(
                f"── {spec.name} run {trial_index}/{RUNS_PER_SCENARIO}  ",
                end="",
                flush=True,
            )
            trial_started = time.perf_counter()
            score, line, trace_completeness = await _run_trial(
                client,
                jwt,
                oracle_client,
                fault_adapter,
                spec,
                creds,
            )
            _record_statistical_trial(
                spec,
                score,
                trial_index=trial_index,
                latency_seconds=time.perf_counter() - trial_started,
                trace_completeness=trace_completeness,
            )
            _record_confidence_observations(
                spec,
                score,
                trial_index=trial_index,
            )
            all_scores.append(score)
            print(line)
            if position < len(schedule):
                await asyncio.sleep(COOLDOWN_SEC)
        print()

    _report(all_scores)


def _mark(v) -> str:
    return "—" if v is None else ("✓" if v else "✗")


def _report(scores) -> None:
    agg = aggregate(scores)
    print("=" * 74)
    print("  RESULTS")
    print("=" * 74)

    def pct(x):
        return "  n/a" if x is None else f"{x*100:5.1f}%"

    def sec(x):
        return "  n/a" if x is None else f"{x:6.0f}s"

    print(
        f"  Oracle recovery rate : {pct(agg['resolution_rate'])}   ({agg['resolved']}/{agg['runs']})"
    )
    print(f"  False-resolved claims: {agg['false_resolved']}")
    print(f"  Invalid scenarios    : {agg['invalid_scenarios']}")
    print(f"  Root-cause accuracy  : {pct(agg['root_cause_accuracy'])}")
    print(f"  Remediation accuracy : {pct(agg['remediation_accuracy'])}")
    print(f"  Severity accuracy    : {pct(agg['severity_accuracy'])}")
    print(f"  Safety rate          : {pct(agg['safety_rate'])}")
    print(
        "  Structured grades   : "
        f"{agg['structured_complete']} complete / "
        f"{agg['structured_incomplete']} incomplete / "
        f"{agg['structured_failed']} failed"
    )
    print(
        "  Oracle MTTR mean/med.: "
        f"{sec(agg['oracle_mttr_mean_s'])} / "
        f"{sec(agg['oracle_mttr_median_s'])}"
    )
    print("=" * 74)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(1)
