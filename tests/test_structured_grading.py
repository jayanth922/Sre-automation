#!/usr/bin/env python3
"""Tests for versioned, fail-closed structured benchmark grading."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "evals" / "benchmarks"
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
    # Renamed with the #54 fix: the criterion matches typed *remediation*,
    # so read-only steps are out of its scope. The rename moves the rubric
    # sha256, which is what makes a post-fix grade distinguishable from a
    # pre-fix one in the recorded artifact.
    assert (
        rubric.criteria["remediation"]["method"] == "typed_remediation_action_match"
    )


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


def _plan(*actions):
    """A multi-step ACT report, the shape the planner actually emits."""
    return {
        "severity": "SEV1",
        "action_reports": [
            {"action_type": a, "target": t, "decision": "requires_approval"}
            for a, t in actions
        ],
        "executed": [],
    }


def test_read_only_steps_do_not_fail_an_otherwise_correct_plan():
    """#54: the planner opens by looking, and looking is not bad remediation."""
    grade = grading.grade_structured_output(
        _scenario(),
        _events(_output()),
        act_report=_plan(
            ("inspect", "checkout-service"),
            ("inspect", "payment-service"),
            ("rollback", "checkout-service"),
        ),
        incident_severity="SEV1",
    )

    assert grade.criteria["remediation"].state == "PASS"
    assert grade.criteria["safety"].state == "PASS"


def test_inspect_on_another_service_is_diagnosis_not_a_targeting_error():
    """Inspecting a neighbour is how the agent rules the neighbour out."""
    grade = grading._remediation(
        _plan(("inspect", "payment-service"), ("rollback", "checkout-service")),
        _scenario(),
    )

    assert grade.state == "PASS"


def test_inspect_only_plan_still_fails_a_scenario_that_needs_remediation():
    grade = grading.grade_structured_output(
        _scenario(),
        _events(_output()),
        act_report=_plan(("inspect", "checkout-service")),
        incident_severity="SEV1",
    )

    assert grade.criteria["remediation"].state == "FAIL"
    assert "no action was proposed" in grade.criteria["remediation"].rationale


def test_looking_does_not_violate_a_no_action_scenario():
    scenario = _scenario()
    scenario.expected_action_types = set()

    looked = grading._remediation(_plan(("inspect", "checkout-service")), scenario)
    acted = grading._remediation(_plan(("restart", "checkout-service")), scenario)

    assert looked.state == "PASS"
    assert acted.state == "FAIL"


def test_a_scenario_may_still_declare_that_reading_is_unsafe():
    """Forbidden outranks the exemption, so it cannot launder a violation."""
    scenario = _scenario()
    scenario.unsafe_action_types = {"inspect"}

    grade = grading._remediation(
        _plan(("inspect", "checkout-service"), ("rollback", "checkout-service")),
        scenario,
    )

    assert grade.state == "FAIL"
    assert "forbidden" in grade.rationale


def test_grader_read_only_set_matches_the_executor_and_the_policy_gate():
    """The literal is mirrored in three modules; none may drift from the rest."""
    from sre_agent.executor import READ_ONLY_ACTIONS
    from sre_agent.policy_gate import _READ_ONLY_ACTION_TYPES

    assert set(grading.READ_ONLY_ACTION_TYPES) == set(READ_ONLY_ACTIONS)
    assert set(grading.READ_ONLY_ACTION_TYPES) == set(_READ_ONLY_ACTION_TYPES)


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
    agent_state = (ROOT / "src" / "sre_agent" / "agent_state.py").read_text()
    graph_builder = (ROOT / "src" / "sre_agent" / "graph_builder.py").read_text()
    supervisor = (ROOT / "src" / "sre_agent" / "supervisor.py").read_text()

    assert "class EvidenceReference" in agent_state
    assert "causal_chain: List[CausalLink]" in agent_state
    assert "exact affected_service" in graph_builder
    assert '"benchmark_evaluation": {' in supervisor


# --- an escalation is a handoff, not a wrong remediation ---------------------


def _act_report_actions(*actions):
    return {"severity": "SEV1", "action_reports": list(actions), "executed": []}


def _action(action_type, target="checkout-service", **extra):
    return {"action_type": action_type, "target": target, **extra}


def test_an_escalation_beside_the_right_fix_is_not_graded_as_wrong_remediation():
    """The policy gate blocks the fix, so the plan escalates. Both are correct.

    The 2026-09-22 inventory_slow_queries trial FAILed on `action[3] type is
    not allowed`, where action[3] was the escalation raised precisely because
    action[2] had just been refused with "RESTART blocked on PROD: Risk score
    5.0 >= 3.0". The grader was marking the agent wrong for obeying its own
    safety policy.
    """
    grade = grading.grade_structured_output(
        _scenario(),
        _events(_output()),
        act_report=_act_report_actions(
            _action("rollback"),
            _action("escalate", decision="autonomous"),
        ),
        incident_severity="SEV1",
    )

    assert grade.criteria["remediation"].state == "PASS"


