#!/usr/bin/env python3
"""Strict loader for versioned SRE benchmark scenario manifests.

Two schema versions are supported. Schema 1 declares exactly one fault target
per scenario. Schema 2 declares a list of contracts, so a scenario can degrade
two services at once, and additionally requires a *fixture manifest*: a
digest-pinned description of the fault surface the reference workload actually
exposes. With a manifest present, an undeclared target, an undeclared config
knob, an out-of-range value, a cleanup payload that is not the real baseline,
an undeclared alert name and a recovery probe that queries a metric nobody
exports are all load-time errors. That is what keeps the corpus honest: a
scenario cannot describe a fault the fixtures cannot produce.
"""

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

SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2})
MANIFEST_SCHEMA_VERSIONS = frozenset({2})
FIXTURE_MANIFEST_FILENAME = "fixtures.json"

_SPLITS = {"train", "dev", "holdout"}
_RISK_CLASSES = {"low", "medium", "high", "critical"}
_SCENARIO_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALERT_KEYS = {"alertname", "severity", "service", "summary", "description"}
_KNOB_TYPES = {"number", "integer", "boolean"}
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
class FixtureManifest:
    """The fault surface the reference workload really exposes."""

    dataset_version: str
    sha256: str
    source_path: Path
    targets: dict[str, dict[str, Any]]
    alerts: dict[str, dict[str, Any]]
    metrics: tuple[str, ...]


@dataclass(frozen=True)
class ScenarioDataset:
    dataset_version: str
    schema_version: int
    split: str
    frozen: bool
    sha256: str
    source_path: Path
    scenarios: tuple[ScenarioSpec, ...]
    fixtures: Optional[FixtureManifest] = None


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


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DatasetError(f"{field} must be numeric")
    return float(value)


def _same_value(actual: Any, expected: Any) -> bool:
    """Compare config values without letting ``True == 1`` slip through."""
    if isinstance(actual, bool) != isinstance(expected, bool):
        return False
    if isinstance(actual, bool):
        return actual is expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return float(actual) == float(expected)
    return actual == expected


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


# --------------------------------------------------------------- fixtures


def _validate_knob(knob: Any, field: str) -> dict[str, Any]:
    spec = _object(knob, field)
    allowed = {"type", "baseline", "minimum", "maximum", "description"}
    unknown = sorted(spec.keys() - allowed)
    if unknown:
        raise DatasetError(f"{field} declares unknown keys: {unknown}")
    if "type" not in spec or "baseline" not in spec:
        raise DatasetError(f"{field} must declare type and baseline")
    knob_type = _string(spec["type"], f"{field}.type")
    if knob_type not in _KNOB_TYPES:
        raise DatasetError(f"{field}.type is unsupported: {knob_type}")
    baseline = spec["baseline"]
    if knob_type == "boolean":
        if not isinstance(baseline, bool):
            raise DatasetError(f"{field}.baseline must be boolean")
        if "minimum" in spec or "maximum" in spec:
            raise DatasetError(f"{field} cannot bound a boolean knob")
    else:
        if isinstance(baseline, bool) or not isinstance(baseline, (int, float)):
            raise DatasetError(f"{field}.baseline must be numeric")
        if knob_type == "integer" and not isinstance(baseline, int):
            raise DatasetError(f"{field}.baseline must be an integer")
        if "minimum" not in spec or "maximum" not in spec:
            raise DatasetError(f"{field} must bound a numeric knob")
        low = _number(spec["minimum"], f"{field}.minimum")
        high = _number(spec["maximum"], f"{field}.maximum")
        if low > high:
            raise DatasetError(f"{field} minimum exceeds maximum")
        if not low <= float(baseline) <= high:
            raise DatasetError(f"{field}.baseline lies outside its own bounds")
    return dict(spec)


