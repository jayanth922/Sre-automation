#!/usr/bin/env python3
"""Strict loader for versioned SRE benchmark scenario manifests."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from recovery_oracle import RecoveryProbe
from scoring import ScenarioSpec

SCHEMA_VERSION = 1
_SPLITS = {"train", "dev", "holdout"}
_RISK_CLASSES = {"low", "medium", "high", "critical"}
_SCENARIO_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALERT_KEYS = {"alertname", "severity", "service", "summary", "description"}
_SCENARIO_KEYS = {
    "id",
    "version",
    "risk_class",
    "taxonomy",
    "provenance",
    "alert",
    "fault",
    "expected_evidence",
    "allowed_action_types",
    "forbidden_action_types",
    "expected_severity_bands",
    "root_cause",
    "recovery_probe",
}


class DatasetError(ValueError):
    """A dataset cannot be loaded without weakening evaluation integrity."""


@dataclass(frozen=True)
class ScenarioDataset:
    dataset_version: str
    schema_version: int
    split: str
    frozen: bool
    sha256: str
    source_path: Path
    scenarios: tuple[ScenarioSpec, ...]


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DatasetError(f"{field} must be an object")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DatasetError(f"{field} must be a non-empty string")
    return value.strip()


def _string_list(value: Any, field: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise DatasetError(f"{field} must be a non-empty string list")
    result = [_string(item, field) for item in value]
    if len(result) != len(set(result)):
        raise DatasetError(f"{field} contains duplicates")
    return result


def _strict_keys(value: dict[str, Any], expected: set[str], field: str) -> None:
    missing = sorted(expected - value.keys())
    extra = sorted(value.keys() - expected)
    if missing or extra:
        raise DatasetError(f"{field} keys mismatch; missing={missing}, extra={extra}")


def _safe_split_path(version_root: Path, filename: str) -> Path:
    path = (version_root / filename).resolve()
    root = version_root.resolve()
    if not path.is_relative_to(root):
        raise DatasetError("split file must remain inside its dataset version")
    return path


def _read_json(path: Path, field: str) -> dict[str, Any]:
    try:
        return _object(json.loads(path.read_text(encoding="utf-8")), field)
    except FileNotFoundError as exc:
        raise DatasetError(f"{field} file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DatasetError(f"{field} is not valid JSON: {exc}") from exc


def _validate_probe(value: Any, scenario_id: str) -> RecoveryProbe:
    probe = _object(value, f"{scenario_id}.recovery_probe")
    expected = {
        "name",
        "query",
        "operator",
        "threshold",
        "unit",
        "required_consecutive_passes",
        "require_failure_observation",
    }
    _strict_keys(probe, expected, f"{scenario_id}.recovery_probe")
    if isinstance(probe["threshold"], bool) or not isinstance(
        probe["threshold"], (int, float)
    ):
        raise DatasetError(f"{scenario_id}.recovery_probe.threshold must be numeric")
    if not isinstance(probe["required_consecutive_passes"], int):
        raise DatasetError(
            f"{scenario_id}.recovery_probe.required_consecutive_passes "
            "must be an integer"
        )
    if not isinstance(probe["require_failure_observation"], bool):
        raise DatasetError(
            f"{scenario_id}.recovery_probe.require_failure_observation "
            "must be boolean"
        )
    try:
        return RecoveryProbe(
            name=_string(probe["name"], f"{scenario_id}.recovery_probe.name"),
            query=_string(probe["query"], f"{scenario_id}.recovery_probe.query"),
            operator=_string(
                probe["operator"], f"{scenario_id}.recovery_probe.operator"
            ),
            threshold=float(probe["threshold"]),
            unit=_string(probe["unit"], f"{scenario_id}.recovery_probe.unit"),
            required_consecutive_passes=probe["required_consecutive_passes"],
            require_failure_observation=probe["require_failure_observation"],
        )
    except ValueError as exc:
        raise DatasetError(f"{scenario_id}.recovery_probe: {exc}") from exc


def _validate_scenario(
    value: Any, *, dataset_version: str, seen_ids: set[str]
) -> ScenarioSpec:
    item = _object(value, "scenario")
    _strict_keys(item, _SCENARIO_KEYS, "scenario")
    scenario_id = _string(item["id"], "scenario.id")
    if not _SCENARIO_ID.fullmatch(scenario_id):
        raise DatasetError(f"invalid scenario id: {scenario_id}")
    if scenario_id in seen_ids:
        raise DatasetError(f"duplicate scenario id: {scenario_id}")
    seen_ids.add(scenario_id)

    scenario_version = _string(item["version"], f"{scenario_id}.version")
    if not _SEMVER.fullmatch(scenario_version):
        raise DatasetError(f"{scenario_id}.version must use MAJOR.MINOR.PATCH")
    risk_class = _string(item["risk_class"], f"{scenario_id}.risk_class")
    if risk_class not in _RISK_CLASSES:
        raise DatasetError(f"{scenario_id}.risk_class is unsupported")

    taxonomy = _object(item["taxonomy"], f"{scenario_id}.taxonomy")
    _strict_keys(taxonomy, {"category", "fault_mode"}, f"{scenario_id}.taxonomy")
    _string(taxonomy["category"], f"{scenario_id}.taxonomy.category")
    _string(taxonomy["fault_mode"], f"{scenario_id}.taxonomy.fault_mode")

    provenance = _object(item["provenance"], f"{scenario_id}.provenance")
    _strict_keys(
        provenance, {"kind", "source", "reference"}, f"{scenario_id}.provenance"
    )
    for key in ("kind", "source", "reference"):
        _string(provenance[key], f"{scenario_id}.provenance.{key}")

    alert = _object(item["alert"], f"{scenario_id}.alert")
    _strict_keys(alert, _ALERT_KEYS, f"{scenario_id}.alert")
    normalized_alert = {
        key: _string(alert[key], f"{scenario_id}.alert.{key}")
        for key in sorted(_ALERT_KEYS)
    }

    fault = _object(item["fault"], f"{scenario_id}.fault")
    _strict_keys(
        fault,
        {"adapter", "target", "inject", "cleanup"},
        f"{scenario_id}.fault",
    )
    _string(fault["adapter"], f"{scenario_id}.fault.adapter")
    _string(fault["target"], f"{scenario_id}.fault.target")
    phases: dict[str, dict[str, Any]] = {}
    for phase in ("inject", "cleanup"):
        contract = _object(fault[phase], f"{scenario_id}.fault.{phase}")
        _strict_keys(contract, {"path", "payload"}, f"{scenario_id}.fault.{phase}")
        path = _string(contract["path"], f"{scenario_id}.fault.{phase}.path")
        if not path.startswith("/") or path.startswith("//"):
            raise DatasetError(f"{scenario_id}.fault.{phase}.path must be relative")
        payload = _object(contract["payload"], f"{scenario_id}.fault.{phase}.payload")
        if not payload:
            raise DatasetError(f"{scenario_id}.fault.{phase}.payload must not be empty")
        phases[phase] = {"path": path, "payload": dict(payload)}
    if phases["inject"]["path"] != phases["cleanup"]["path"]:
        raise DatasetError(f"{scenario_id} inject/cleanup paths must match")
    inject_keys = set(phases["inject"]["payload"])
    cleanup_keys = set(phases["cleanup"]["payload"])
    if inject_keys != cleanup_keys:
        raise DatasetError(f"{scenario_id} inject/cleanup payload keys must match")
    if phases["inject"]["payload"] == phases["cleanup"]["payload"]:
        raise DatasetError(f"{scenario_id} fault inject and cleanup are identical")
    normalized_fault = {
        "adapter": fault["adapter"],
        "target": fault["target"],
        **phases,
    }

    expected_evidence = _string_list(
        item["expected_evidence"], f"{scenario_id}.expected_evidence"
    )
    allowed = set(
        _string_list(
            item["allowed_action_types"],
            f"{scenario_id}.allowed_action_types",
            allow_empty=True,
        )
    )
    forbidden = set(
        _string_list(
            item["forbidden_action_types"],
            f"{scenario_id}.forbidden_action_types",
            allow_empty=True,
        )
    )
    if allowed & forbidden:
        raise DatasetError(
            f"{scenario_id} allowed/forbidden action overlap: "
            f"{sorted(allowed & forbidden)}"
        )
    severity_bands = set(
        _string_list(
            item["expected_severity_bands"],
            f"{scenario_id}.expected_severity_bands",
        )
    )

    root_cause = _object(item["root_cause"], f"{scenario_id}.root_cause")
    _strict_keys(root_cause, {"service", "keywords"}, f"{scenario_id}.root_cause")
    service = _string(root_cause["service"], f"{scenario_id}.root_cause.service")
    keywords = _string_list(
        root_cause["keywords"], f"{scenario_id}.root_cause.keywords"
    )

    return ScenarioSpec(
        name=scenario_id,
        alert=normalized_alert,
        ground_truth_service=service,
        root_cause_keywords=keywords,
        expected_action_types=allowed,
        expected_severity_band=severity_bands,
        recovery_probe=_validate_probe(item["recovery_probe"], scenario_id),
        unsafe_action_types=forbidden,
        dataset_version=dataset_version,
        scenario_version=scenario_version,
        risk_class=risk_class,
        expected_evidence=expected_evidence,
        provenance=dict(provenance),
        fault=normalized_fault,
        taxonomy=dict(taxonomy),
    )


def load_dataset(
    root: Path,
    version: str,
    split: str,
    *,
    allow_holdout: bool = False,
    ci: Optional[bool] = None,
) -> ScenarioDataset:
    """Load one content-addressed split, failing closed on any drift."""
    if split not in _SPLITS:
        raise DatasetError(f"unsupported dataset split: {split}")
    effective_ci = (
        ci
        if ci is not None
        else os.getenv("CI", "").strip().lower() in {"1", "true", "yes"}
    )
    if split == "holdout":
        if effective_ci:
            raise DatasetError("holdout labels are unavailable to CI")
        if not allow_holdout:
            raise DatasetError(
                "holdout split is protected; set explicit local holdout access"
            )

    version_root = root / _string(version, "dataset version directory")
    index = _read_json(version_root / "dataset.json", "dataset index")
    _strict_keys(
        index,
        {"schema_version", "dataset_version", "description", "splits"},
        "dataset index",
    )
    if index["schema_version"] != SCHEMA_VERSION:
        raise DatasetError("unsupported dataset index schema version")
    dataset_version = _string(index["dataset_version"], "dataset_version")
    _string(index["description"], "dataset description")
    splits = _object(index["splits"], "dataset splits")
    if set(splits) != _SPLITS:
        raise DatasetError("dataset index must define train, dev, and holdout")

    split_metadata = _object(splits[split], f"dataset split {split}")
    _strict_keys(split_metadata, {"file", "sha256", "frozen"}, f"dataset split {split}")
    filename = _string(split_metadata["file"], f"dataset split {split}.file")
    expected_sha = _string(split_metadata["sha256"], f"dataset split {split}.sha256")
    if not _SHA256.fullmatch(expected_sha):
        raise DatasetError(f"dataset split {split}.sha256 is invalid")
    if not isinstance(split_metadata["frozen"], bool):
        raise DatasetError(f"dataset split {split}.frozen must be boolean")

    split_path = _safe_split_path(version_root, filename)
    try:
        raw = split_path.read_bytes()
    except FileNotFoundError as exc:
        raise DatasetError(f"dataset split file does not exist: {split_path}") from exc
    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != expected_sha:
        raise DatasetError(
            f"dataset split {split} digest mismatch: "
            f"expected {expected_sha}, got {actual_sha}"
        )
    try:
        payload = _object(json.loads(raw), f"dataset split {split}")
    except json.JSONDecodeError as exc:
        raise DatasetError(f"dataset split {split} is not valid JSON: {exc}") from exc
    _strict_keys(
        payload,
        {"schema_version", "dataset_version", "split", "scenarios"},
        f"dataset split {split}",
    )
    if payload["schema_version"] != SCHEMA_VERSION:
        raise DatasetError(f"dataset split {split} schema version is unsupported")
    if payload["dataset_version"] != dataset_version:
        raise DatasetError(f"dataset split {split} version does not match index")
    if payload["split"] != split:
        raise DatasetError(f"dataset split {split} declares the wrong split")
    if not isinstance(payload["scenarios"], list) or not payload["scenarios"]:
        raise DatasetError(f"dataset split {split} must contain scenarios")

    seen_ids: set[str] = set()
    scenarios = tuple(
        _validate_scenario(scenario, dataset_version=dataset_version, seen_ids=seen_ids)
        for scenario in payload["scenarios"]
    )
    return ScenarioDataset(
        dataset_version=dataset_version,
        schema_version=SCHEMA_VERSION,
        split=split,
        frozen=split_metadata["frozen"],
        sha256=actual_sha,
        source_path=split_path,
        scenarios=scenarios,
    )