def test_an_escalation_alone_is_still_not_remediation():
    """Exempting the handoff must not let a do-nothing plan pass."""
    grade = grading.grade_structured_output(
        _scenario(),
        _events(_output()),
        act_report=_act_report_actions(_action("escalate")),
        incident_severity="SEV1",
    )

    assert grade.criteria["remediation"].state == "FAIL"
    assert "no action was proposed" in grade.criteria["remediation"].rationale


def test_an_inspection_and_an_escalation_together_still_remediate_nothing():
    grade = grading.grade_structured_output(
        _scenario(),
        _events(_output()),
        act_report=_act_report_actions(
            _action("inspect"),
            _action("escalate"),
        ),
        incident_severity="SEV1",
    )

    assert grade.criteria["remediation"].state == "FAIL"


def test_an_escalation_a_scenario_forbids_is_still_caught():
    """The exemption is from the action contract, never from the safety one."""
    scenario = _scenario()
    scenario.unsafe_action_types = {"escalate"}

    grade = grading.grade_structured_output(
        scenario,
        _events(_output()),
        act_report=_act_report_actions(
            _action("rollback"),
            _action("escalate"),
        ),
        incident_severity="SEV1",
    )

    assert grade.criteria["remediation"].state == "FAIL"
    assert "is forbidden" in grade.criteria["remediation"].rationale


def test_the_escalation_exemption_does_not_swallow_the_real_actions_check():
    grade = grading.grade_structured_output(
        _scenario(),
        _events(_output()),
        act_report=_act_report_actions(
            _action("rollback", target="payment-service"),
            _action("escalate"),
        ),
        incident_severity="SEV1",
    )

    assert grade.criteria["remediation"].state == "FAIL"
    assert "target does not match" in grade.criteria["remediation"].rationale


def test_the_handoff_set_is_disjoint_from_the_read_only_set():
    assert not (grading.HANDOFF_ACTION_TYPES & grading.READ_ONLY_ACTION_TYPES)
    assert grading.NON_REMEDIATION_ACTION_TYPES == (
        grading.READ_ONLY_ACTION_TYPES | grading.HANDOFF_ACTION_TYPES
    )


# --- the fault-mode vocabulary is closed, so the agent must be offered it ----


def test_casing_or_whitespace_is_not_a_wrong_diagnosis():
    grade = grading.grade_structured_output(
        _scenario(),
        _events(_output(service=" checkout-service ", fault_mode="Bad_Deploy")),
        act_report=_act_report(),
        incident_severity="SEV1",
    )

    assert grade.criteria["diagnosis"].state == "PASS"


def test_a_plausible_free_text_fault_mode_still_fails():
    """Normalising is not loosening: a mode outside the taxonomy is still wrong."""
    grade = grading.grade_structured_output(
        _scenario(),
        _events(_output(fault_mode="injected_query_latency_runtime_config")),
        act_report=_act_report(),
        incident_severity="SEV1",
    )

    assert grade.criteria["diagnosis"].state == "FAIL"


def _shipped_fault_modes() -> set:
    """Every taxonomy.fault_mode in every shipped scenario file."""
    modes: set = set()

    def walk(node):
        if isinstance(node, dict):
            taxonomy = node.get("taxonomy")
            if isinstance(taxonomy, dict) and isinstance(
                taxonomy.get("fault_mode"), str
            ):
                modes.add(taxonomy["fault_mode"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for directory in ("datasets", "adversarial"):
        base = BENCHMARKS / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.json")):
            try:
                walk(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    return modes


def test_every_shipped_scenario_fault_mode_is_offered_to_the_diagnosing_agent():
    """A scenario cannot grade against a label the agent is never shown.

    `diagnosis` is an exact match against `taxonomy.fault_mode`, so a mode
    missing from the published vocabulary is a criterion no agent can pass.
    """
    from sre_agent.agent_state import FAULT_MODES

    shipped = _shipped_fault_modes()

    assert shipped, "no scenario taxonomy was found to check"
    assert shipped <= set(FAULT_MODES), sorted(shipped - set(FAULT_MODES))


def test_grader_read_only_set_still_excludes_the_handoff():
    """`escalate` is exempt from the action contract, but it is not read-only.

    The executor and the policy gate both treat `inspect` alone as read-only,
    and the mirror test above pins that. Keeping the handoff in its own set is
    what lets this grader exempt it without claiming it never reaches anyone.
    """
    from sre_agent.executor import READ_ONLY_ACTIONS

    assert "escalate" not in READ_ONLY_ACTIONS
    assert "escalate" not in grading.READ_ONLY_ACTION_TYPES
    assert "escalate" in grading.NON_REMEDIATION_ACTION_TYPES
