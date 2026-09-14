#!/usr/bin/env python3
"""Tests for A10 verified-only learning gates."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_VL = ROOT / "sre_agent" / "verified_learning.py"
_spec = importlib.util.spec_from_file_location("verified_learning", _VL)
vl = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = vl
_spec.loader.exec_module(vl)

_SS = ROOT / "sre_agent" / "skill_store.py"
_ss_spec = importlib.util.spec_from_file_location("skill_store_a10", _SS)
skill_store = importlib.util.module_from_spec(_ss_spec)
sys.modules[_ss_spec.name] = skill_store
_ss_spec.loader.exec_module(skill_store)

_AP = ROOT / "sre_agent" / "act_phase.py"
# act_phase has package-relative imports; load via package path.
sys.path.insert(0, str(ROOT))
from sre_agent.act_phase import ActReport, apply_skill_learning  # noqa: E402


def _alert():
    return {
        "alert_name": "CheckoutHighErrorRate",
        "labels": {"service": "checkout-service"},
    }


def test_dry_run_and_missing_verification_cannot_promote_success():
    eligibility = vl.assess_learning_eligibility(
        act_report={"plan_present": True, "aggregate_decision": "autonomous"},
        executed=[{"action_type": "restart", "target": "checkout-service"}],
    )
    assert eligibility.eligible_for_success is False
    assert eligibility.outcome_class == "dry_run"


def test_failed_and_unknown_verification_are_not_successful_exemplars():
    failed = vl.assess_learning_eligibility(
        verification_outcome={"status": "FAILED"},
        live_results=[{"status": "EXECUTED", "action_type": "restart"}],
        act_report={"plan_present": True, "aggregate_decision": "autonomous"},
    )
    unknown = vl.assess_learning_eligibility(
        verification_outcome={"status": "UNKNOWN"},
        live_results=[{"status": "EXECUTED", "action_type": "restart"}],
        act_report={"plan_present": True, "aggregate_decision": "autonomous"},
    )
    assert failed.outcome_class == "failed"
    assert unknown.outcome_class == "unknown"
    assert failed.eligible_for_success is False
    assert unknown.eligible_for_success is False


def test_blocked_plan_cannot_promote_success():
    eligibility = vl.assess_learning_eligibility(
        act_report={"plan_present": True, "aggregate_decision": "blocked"},
        verification_outcome={"status": "RESOLVED"},
        live_results=[{"status": "EXECUTED", "action_type": "restart"}],
    )
    assert eligibility.eligible_for_success is False
    assert eligibility.outcome_class == "blocked"


def test_verified_live_execution_is_eligible():
    eligibility = vl.assess_learning_eligibility(
        act_report={"plan_present": True, "aggregate_decision": "autonomous"},
        verification_outcome={"status": "RESOLVED"},
        live_results=[
            {
                "status": "EXECUTED",
                "action_type": "rollback",
                "target": "checkout-service",
            }
        ],
    )
    assert eligibility.eligible_for_success is True
    assert eligibility.outcome_class == "verified_success"


def test_a_successful_page_is_not_a_successful_fix():
    """`escalate` executes by paging a human — it mutates nothing.

    Counting it as a live execution would let "escalate" be promoted as a
    verified remediation the agent should reach for again, on an incident a
    human actually fixed.
    """
    eligibility = vl.assess_learning_eligibility(
        act_report={"plan_present": True, "aggregate_decision": "autonomous"},
        verification_outcome={"status": "RESOLVED"},
        live_results=[
            {
                "status": "EXECUTED",
                "action_type": "escalate",
                "target": "checkout-service",
            }
        ],
    )
    assert eligibility.live_executed_count == 0
    assert eligibility.eligible_for_success is False


def test_memory_metadata_requires_verified_success():
    eligibility = vl.assess_learning_eligibility(
        verification_outcome={"status": "FAILED"},
        live_results=[{"status": "EXECUTED"}],
        act_report={"plan_present": True, "aggregate_decision": "autonomous"},
    )
    with pytest.raises(vl.VerifiedLearningError):
        vl.memory_metadata_for_promotion(
            eligibility=eligibility,
            provenance=vl.LearningProvenance(
                incident_id="inc-1",
                verification_status="FAILED",
                outcome_class="failed",
                artifact_kind="memory",
            ),
        )


def test_apply_skill_learning_records_only_verified_live_actions():
    store = skill_store.InMemorySkillStore()
    state = {"alert_context": _alert(), "incident_id": "inc-1"}
    report = ActReport(
        severity="SEV3",
        severity_rationale="test",
        plan_present=True,
        aggregate_decision="autonomous",
        executed=[{"action_type": "rollback", "target": "checkout-service"}],
        summary="dry-run only",
    )

    dry = apply_skill_learning(state, report, store=store)
    assert dry["recorded_skill"] is None
    assert dry["negative_exemplar"] is not None
    assert dry["learning_eligibility"]["outcome_class"] == "dry_run"

    live = apply_skill_learning(
        state,
        report,
        store=store,
        verification_outcome={"status": "RESOLVED"},
        live_results=[
            {
                "status": "EXECUTED",
                "action_type": "rollback",
                "target": "checkout-service",
            }
        ],
    )
    assert live["recorded_skill"] is not None
    assert live["recorded_skill"]["verification_status"] == "RESOLVED"
    assert len(store.all()) == 1


def test_invalidated_skills_are_not_proposed():
    store = skill_store.InMemorySkillStore()
    skill = skill_store.skill_from_remediation(
        _alert(),
        [{"action_type": "rollback", "target": "checkout-service"}],
        "inc-1",
        verification_status="RESOLVED",
    )
    store.add(skill)
    store.invalidate(
        skill.skill_id, reason="verification reversed", evidence={"status": "FAILED"}
    )
    proposed = skill_store.propose_skills(store, _alert())
    assert proposed == []


# --- The incident-status cross-check -----------------------------------------
# The gate below is the one that made the whole positive-learning path dead in
# production: it demanded the incident row read RESOLVED, a status the graph
# never computes. See _VERIFIED_INCIDENT_STATUSES in verified_learning.py.


def _resolved_autonomous_report():
    return {
        "plan_present": True,
        "aggregate_decision": "autonomous",
        "live_results": [
            {
                "status": "EXECUTED",
                "action_type": "patch_deployment_env",
                "target": "inventory-service",
            }
        ],
    }


def test_a_verified_fix_awaiting_acknowledgment_can_still_be_promoted():
    """PENDING_ACKNOWLEDGMENT is what a verified autonomous fix looks like the
    moment the run ends — the human's acknowledgement comes later, long after
    the learning step has had its only chance to run."""
    eligibility = vl.assess_learning_eligibility(
        act_report=_resolved_autonomous_report(),
        verification_outcome={"status": "RESOLVED"},
        incident_status="pending_acknowledgment",
    )
    assert eligibility.eligible_for_success
    assert eligibility.outcome_class == "verified_success"


def test_an_incident_status_that_contradicts_the_oracle_still_blocks():
    eligibility = vl.assess_learning_eligibility(
        act_report=_resolved_autonomous_report(),
        verification_outcome={"status": "RESOLVED"},
        incident_status="remediation_failed",
    )
    assert not eligibility.eligible_for_success
    assert eligibility.outcome_class == "incomplete"


def test_the_status_the_graph_really_computes_does_not_block_promotion():
    """Wires the two modules together instead of restating a status string.

    `graph_builder` computes the incident status with
    `incident_status.compute_incident_status` and hands the result straight to
    this gate. Whatever that function returns for a verified fix has to be
    promotable, or the self-improving loop can only ever learn from failures.
    """
    from sre_agent.incident_status import compute_incident_status

    report = _resolved_autonomous_report()
    verification = {"status": "RESOLVED"}
    computed = compute_incident_status(
        state={}, report_payload=report, verification_outcome=verification
    )

    eligibility = vl.assess_learning_eligibility(
        act_report=report,
        verification_outcome=verification,
        incident_status=computed,
    )
    assert eligibility.eligible_for_success, (
        f"compute_incident_status returns {computed} for a verified fix, which "
        "assess_learning_eligibility rejects; no live run can ever record a "
        "successful exemplar"
    )


def test_a_verified_run_records_a_skill_rather_than_a_negative_exemplar():
    """The same thing again through `apply_skill_learning`, the caller that
    actually decides between `record_successful_remediation` and
    `build_negative_exemplar`."""
    store = skill_store.InMemorySkillStore()
    state = {"alert_context": _alert(), "incident_id": "inc-ack", "metadata": {}}
    report = ActReport(
        severity="SEV3",
        severity_rationale="test",
        plan_present=True,
        aggregate_decision="autonomous",
        executed=[],
        summary="fault injection disabled",
    )

    result = apply_skill_learning(
        state,
        report,
        store=store,
        verification_outcome={"status": "RESOLVED"},
        incident_status="pending_acknowledgment",
        live_results=[
            {
                "status": "EXECUTED",
                "action_type": "patch_deployment_env",
                "target": "inventory-service",
            }
        ],
    )

    assert result["recorded_skill"] is not None
    assert result["negative_exemplar"] is None
    assert result["learning_eligibility"]["outcome_class"] == "verified_success"


# --- "Blocked, but a human said yes" -----------------------------------------
# A plan aggregates to `blocked` when *any single action* is blocked by policy,
# so a five-action plan with one policy-blocked rollback is `blocked` even
# though a human approved it and the remaining actions fixed the incident. The
# escape hatch for that case read the approval off the ActReport, which has no
# `approval` field — it was dead, and every such run was graded `blocked`.


def _blocked_plan_with_one_verified_fix():
    return {
        "plan_present": True,
        "aggregate_decision": "blocked",
        "live_results": [
            {
                "status": "EXECUTED",
                "action_type": "config_change",
                "target": "payment-service",
            }
        ],
    }


def test_a_human_approved_blocked_plan_that_worked_is_a_verified_success():
    eligibility = vl.assess_learning_eligibility(
        act_report=_blocked_plan_with_one_verified_fix(),
        verification_outcome={"status": "RESOLVED"},
        incident_status="pending_acknowledgment",
        human_approved=True,
    )
    assert eligibility.eligible_for_success
    assert eligibility.outcome_class == "verified_success"


def test_a_blocked_plan_nobody_approved_is_still_blocked():
    eligibility = vl.assess_learning_eligibility(
        act_report=_blocked_plan_with_one_verified_fix(),
        verification_outcome={"status": "RESOLVED"},
        incident_status="pending_acknowledgment",
        human_approved=False,
    )
    assert not eligibility.eligible_for_success
    assert eligibility.outcome_class == "blocked"


def test_the_approval_the_graph_verified_is_what_reaches_the_gate():
    """The ActReport the graph hands to learning carries no approval.

    `graph_builder` attaches the approval to `report.to_dict()` and then calls
    `apply_skill_learning` with the ActReport *object*. Passing the dataclass
    through the gate must therefore not be how the approval is discovered, or
    the fact is lost exactly when it matters.
    """
    report = ActReport(
        severity="UNKNOWN",
        severity_rationale="no measured telemetry",
        plan_present=True,
        aggregate_decision="blocked",
        executed=[],
        summary="1 blocked by policy, 3 held for approval",
    )
    assert not hasattr(report, "approval"), (
        "if ActReport gains an approval field, the gate may read it directly "
        "and this plumbing can be simplified"
    )

    store = skill_store.InMemorySkillStore()
    state = {
        "alert_context": _alert(),
        "incident_id": "inc-blocked-approved",
        "metadata": {},
    }
    live = [
        {
            "status": "EXECUTED",
            "action_type": "config_change",
            "target": "payment-service",
        }
    ]

    lost = apply_skill_learning(
        state, report, store=store,
        verification_outcome={"status": "RESOLVED"},
        incident_status="pending_acknowledgment",
        live_results=live,
    )
    assert lost["learning_eligibility"]["outcome_class"] == "blocked"

    passed = apply_skill_learning(
        state, report, store=skill_store.InMemorySkillStore(),
        verification_outcome={"status": "RESOLVED"},
        incident_status="pending_acknowledgment",
        live_results=live,
        human_approved=True,
    )
    assert passed["recorded_skill"] is not None
    assert passed["negative_exemplar"] is None
    assert passed["learning_eligibility"]["outcome_class"] == "verified_success"


def test_the_durable_approval_record_is_the_fallback():
    """With no explicit boolean, `state["metadata"]["approval"]` is consulted —
    the record the approval flow actually persists."""
    report = ActReport(
        severity="UNKNOWN",
        severity_rationale="no measured telemetry",
        plan_present=True,
        aggregate_decision="blocked",
        executed=[],
        summary="blocked plan",
    )
    state = {
        "alert_context": _alert(),
        "incident_id": "inc-metadata-approval",
        "metadata": {"approval": {"status": "approved", "action_hash": "abc"}},
    }

    result = apply_skill_learning(
        state, report, store=skill_store.InMemorySkillStore(),
        verification_outcome={"status": "RESOLVED"},
        incident_status="pending_acknowledgment",
        live_results=[
            {
                "status": "EXECUTED",
                "action_type": "config_change",
                "target": "payment-service",
            }
        ],
    )
    assert result["learning_eligibility"]["outcome_class"] == "verified_success"