def _load_fixture_manifest(
    version_root: Path, metadata: dict[str, Any], dataset_version: str
) -> FixtureManifest:
    _strict_keys(metadata, {"file", "sha256"}, "dataset fixtures")
    filename = _string(metadata["file"], "dataset fixtures.file")
    expected_sha = _string(metadata["sha256"], "dataset fixtures.sha256")
    if not _SHA256.fullmatch(expected_sha):
        raise DatasetError("dataset fixtures.sha256 is invalid")

    path = _safe_split_path(version_root, filename)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise DatasetError(f"fixture manifest does not exist: {path}") from exc
    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != expected_sha:
        raise DatasetError(
            f"fixture manifest digest mismatch: "
            f"expected {expected_sha}, got {actual_sha}"
        )
    try:
        payload = _object(json.loads(raw), "fixture manifest")
    except json.JSONDecodeError as exc:
        raise DatasetError(f"fixture manifest is not valid JSON: {exc}") from exc

    _strict_keys(
        payload,
        {
            "schema_version",
            "dataset_version",
            "description",
            "source",
            "targets",
            "alerts",
            "metrics",
        },
        "fixture manifest",
    )
    if payload["schema_version"] not in MANIFEST_SCHEMA_VERSIONS:
        raise DatasetError("unsupported fixture manifest schema version")
    if payload["dataset_version"] != dataset_version:
        raise DatasetError("fixture manifest version does not match the index")
    _string(payload["description"], "fixture manifest description")
    source = _object(payload["source"], "fixture manifest source")
    _strict_keys(source, {"name", "reference"}, "fixture manifest source")
    for key in ("name", "reference"):
        _string(source[key], f"fixture manifest source.{key}")

    raw_targets = _object(payload["targets"], "fixture manifest targets")
    if not raw_targets:
        raise DatasetError("fixture manifest must declare at least one target")
    targets: dict[str, dict[str, Any]] = {}
    for name, value in raw_targets.items():
        field = f"fixture target {name}"
        target = _object(value, field)
        _strict_keys(target, {"adapter", "path", "reference", "knobs"}, field)
        _string(target["adapter"], f"{field}.adapter")
        knob_path = _string(target["path"], f"{field}.path")
        if not knob_path.startswith("/") or knob_path.startswith("//"):
            raise DatasetError(f"{field}.path must be relative")
        _string(target["reference"], f"{field}.reference")
        knobs = _object(target["knobs"], f"{field}.knobs")
        if not knobs:
            raise DatasetError(f"{field} must declare at least one knob")
        targets[name] = {
            "adapter": target["adapter"],
            "path": knob_path,
            "reference": target["reference"],
            "knobs": {
                knob_name: _validate_knob(knob, f"{field}.knobs.{knob_name}")
                for knob_name, knob in knobs.items()
            },
        }

    raw_alerts = _object(payload["alerts"], "fixture manifest alerts")
    if not raw_alerts:
        raise DatasetError("fixture manifest must declare at least one alert")
    alerts: dict[str, dict[str, Any]] = {}
    for name, value in raw_alerts.items():
        field = f"fixture alert {name}"
        alert = _object(value, field)
        _strict_keys(alert, {"severity", "service", "source", "reference"}, field)
        for key in ("severity", "service", "source", "reference"):
            _string(alert[key], f"{field}.{key}")
        alerts[name] = dict(alert)

    metrics = tuple(_string_list(payload["metrics"], "fixture manifest metrics"))
    return FixtureManifest(
        dataset_version=dataset_version,
        sha256=actual_sha,
        source_path=path,
        targets=targets,
        alerts=alerts,
        metrics=metrics,
    )


def _check_contract_against_fixtures(
    manifest: FixtureManifest,
    scenario_id: str,
    adapter: str,
    contract: dict[str, Any],
) -> None:
    target = contract["target"]
    declared = manifest.targets.get(target)
    if declared is None:
        raise DatasetError(
            f"{scenario_id} injects into undeclared fixture target: {target}"
        )
    if declared["adapter"] != adapter:
        raise DatasetError(
            f"{scenario_id} uses adapter {adapter!r} but fixture target "
            f"{target} is driven by {declared['adapter']!r}"
        )
    if declared["path"] != contract["path"]:
        raise DatasetError(
            f"{scenario_id} fault path {contract['path']} is not the declared "
            f"config path for {target}"
        )
    knobs = declared["knobs"]
    for key, value in contract["inject"].items():
        knob = knobs.get(key)
        if knob is None:
            raise DatasetError(
                f"{scenario_id} sets undeclared knob {target}.{key}"
            )
        if knob["type"] == "boolean":
            if not isinstance(value, bool):
                raise DatasetError(
                    f"{scenario_id}.{target}.{key} must be boolean"
                )
            if value is knob["baseline"]:
                raise DatasetError(
                    f"{scenario_id}.{target}.{key} injects the healthy baseline"
                )
        else:
            number = _number(value, f"{scenario_id}.{target}.{key}")
            if knob["type"] == "integer" and not isinstance(value, int):
                raise DatasetError(
                    f"{scenario_id}.{target}.{key} must be an integer"
                )
            if not knob["minimum"] <= number <= knob["maximum"]:
                raise DatasetError(
                    f"{scenario_id}.{target}.{key}={number} is outside the "
                    f"declared range [{knob['minimum']}, {knob['maximum']}]"
                )
    for key, value in contract["cleanup"].items():
        knob = knobs[key]
        if not _same_value(value, knob["baseline"]):
            raise DatasetError(
                f"{scenario_id} cleans up {target}.{key} to {value!r}, but the "
                f"fixture baseline is {knob['baseline']!r}; the adapter would "
                "refuse to inject"
            )


