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
