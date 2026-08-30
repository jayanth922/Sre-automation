#!/usr/bin/env python3
"""A09 content-addressed release evidence, rollout, and CI impact gate."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = 1
_SHA256_LENGTH = 64
_REQUIRED_EVIDENCE = {
    "paired_trials",
    "adversarial_observations",
    "root_traces",
}
_CHANGE_CLASSES = {"prompt", "model", "tool", "mixed"}


class ReleaseGateError(ValueError):
    """Release evidence or policy is malformed or cannot support promotion."""


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_value(value: Any, field: str) -> str:
    text = _string(value, field)
    if len(text) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise ReleaseGateError(f"{field} must be lowercase SHA-256")
    return text


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseGateError(f"{field} must be a non-empty string")
    return value.strip()


def _number(value: Any, field: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReleaseGateError(f"{field} must be numeric")
    result = float(value)
    if result < minimum or result != result or result in {float("inf"), -float("inf")}:
        raise ReleaseGateError(f"{field} must be finite and at least {minimum}")
    return result


def _bounded_number(value: Any, field: str, *, minimum: float, maximum: float) -> float:
    result = _number(value, field, minimum=minimum)
    if result > maximum:
        raise ReleaseGateError(f"{field} must be at most {maximum}")
    return result


def _integer(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ReleaseGateError(f"{field} must be an integer of at least {minimum}")
    return value


def _object(
    value: Any,
    field: str,
    *,
    exact_keys: Optional[set[str]] = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseGateError(f"{field} must be an object")
    if exact_keys is not None and set(value) != exact_keys:
        raise ReleaseGateError(f"{field} keys do not match schema v{SCHEMA_VERSION}")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ReleaseGateError(f"{field} must be a list")
    return value


def _load_json(path: Path, field: str) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ReleaseGateError(f"{field} does not exist: {path}") from exc
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseGateError(f"{field} is not valid JSON: {path}") from exc
    return _object(payload, field), _sha256(raw)


def _resolve_inside(base: Path, relative: Any, field: str) -> Path:
    value = _string(relative, field)
    candidate = (base / value).resolve()
    root = base.resolve()
    if candidate != root and root not in candidate.parents:
        raise ReleaseGateError(f"{field} escapes its evidence directory")
    return candidate


def load_policy(path: Path) -> tuple[dict[str, Any], str]:
    policy, digest = _load_json(path, "policy")
    _object(
        policy,
        "policy",
        exact_keys={
            "schema_version",
            "policy_id",
            "statistical",
            "safety",
            "rollout",
            "protected_path_rules",
        },
    )
    if policy["schema_version"] != SCHEMA_VERSION:
        raise ReleaseGateError("unsupported release policy schema")
    _string(policy["policy_id"], "policy.policy_id")
    statistical = _object(
        policy["statistical"],
        "policy.statistical",
        exact_keys={
            "minimum_pairs",
            "recovery_noninferiority_margin",
            "quality_noninferiority_margin",
            "maximum_latency_regression_ratio",
            "maximum_cost_regression_ratio",
        },
    )
    _integer(
        statistical["minimum_pairs"], "policy.statistical.minimum_pairs", minimum=1
    )
    for key in (
        "recovery_noninferiority_margin",
        "quality_noninferiority_margin",
        "maximum_latency_regression_ratio",
        "maximum_cost_regression_ratio",
    ):
        _bounded_number(
            statistical[key], f"policy.statistical.{key}", minimum=0.0, maximum=1.0
        )
    safety = _object(
        policy["safety"],
        "policy.safety",
        exact_keys={
            "maximum_candidate_safety_failures",
            "maximum_adversarial_failures",
            "require_complete_structured_grades",
            "require_complete_trace_cost",
        },
    )
    _integer(
        safety["maximum_candidate_safety_failures"],
        "policy.safety.maximum_candidate_safety_failures",
    )
    _integer(
        safety["maximum_adversarial_failures"],
        "policy.safety.maximum_adversarial_failures",
    )
    for key in ("require_complete_structured_grades", "require_complete_trace_cost"):
        if safety[key] is not True:
            raise ReleaseGateError(f"policy.safety.{key} must be true")
    rollout = _object(
        policy["rollout"],
        "policy.rollout",
        exact_keys={
            "initial_stage",
            "maximum_canary_traffic_percent",
            "minimum_shadow_observation_minutes",
            "minimum_canary_observation_minutes",
            "automatic_rollback_required",
        },
    )
    if rollout["initial_stage"] != "shadow":
        raise ReleaseGateError("policy rollout must begin in shadow")
    _bounded_number(
        rollout["maximum_canary_traffic_percent"],
        "policy.rollout.maximum_canary_traffic_percent",
        minimum=0.01,
        maximum=100.0,
    )
    _integer(
        rollout["minimum_shadow_observation_minutes"],
        "policy.rollout.minimum_shadow_observation_minutes",
        minimum=1,
    )
    _integer(
        rollout["minimum_canary_observation_minutes"],
        "policy.rollout.minimum_canary_observation_minutes",
        minimum=1,
    )
    if rollout["automatic_rollback_required"] is not True:
        raise ReleaseGateError("policy must require automatic rollback")
    rules = _list(policy["protected_path_rules"], "policy.protected_path_rules")
    if not rules:
        raise ReleaseGateError("policy requires protected path rules")
    for index, rule in enumerate(rules):
        item = _object(
            rule,
            f"policy.protected_path_rules[{index}]",
            exact_keys={"category", "pattern"},
        )
        if item["category"] not in _CHANGE_CLASSES - {"mixed"}:
            raise ReleaseGateError("protected path category is unsupported")
        _string(item["pattern"], f"policy.protected_path_rules[{index}].pattern")
    return policy, digest


def _protected_files(repo_root: Path, policy: dict[str, Any]) -> tuple[Path, ...]:
    files: set[Path] = set()
    for rule in policy["protected_path_rules"]:
        pattern = rule["pattern"]
        if pattern.endswith("/**"):
            directory = repo_root / pattern[:-3]
            if directory.is_dir():
                files.update(
                    path
                    for path in directory.rglob("*")
                    if path.is_file()
                    and "__pycache__" not in path.parts
                    and not path.name.endswith((".pyc", ".pyo", ".DS_Store"))
                )
        else:
            path = repo_root / pattern
            if (
                path.is_file()
                and "__pycache__" not in path.parts
                and not path.name.endswith((".pyc", ".pyo", ".DS_Store"))
            ):
                files.add(path)
    return tuple(sorted(files, key=lambda path: path.relative_to(repo_root).as_posix()))


def protected_source_digest(repo_root: Path, policy: dict[str, Any]) -> str:
    """Hash all protected prompt/model/tool sources without release artifacts."""
    root = repo_root.resolve()
    values = [
        {
            "path": path.resolve().relative_to(root).as_posix(),
            "sha256": _sha256(path.read_bytes()),
        }
        for path in _protected_files(root, policy)
    ]
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    return _sha256(encoded)


def protected_change_classes(
    changed_files: list[str], policy: dict[str, Any]
) -> tuple[str, ...]:
    categories = {
        rule["category"]
        for path in changed_files
        for rule in policy["protected_path_rules"]
        if fnmatch.fnmatch(path.strip(), rule["pattern"])
    }
    return tuple(sorted(categories))


def _artifact_evidence(bundle_path: Path, values: Any) -> list[dict[str, Any]]:
    descriptors = _list(values, "bundle.evidence_artifacts")
    evidence: list[dict[str, Any]] = []
    kinds: set[str] = set()
    for index, value in enumerate(descriptors):
        field = f"bundle.evidence_artifacts[{index}]"
        descriptor = _object(
            value,
            field,
            exact_keys={"kind", "path", "sha256", "records"},
        )
        kind = _string(descriptor["kind"], f"{field}.kind")
        if kind in kinds:
            raise ReleaseGateError(f"duplicate evidence kind: {kind}")
        kinds.add(kind)
        expected = _sha256_value(descriptor["sha256"], f"{field}.sha256")
        records = _integer(descriptor["records"], f"{field}.records", minimum=1)
        path = _resolve_inside(bundle_path.parent, descriptor["path"], f"{field}.path")
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise ReleaseGateError(f"evidence artifact does not exist: {path}") from exc
        actual = _sha256(raw)
        if actual != expected:
            raise ReleaseGateError(f"evidence artifact digest mismatch: {kind}")
        if path.suffix == ".jsonl":
            try:
                lines = raw.decode("utf-8").splitlines()
                if not lines or any(not line.strip() for line in lines):
                    raise ValueError
                for line in lines:
                    json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise ReleaseGateError(
                    f"evidence artifact is not strict JSONL: {kind}"
                ) from exc
            if len(lines) != records:
                raise ReleaseGateError(
                    f"evidence artifact record count mismatch: {kind}"
                )
        evidence.append(
            {
                "kind": kind,
                "path": str(path),
                "sha256": actual,
                "records": records,
            }
        )
    if kinds != _REQUIRED_EVIDENCE:
        raise ReleaseGateError(
            "bundle evidence kinds are incomplete; "
            f"missing={sorted(_REQUIRED_EVIDENCE - kinds)}, "
            f"unknown={sorted(kinds - _REQUIRED_EVIDENCE)}"
        )
    return evidence


def _candidate(value: Any, field: str) -> dict[str, str]:
    result = _object(
        value,
        field,
        exact_keys={"candidate_id", "config_fingerprint", "source_digest"},
    )
    return {
        "candidate_id": _string(result["candidate_id"], f"{field}.candidate_id"),
        "config_fingerprint": _sha256_value(
            result["config_fingerprint"], f"{field}.config_fingerprint"
        ),
        "source_digest": _sha256_value(
            result["source_digest"], f"{field}.source_digest"
        ),
    }


def _mean(summary: dict[str, Any], key: str, field: str) -> float:
    metric = _object(summary.get(key), f"{field}.{key}")
    return _number(metric.get("mean"), f"{field}.{key}.mean")


def _regression_ratio(baseline: float, candidate: float) -> float:
    if baseline == 0:
        return 0.0 if candidate == 0 else float("inf")
    return (candidate - baseline) / baseline


def evaluate_bundle(
    bundle_path: Path,
    policy_path: Path,
    *,
    expected_bundle_sha256: Optional[str] = None,
    expected_policy_sha256: Optional[str] = None,
) -> dict[str, Any]:
    """Evaluate one release bundle and return deterministic promotion evidence."""
    policy, policy_sha = load_policy(policy_path)
    bundle, bundle_sha = _load_json(bundle_path, "release bundle")
    if expected_bundle_sha256 and bundle_sha != _sha256_value(
        expected_bundle_sha256, "expected_bundle_sha256"
    ):
        raise ReleaseGateError("release bundle digest mismatch")
    if expected_policy_sha256 and policy_sha != _sha256_value(
        expected_policy_sha256, "expected_policy_sha256"
    ):
        raise ReleaseGateError("release policy digest mismatch")
    _object(
        bundle,
        "bundle",
        exact_keys={
            "schema_version",
            "release_id",
            "change_class",
            "baseline",
            "candidate",
            "statistical_report",
            "adversarial_report",
            "evidence_artifacts",
            "rollout_plan",
        },
    )
    if bundle["schema_version"] != SCHEMA_VERSION:
        raise ReleaseGateError("unsupported release bundle schema")
    release_id = _string(bundle["release_id"], "bundle.release_id")
    change_class = _string(bundle["change_class"], "bundle.change_class")
    if change_class not in _CHANGE_CLASSES:
        raise ReleaseGateError("bundle.change_class is unsupported")
    baseline = _candidate(bundle["baseline"], "bundle.baseline")
    candidate = _candidate(bundle["candidate"], "bundle.candidate")
    if baseline["candidate_id"] == candidate["candidate_id"]:
        raise ReleaseGateError("baseline and candidate IDs must differ")
    if baseline["config_fingerprint"] == candidate["config_fingerprint"]:
        raise ReleaseGateError("baseline and candidate fingerprints must differ")
    evidence = _artifact_evidence(bundle_path, bundle["evidence_artifacts"])
    evidence_by_kind = {item["kind"]: item for item in evidence}

    reasons: list[str] = []

    def require(condition: bool, reason: str) -> None:
        if not condition:
            reasons.append(reason)

    statistical = _object(bundle["statistical_report"], "bundle.statistical_report")
    if statistical.get("schema_version") != 2:
        raise ReleaseGateError("statistical report must use schema v2")
    before = _object(statistical.get("baseline"), "statistical_report.baseline")
    after = _object(statistical.get("candidate"), "statistical_report.candidate")
    require(
        before.get("candidate_id") == baseline["candidate_id"]
        and before.get("config_fingerprint") == baseline["config_fingerprint"],
        "statistical baseline identity does not match bundle",
    )
    require(
        after.get("candidate_id") == candidate["candidate_id"]
        and after.get("config_fingerprint") == candidate["config_fingerprint"],
        "statistical candidate identity does not match bundle",
    )
    paired = _object(statistical.get("paired"), "statistical_report.paired")
    pair_count = _integer(paired.get("pair_count"), "paired.pair_count")
    statistical_policy = policy["statistical"]
    require(
        pair_count >= statistical_policy["minimum_pairs"],
        "paired trial count is below policy minimum",
    )
    require(
        evidence_by_kind["paired_trials"]["records"] >= pair_count * 2,
        "paired-trial evidence has fewer than two records per pair",
    )
    require(
        evidence_by_kind["root_traces"]["records"] >= pair_count,
        "root-trace evidence has fewer records than paired trials",
    )
    for metric_name, policy_name in (
        ("recovery", "recovery_noninferiority_margin"),
        ("quality", "quality_noninferiority_margin"),
    ):
        metric = _object(paired.get(metric_name), f"paired.{metric_name}")
        interval = _list(
            metric.get("conservative_wilson_95"),
            f"paired.{metric_name}.conservative_wilson_95",
        )
        if len(interval) != 2:
            raise ReleaseGateError(
                f"paired.{metric_name} interval must have two values"
            )
        lower = _bounded_number(
            interval[0],
            f"paired.{metric_name}.conservative_wilson_95[0]",
            minimum=-1.0,
            maximum=1.0,
        )
        upper = _bounded_number(
            interval[1],
            f"paired.{metric_name}.conservative_wilson_95[1]",
            minimum=-1.0,
            maximum=1.0,
        )
        if lower > upper:
            raise ReleaseGateError(f"paired.{metric_name} interval is inverted")
        require(
            lower >= -statistical_policy[policy_name],
            f"candidate {metric_name} exceeds approved non-inferiority margin",
        )
    baseline_latency = _mean(before, "latency_seconds", "statistical_report.baseline")
    candidate_latency = _mean(after, "latency_seconds", "statistical_report.candidate")
    latency_ratio = _regression_ratio(baseline_latency, candidate_latency)
    require(
        latency_ratio <= statistical_policy["maximum_latency_regression_ratio"],
        "candidate latency exceeds approved regression ratio",
    )
    baseline_cost = _mean(before, "cost_usd", "statistical_report.baseline")
    candidate_cost = _mean(after, "cost_usd", "statistical_report.candidate")
    cost_ratio = _regression_ratio(baseline_cost, candidate_cost)
    require(
        cost_ratio <= statistical_policy["maximum_cost_regression_ratio"],
        "candidate cost exceeds approved regression ratio",
    )
    safety_policy = policy["safety"]
    runs = _integer(after.get("runs"), "statistical_report.candidate.runs", minimum=1)
    safety_rate = _bounded_number(
        after.get("safety_rate"),
        "statistical_report.candidate.safety_rate",
        minimum=0.0,
        maximum=1.0,
    )
    safety_failures = round((1.0 - safety_rate) * runs)
    require(
        safety_failures <= safety_policy["maximum_candidate_safety_failures"],
        "candidate has a safety failure",
    )
    structured_complete = _integer(
        after.get("structured_complete"),
        "statistical_report.candidate.structured_complete",
    )
    require(
        structured_complete == runs,
        "candidate structured grades are incomplete",
    )
    cost_summary = _object(
        after.get("cost_usd"), "statistical_report.candidate.cost_usd"
    )
    require(
        _bounded_number(
            cost_summary.get("coverage"),
            "candidate.cost_usd.coverage",
            minimum=0.0,
            maximum=1.0,
        )
        == 1.0,
        "candidate trace-complete cost coverage is incomplete",
    )
    statistical_decision = _object(
        statistical.get("release_decision"), "statistical_report.release_decision"
    )
    require(
        statistical_decision.get("status") == "PROMOTE",
        "statistical evaluator blocked the candidate",
    )
    statistical_raw = _list(
        statistical.get("raw_artifacts"), "statistical_report.raw_artifacts"
    )
    require(
        any(
            isinstance(item, dict)
            and item.get("sha256") == evidence_by_kind["paired_trials"]["sha256"]
            for item in statistical_raw
        ),
        "statistical report is not linked to paired-trial evidence",
    )

    adversarial = _object(bundle["adversarial_report"], "bundle.adversarial_report")
    if adversarial.get("schema_version") != 1:
        raise ReleaseGateError("adversarial report must use schema v1")
    require(
        adversarial.get("config_fingerprint") == candidate["config_fingerprint"],
        "adversarial report fingerprint does not match candidate",
    )
    adversarial_failures = _integer(
        adversarial.get("failed"), "adversarial_report.failed"
    )
    adversarial_cases = _integer(
        adversarial.get("cases"), "adversarial_report.cases", minimum=1
    )
    require(
        evidence_by_kind["adversarial_observations"]["records"] == adversarial_cases,
        "adversarial evidence count does not match report",
    )
    require(
        adversarial_failures <= safety_policy["maximum_adversarial_failures"],
        "candidate has an adversarial safety failure",
    )
    adversarial_decision = _object(
        adversarial.get("release_decision"), "adversarial_report.release_decision"
    )
    require(
        adversarial_decision.get("status") == "PASS",
        "adversarial evaluator blocked the candidate",
    )
    adversarial_raw = _list(
        adversarial.get("raw_artifacts"), "adversarial_report.raw_artifacts"
    )
    require(
        any(
            isinstance(item, dict)
            and item.get("sha256")
            == evidence_by_kind["adversarial_observations"]["sha256"]
            for item in adversarial_raw
        ),
        "adversarial report is not linked to observation evidence",
    )

    rollout = _object(
        bundle["rollout_plan"],
        "bundle.rollout_plan",
        exact_keys={"initial_stage", "stages", "rollback"},
    )
    require(
        rollout["initial_stage"] == policy["rollout"]["initial_stage"],
        "rollout does not begin in the policy shadow stage",
    )
    stages = _list(rollout["stages"], "rollout_plan.stages")
    require(
        len(stages) == 2
        and all(isinstance(stage, dict) for stage in stages)
        and [stage.get("name") for stage in stages] == ["shadow", "canary"],
        "rollout must define ordered shadow and canary stages",
    )
    if len(stages) == 2 and all(isinstance(stage, dict) for stage in stages):
        shadow, canary = stages
        require(shadow.get("traffic_percent") == 0, "shadow traffic must be zero")
        require(
            _integer(
                shadow.get("observation_minutes"),
                "rollout_plan.stages.shadow.observation_minutes",
            )
            >= policy["rollout"]["minimum_shadow_observation_minutes"],
            "shadow observation window is below policy minimum",
        )
        canary_traffic = _bounded_number(
            canary.get("traffic_percent"),
            "rollout_plan.stages.canary.traffic_percent",
            minimum=0.01,
            maximum=100.0,
        )
        require(
            canary_traffic <= policy["rollout"]["maximum_canary_traffic_percent"],
            "canary traffic exceeds policy maximum",
        )
        require(
            _integer(
                canary.get("observation_minutes"),
                "rollout_plan.stages.canary.observation_minutes",
            )
            >= policy["rollout"]["minimum_canary_observation_minutes"],
            "canary observation window is below policy minimum",
        )
    rollback = _object(
        rollout["rollback"],
        "rollout_plan.rollback",
        exact_keys={"automatic", "target_config_fingerprint", "triggers"},
    )
    require(rollback["automatic"] is True, "rollout rollback is not automatic")
    require(
        rollback["target_config_fingerprint"] == baseline["config_fingerprint"],
        "rollback target is not the evaluated baseline",
    )
    triggers = _object(
        rollback["triggers"],
        "rollout_plan.rollback.triggers",
        exact_keys={
            "safety_failure_count_above",
            "recovery_delta_below",
            "quality_delta_below",
            "latency_regression_ratio_above",
            "cost_regression_ratio_above",
            "trace_incomplete",
        },
    )
    expected_triggers = {
        "safety_failure_count_above": safety_policy[
            "maximum_candidate_safety_failures"
        ],
        "recovery_delta_below": -statistical_policy["recovery_noninferiority_margin"],
        "quality_delta_below": -statistical_policy["quality_noninferiority_margin"],
        "latency_regression_ratio_above": statistical_policy[
            "maximum_latency_regression_ratio"
        ],
        "cost_regression_ratio_above": statistical_policy[
            "maximum_cost_regression_ratio"
        ],
        "trace_incomplete": True,
    }
    require(triggers == expected_triggers, "rollback triggers do not match policy")

    reasons = list(dict.fromkeys(reasons))
    status = "PROMOTE" if not reasons else "BLOCK"
    return {
        "schema_version": SCHEMA_VERSION,
        "release_id": release_id,
        "change_class": change_class,
        "policy": {
            "policy_id": policy["policy_id"],
            "path": str(policy_path),
            "sha256": policy_sha,
        },
        "bundle": {"path": str(bundle_path), "sha256": bundle_sha},
        "baseline": baseline,
        "candidate": candidate,
        "metrics": {
            "pairs": pair_count,
            "latency_regression_ratio": latency_ratio,
            "cost_regression_ratio": cost_ratio,
            "adversarial_failures": adversarial_failures,
            "candidate_safety_failures": safety_failures,
        },
        "evidence_artifacts": evidence,
        "rollout_plan": rollout,
        "release_decision": {"status": status, "reasons": reasons},
    }


def run_matrix(matrix_path: Path, output_path: Optional[Path] = None) -> dict[str, Any]:
    matrix, matrix_sha = _load_json(matrix_path, "release matrix")
    _object(
        matrix,
        "release matrix",
        exact_keys={"schema_version", "matrix_id", "policy", "cases"},
    )
    if matrix["schema_version"] != SCHEMA_VERSION:
        raise ReleaseGateError("unsupported release matrix schema")
    matrix_id = _string(matrix["matrix_id"], "matrix.matrix_id")
    policy_descriptor = _object(
        matrix["policy"],
        "matrix.policy",
        exact_keys={"path", "sha256"},
    )
    policy_path = _resolve_inside(
        matrix_path.parent, policy_descriptor["path"], "matrix.policy.path"
    )
    policy_sha = _sha256_value(policy_descriptor["sha256"], "matrix.policy.sha256")
    cases = _list(matrix["cases"], "matrix.cases")
    if not cases:
        raise ReleaseGateError("release matrix is empty")
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, value in enumerate(cases):
        field = f"matrix.cases[{index}]"
        case = _object(
            value,
            field,
            exact_keys={"case_id", "change_class", "bundle", "expected_status"},
        )
        case_id = _string(case["case_id"], f"{field}.case_id")
        if case_id in seen:
            raise ReleaseGateError(f"duplicate release matrix case: {case_id}")
        seen.add(case_id)
        expected = case["expected_status"]
        if expected not in {"PROMOTE", "BLOCK"}:
            raise ReleaseGateError(f"{field}.expected_status is unsupported")
        descriptor = _object(
            case["bundle"],
            f"{field}.bundle",
            exact_keys={"path", "sha256"},
        )
        bundle_path = _resolve_inside(
            matrix_path.parent, descriptor["path"], f"{field}.bundle.path"
        )
        report = evaluate_bundle(
            bundle_path,
            policy_path,
            expected_bundle_sha256=descriptor["sha256"],
            expected_policy_sha256=policy_sha,
        )
        actual = report["release_decision"]["status"]
        declared_class = _string(case["change_class"], f"{field}.change_class")
        matches = actual == expected and report["change_class"] == declared_class
        results.append(
            {
                "case_id": case_id,
                "change_class": declared_class,
                "expected_status": expected,
                "actual_status": actual,
                "status": "PASS" if matches else "FAIL",
                "bundle_sha256": report["bundle"]["sha256"],
                "reasons": report["release_decision"]["reasons"],
            }
        )
    status = (
        "PASS" if all(result["status"] == "PASS" for result in results) else "BLOCK"
    )
    report = {
        "schema_version": SCHEMA_VERSION,
        "matrix_id": matrix_id,
        "matrix_sha256": matrix_sha,
        "policy_sha256": policy_sha,
        "cases": results,
        "release_decision": {
            "status": status,
            "reasons": (
                []
                if status == "PASS"
                else ["release matrix produced an unexpected decision"]
            ),
        },
    }
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return report


def evaluate_impact(
    changed_files_path: Path,
    policy_path: Path,
    *,
    repo_root: Path,
    bundle_path: Optional[Path],
) -> dict[str, Any]:
    policy, policy_sha = load_policy(policy_path)
    try:
        changed_files = [
            line.strip()
            for line in changed_files_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except FileNotFoundError as exc:
        raise ReleaseGateError("changed-files artifact does not exist") from exc
    categories = protected_change_classes(changed_files, policy)
    if not categories:
        return {
            "schema_version": SCHEMA_VERSION,
            "policy_sha256": policy_sha,
            "protected_changes": [],
            "release_decision": {"status": "NOT_REQUIRED", "reasons": []},
        }
    if bundle_path is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "policy_sha256": policy_sha,
            "protected_changes": list(categories),
            "release_decision": {
                "status": "BLOCK",
                "reasons": [
                    "protected prompt/model/tool change lacks release evidence"
                ],
            },
        }
    report = evaluate_bundle(bundle_path, policy_path)
    reasons = list(report["release_decision"]["reasons"])
    expected_class = categories[0] if len(categories) == 1 else "mixed"
    if report["change_class"] != expected_class:
        reasons.append("release evidence change class does not cover protected changes")
    actual_digest = protected_source_digest(repo_root, policy)
    if report["candidate"]["source_digest"] != actual_digest:
        reasons.append("release evidence does not match protected source digest")
    report["protected_changes"] = list(categories)
    report["actual_source_digest"] = actual_digest
    report["release_decision"] = {
        "status": "PROMOTE" if not reasons else "BLOCK",
        "reasons": list(dict.fromkeys(reasons)),
    }
    return report


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate A09 release evidence")
    subparsers = parser.add_subparsers(dest="command", required=True)

    evaluate_parser = subparsers.add_parser("evaluate")
    evaluate_parser.add_argument("--bundle", type=Path, required=True)
    evaluate_parser.add_argument("--policy", type=Path, required=True)
    evaluate_parser.add_argument("--output", type=Path, required=True)

    matrix_parser = subparsers.add_parser("matrix")
    matrix_parser.add_argument("--matrix", type=Path, required=True)
    matrix_parser.add_argument("--output", type=Path, required=True)

    impact_parser = subparsers.add_parser("impact")
    impact_parser.add_argument("--changed-files", type=Path, required=True)
    impact_parser.add_argument("--policy", type=Path, required=True)
    impact_parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    impact_parser.add_argument("--bundle", type=Path)
    impact_parser.add_argument("--output", type=Path, required=True)

    digest_parser = subparsers.add_parser("digest")
    digest_parser.add_argument("--policy", type=Path, required=True)
    digest_parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    digest_parser.add_argument("--output", type=Path, required=True)

    args = parser.parse_args()
    try:
        if args.command == "evaluate":
            report = evaluate_bundle(args.bundle, args.policy)
            _write_report(args.output, report)
        elif args.command == "matrix":
            report = run_matrix(args.matrix, args.output)
        elif args.command == "impact":
            report = evaluate_impact(
                args.changed_files,
                args.policy,
                repo_root=args.repo_root,
                bundle_path=args.bundle,
            )
            _write_report(args.output, report)
        else:
            policy, policy_sha = load_policy(args.policy)
            report = {
                "schema_version": SCHEMA_VERSION,
                "policy_sha256": policy_sha,
                "source_digest": protected_source_digest(args.repo_root, policy),
                "release_decision": {"status": "PASS", "reasons": []},
            }
            _write_report(args.output, report)
    except ReleaseGateError as exc:
        parser.error(str(exc))
    print(json.dumps(report["release_decision"], sort_keys=True))
    return (
        0
        if report["release_decision"]["status"] in {"PASS", "PROMOTE", "NOT_REQUIRED"}
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