def _check_scenario_against_fixtures(
    manifest: FixtureManifest,
    scenario_id: str,
    alert: dict[str, str],
    probe_query: str,
) -> None:
    alertname = alert["alertname"]
    declared = manifest.alerts.get(alertname)
    if declared is None:
        raise DatasetError(
            f"{scenario_id} fires undeclared alert: {alertname}"
        )
    for key in ("severity", "service"):
        if declared[key] != alert[key]:
            raise DatasetError(
                f"{scenario_id}.alert.{key}={alert[key]!r} contradicts the "
                f"declared rule ({declared[key]!r})"
            )
    if not any(metric in probe_query for metric in manifest.metrics):
        raise DatasetError(
            f"{scenario_id}.recovery_probe.query references no declared metric"
        )


# --------------------------------------------------------------- scenarios


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


def _validate_phase(
    raw: Any, *, scenario_id: str, field: str
) -> tuple[str, dict[str, Any]]:
    contract = _object(raw, field)
    _strict_keys(contract, {"path", "payload"}, field)
    path = _string(contract["path"], f"{field}.path")
    if not path.startswith("/") or path.startswith("//"):
        raise DatasetError(f"{field}.path must be relative")
    payload = _object(contract["payload"], f"{field}.payload")
    if not payload:
        raise DatasetError(f"{field}.payload must not be empty")
    return path, dict(payload)


def _normalize_contract(
    *, scenario_id: str, field: str, target: str, inject: Any, cleanup: Any
) -> dict[str, Any]:
    inject_path, inject_payload = _validate_phase(
        inject, scenario_id=scenario_id, field=f"{field}.inject"
    )
    cleanup_path, cleanup_payload = _validate_phase(
        cleanup, scenario_id=scenario_id, field=f"{field}.cleanup"
    )
    if inject_path != cleanup_path:
        raise DatasetError(f"{field} inject/cleanup paths must match")
    if set(inject_payload) != set(cleanup_payload):
        raise DatasetError(f"{field} inject/cleanup payload keys must match")
    if inject_payload == cleanup_payload:
        raise DatasetError(f"{field} inject and cleanup are identical")
    return {
        "target": target,
        "path": inject_path,
        "inject": inject_payload,
        "cleanup": cleanup_payload,
    }


def _validate_fault(
    value: Any, *, scenario_id: str, schema_version: int
) -> dict[str, Any]:
    fault = _object(value, f"{scenario_id}.fault")
    field = f"{scenario_id}.fault"
    if schema_version == 1:
        _strict_keys(fault, {"adapter", "target", "inject", "cleanup"}, field)
        adapter = _string(fault["adapter"], f"{field}.adapter")
        contracts = [
            _normalize_contract(
                scenario_id=scenario_id,
                field=field,
                target=_string(fault["target"], f"{field}.target"),
                inject=fault["inject"],
                cleanup=fault["cleanup"],
            )
        ]
        return {"adapter": adapter, "contracts": contracts}

    _strict_keys(fault, {"adapter", "contracts"}, field)
    adapter = _string(fault["adapter"], f"{field}.adapter")
    raw_contracts = fault["contracts"]
    if not isinstance(raw_contracts, list) or not raw_contracts:
        raise DatasetError(f"{field}.contracts must be a non-empty list")
    contracts: list[dict[str, Any]] = []
    seen_targets: set[str] = set()
    for index, raw in enumerate(raw_contracts):
        contract_field = f"{field}.contracts[{index}]"
        item = _object(raw, contract_field)
        _strict_keys(item, {"target", "inject", "cleanup"}, contract_field)
        target = _string(item["target"], f"{contract_field}.target")
        if target in seen_targets:
            raise DatasetError(f"{scenario_id} declares {target} twice")
        seen_targets.add(target)
        contracts.append(
            _normalize_contract(
                scenario_id=scenario_id,
                field=contract_field,
                target=target,
                inject=item["inject"],
                cleanup=item["cleanup"],
            )
        )
    return {"adapter": adapter, "contracts": contracts}


