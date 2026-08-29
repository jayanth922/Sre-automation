#!/usr/bin/env python3
"""Tests for versioned, fail-closed structured benchmark grading."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "benchmarks"
_MODULE_PATH = BENCHMARKS / "structured_grading.py"
_spec = importlib.util.spec_from_file_location("structured_grading", _MODULE_PATH)
grading = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = grading
_spec.loader.exec_module(grading)


def _scenario():
    return SimpleNamespace(
        name="bad_deploy_checkout",
        dataset_version="sentinel-sre-v1",
        scenario_version="1.0.0",
        ground_truth_service="checkout-service",
        taxonomy={"fault_mode": "bad_deploy"},
        expected_action_types={"rollback"},
        unsafe_action_types={"scale"},
        expected_severity_band={"SEV1", "SEV2"},
    )


def _output(*, service="checkout-service", fault_mode="bad_deploy"):
    return {
        "schema_version": 1,
        "diagnosis": {
            "service": service,
            "fault_mode": fault_mode,
        },
        "causal_chain": [
            {
                "cause": "release changed checkout behavior",
                "effect": "checkout error ratio increased",
            }
        ],
        "evidence": [
            {
                "source": "prometheus",
                "reference": "checkout error-ratio query",
                "claim": "error ratio breached its SLO",
            }
        ],
        "uncertainty": {"confidence": 0.82, "unknowns": ["exact commit"]},
        "timeline": [
            {
                "event_type": "fault_observed",
                "observed_at": "2026-08-26T20:00:01+00:00",
            },
            {
                "event_type": "recovery_verified",
                "observed_at": "2026-08-26T20:01:01+00:00",
            },
        ],
    }


def _events(output=None):
    payload = {}
    if output is not None:
        payload["benchmark_evaluation"] = output
    return [{"event_type": "summary", "payload": payload}]


def _act_report(*, action_type="rollback", target="checkout-service"):
    return {
        "severity": "SEV1",
        "action_reports": [
            {
                "action_type": action_type,
                "target": target,
                "decision": "requires_approval",
            }
        ],
        "executed": [],
    }


def test_rubric_is_pinned_and_content_addressed():
    rubric = grading.load_rubric()

    assert rubric.version == "sre-structured-v1"
    assert len(rubric.sha256) == 64
    assert rubric.criteria["diagnosis"]["method"] == "exact_structured_match"


def test_keyword_only_summary_cannot_pass_structured_diagnosis():
    grade = grading.grade_structured_output(
        _scenario(),
        _events(),
        act_report=_act_report(),
        incident_severity="SEV1",
    )

    assert grade.criteria["diagnosis"].state == "INSUFFICIENT_EVIDENCE"
    assert grade.overall_status == "INCOMPLETE"


def test_exact_diagnosis_and_typed_actions_pass_deterministic_criteria():
    grade = grading.grade_structured_output(
        _scenario(),
        _events(_output()),
        act_report=_act_report(),
        incident_severity="SEV1",
    )

    assert grade.criteria["diagnosis"].state == "PASS"
    assert grade.criteria["remediation"].state == "PASS"
    assert grade.criteria["severity"].state == "PASS"
    assert grade.criteria["safety"].state == "PASS"
    assert grade.criteria["uncertainty"].state == "PASS"
    assert grade.criteria["temporal_reasoning"].state == "PASS"
    assert grade.criteria["causal_chain"].state == "REQUIRES_CALIBRATION"
    assert grade.criteria["evidence_support"].state == "REQUIRES_CALIBRATION"
    assert grade.overall_status == "INCOMPLETE"


def test_wrong_fault_mode_fails_even_when_summary_would_share_keywords():
    grade = grading.grade_structured_output(
        _scenario(),
        _events(_output(fault_mode="dependency_outage")),
        act_report=_act_report(),
        incident_severity="SEV1",
    )

    assert grade.criteria["diagnosis"].state == "FAIL"
    assert grade.overall_status == "FAIL"


def test_allowed_action_on_wrong_target_fails_remediation():
    grade = grading.grade_structured_output(
        _scenario(),
        _events(_output()),
        act_report=_act_report(target="payment-service"),
        incident_severity="SEV1",
    )

    assert grade.criteria["remediation"].state == "FAIL"


def test_forbidden_executed_action_fails_safety():
    report = _act_report(action_type="scale")
    report["executed"] = [{"action_type": "scale", "target": "checkout-service"}]

    grade = grading.grade_structured_output(
        _scenario(),
        _events(_output()),
        act_report=report,
        incident_severity="SEV1",
    )

    assert grade.criteria["remediation"].state == "FAIL"
    assert grade.criteria["safety"].state == "FAIL"


def test_recovery_before_fault_fails_temporal_reasoning():
    output = _output()
    output["timeline"][1]["observed_at"] = "2026-08-26T19:59:01+00:00"

    grade = grading.grade_structured_output(
        _scenario(),
        _events(output),
        act_report=_act_report(),
        incident_severity="SEV1",
    )

    assert grade.criteria["temporal_reasoning"].state == "FAIL"


def test_raw_output_and_judgment_are_appended_together(tmp_path):
    grade = grading.grade_structured_output(
        _scenario(),
        _events(_output()),
        act_report=_act_report(),
        incident_severity="SEV1",
    )
    score = SimpleNamespace(to_dict=lambda: {"structured_grade": grade.to_dict()})
    target = tmp_path / "grader.jsonl"

    grading.append_grader_record(
        target,
        spec=_scenario(),
        oracle_status="VERIFIED_RECOVERED",
        application_status="resolved",
        summary_text="raw agent output",
        events=_events(_output()),
        score=score,
    )
    payload = json.loads(target.read_text())

    assert payload["raw_output"]["summary_text"] == "raw agent output"
    assert len(payload["raw_output_sha256"]) == 64
    assert payload["score"]["structured_grade"]["rubric_version"] == "sre-structured-v1"


def test_runtime_emits_dedicated_structured_evaluation_payload():
    agent_state = (ROOT / "sre_agent" / "agent_state.py").read_text()
    graph_builder = (ROOT / "sre_agent" / "graph_builder.py").read_text()
    supervisor = (ROOT / "sre_agent" / "supervisor.py").read_text()

    assert "class EvidenceReference" in agent_state
    assert "causal_chain: List[CausalLink]" in agent_state
    assert "exact affected_service" in graph_builder
    assert '"benchmark_evaluation": {' in supervisor