def _validate_scenario(
    value: Any,
    *,
    dataset_version: str,
    schema_version: int,
    seen_ids: set[str],
    fixtures: Optional[FixtureManifest] = None,
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

    normalized_fault = _validate_fault(
        item["fault"], scenario_id=scenario_id, schema_version=schema_version
    )

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

    probe = _validate_probe(item["recovery_probe"], scenario_id)

    if fixtures is not None:
        for contract in normalized_fault["contracts"]:
            _check_contract_against_fixtures(
                fixtures, scenario_id, normalized_fault["adapter"], contract
            )
        _check_scenario_against_fixtures(
            fixtures, scenario_id, normalized_alert, probe.query
        )

    return ScenarioSpec(
        name=scenario_id,
        alert=normalized_alert,
        ground_truth_service=service,
        root_cause_keywords=keywords,
        expected_action_types=allowed,
        expected_severity_band=severity_bands,
        recovery_probe=probe,
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
    schema_version = index.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise DatasetError("unsupported dataset index schema version")
    index_keys = {"schema_version", "dataset_version", "description", "splits"}
    if schema_version in MANIFEST_SCHEMA_VERSIONS:
        index_keys = index_keys | {"fixtures"}
    _strict_keys(index, index_keys, "dataset index")
    dataset_version = _string(index["dataset_version"], "dataset_version")
    _string(index["description"], "dataset description")
    splits = _object(index["splits"], "dataset splits")
    if set(splits) != _SPLITS:
        raise DatasetError("dataset index must define train, dev, and holdout")

    fixtures: Optional[FixtureManifest] = None
    if schema_version in MANIFEST_SCHEMA_VERSIONS:
        fixtures = _load_fixture_manifest(
            version_root,
            _object(index["fixtures"], "dataset fixtures"),
            dataset_version,
        )

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
    if payload["schema_version"] != schema_version:
        raise DatasetError(f"dataset split {split} schema version is unsupported")
    if payload["dataset_version"] != dataset_version:
        raise DatasetError(f"dataset split {split} version does not match index")
    if payload["split"] != split:
        raise DatasetError(f"dataset split {split} declares the wrong split")
    if not isinstance(payload["scenarios"], list) or not payload["scenarios"]:
        raise DatasetError(f"dataset split {split} must contain scenarios")

    seen_ids: set[str] = set()
    scenarios = tuple(
        _validate_scenario(
            scenario,
            dataset_version=dataset_version,
            schema_version=schema_version,
            seen_ids=seen_ids,
            fixtures=fixtures,
        )
        for scenario in payload["scenarios"]
    )
    return ScenarioDataset(
        dataset_version=dataset_version,
        schema_version=schema_version,
        split=split,
        frozen=split_metadata["frozen"],
        sha256=actual_sha,
        source_path=split_path,
        scenarios=scenarios,
        fixtures=fixtures,
    )


def repin(root: Path, version: str) -> dict[str, str]:
    """Recompute every digest in a dataset index, then prove it still loads.

    Hand-editing a split breaks its pinned digest by design. This recomputes
    the index from the files on disk and immediately re-loads every split, so
    a re-pin can never turn an invalid dataset into a loadable one — it only
    restores content addressing after a deliberate edit.
    """
    version_root = root / version
    index_path = version_root / "dataset.json"
    index = _read_json(index_path, "dataset index")
    digests: dict[str, str] = {}

    fixtures = index.get("fixtures")
    if isinstance(fixtures, dict) and isinstance(fixtures.get("file"), str):
        digest = _sha256_file(version_root / fixtures["file"])
        fixtures["sha256"] = digest
        digests[fixtures["file"]] = digest
    for split, metadata in sorted(index.get("splits", {}).items()):
        digest = _sha256_file(version_root / metadata["file"])
        metadata["sha256"] = digest
        digests[metadata["file"]] = digest

    index_path.write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    for split in sorted(_SPLITS):
        load_dataset(
            root, version, split, allow_holdout=split == "holdout", ci=False
        )
    return digests


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise DatasetError(f"cannot digest {path}") from exc


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent / "datasets",
        help="directory containing the dataset version directories",
    )
    parser.add_argument("--version", default="v2", help="dataset version directory")
    parser.add_argument(
        "--repin",
        action="store_true",
        help="recompute the pinned digests after a deliberate edit",
    )
    args = parser.parse_args(argv)

    try:
        if args.repin:
            for name, digest in repin(args.root, args.version).items():
                print(f"{digest}  {name}")
            return 0
        total = 0
        for split in ("train", "dev", "holdout"):
            dataset = load_dataset(
                args.root,
                args.version,
                split,
                allow_holdout=split == "holdout",
                ci=False,
            )
            total += len(dataset.scenarios)
            print(f"{split:8s} {len(dataset.scenarios):3d} scenarios  {dataset.sha256}")
        print(f"{'total':8s} {total:3d} scenarios")
    except DatasetError as exc:
        print(f"FAIL — {exc}")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
