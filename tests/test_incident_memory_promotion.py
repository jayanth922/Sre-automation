#!/usr/bin/env python3
"""#72: a verified, approval-gated remediation must write incident memory.

The regression these guard: the skill was promoted from inside the ACT phase
while incident memory was promoted only at the end of `_run_graph_impl`. Every
mutating plan is approval-gated, so that end-of-run pass never saw a live
success -- it re-derived `dry_run` and skipped the write. `sre_skills_v1`
filled up normally while `sre_incidents_v2` stayed empty, globally, and the
no_memory ablation arm's recall half was therefore inert in every arm.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sre_agent import graph_builder  # noqa: E402
from sre_agent.verified_learning import assess_learning_eligibility  # noqa: E402


def _promote_incident_memory(*args, **kwargs):
    """Resolved at call time, not import time, so this module still imports
    against a build without the fix -- the wiring tests below have to fail on
    their own assertions, which is the actual demonstration of #72, rather
    than on a collection error that proves only that a name is new."""
    return graph_builder._promote_incident_memory(*args, **kwargs)

_COMMAND = "kubectl rollout restart deployment/inventory-service -n meridian"


class _RecordingMemory:
    def __init__(self, available: bool = True, result: bool = True):
        self._available = available
        self._result = result
        self.calls: list[dict] = []

    def is_available(self) -> bool:
        return self._available

    def store_incident(self, incident_id, **kwargs):
        self.calls.append({"incident_id": incident_id, **kwargs})
        return self._result


class _Ablation:
    def __init__(self, writes: bool):
        self.writes_learned_memory = writes
        self.reads_learned_memory = writes


def _live_results():
    return [
        {
            "status": "EXECUTED",
            "action_type": "restart",
            "target": "inventory-service",
            "command": _COMMAND,
        }
    ]


def _eligibility(**over):
    kwargs = dict(
        act_report={"plan_present": True, "aggregate_decision": "approval_required"},
        verification_outcome={"status": "RESOLVED"},
        live_results=_live_results(),
        executed=[{"action_type": "restart", "target": "inventory-service"}],
        human_approved=True,
    )
    kwargs.update(over)
    return assess_learning_eligibility(**kwargs).to_dict()


def _state():
    return {
        "alert_context": {
            "alert_name": "InventorySlowQueries",
            "labels": {"service": "inventory-service"},
        },
        "reflector_analysis": {
            "hypothesis": "Connection pool exhausted on inventory-service"
        },
        "incident_id": "inc-72",
        "metadata": {"organization_id": "org-1", "cluster_id": "cl-1"},
    }


def _payload():
    return {
        "severity": "SEV2",
        "summary": "1/1 held for approval",
        "verification": {"status": "RESOLVED", "detail": "p99 back under SLO"},
        "live_results": _live_results(),
        "executed": [{"action_type": "restart", "target": "inventory-service"}],
        "recorded_skill": {"skill_id": "skill-org-1-cl-1-latency-inventory-service"},
    }


@pytest.fixture(autouse=True)
def _production_ablation(monkeypatch):
    monkeypatch.setattr(graph_builder, "current_ablation", lambda: _Ablation(True))


def test_approval_gated_verified_success_is_eligible():
    """Guards the premise: this shape is what act_phase records a skill on."""
    assert _eligibility()["eligible_for_success"] is True


def test_verified_remediation_writes_incident_memory():
    memory = _RecordingMemory()
    stored = _promote_incident_memory(
        _state(),
        _payload(),
        eligibility=_eligibility(),
        incident_id="inc-72",
        metadata=_state()["metadata"],
        memory=memory,
    )

    assert stored is True
    assert len(memory.calls) == 1, "a verified success must write exactly one row"
    call = memory.calls[0]
    assert call["incident_id"] == "inc-72"
    assert call["organization_id"] == "org-1"
    assert call["cluster_id"] == "cl-1"
    assert "InventorySlowQueries" in call["symptoms"]
    assert "Connection pool exhausted" in call["root_cause"]
    assert _COMMAND in call["resolution"], "what actually ran must be recallable"


def test_stored_row_carries_verified_provenance_and_the_skill_crosslink():
    memory = _RecordingMemory()
    _promote_incident_memory(
        _state(),
        _payload(),
        eligibility=_eligibility(),
        incident_id="inc-72",
        metadata=_state()["metadata"],
        memory=memory,
    )
    md = memory.calls[0]["metadata"]
    assert md["learning_outcome"] == "verified_success"
    assert md["verification_status"] == "RESOLVED"
    assert md["incident_id"] == "inc-72"
    # The skill and the memory row come from one success; keep them reconcilable.
    assert md["skill_id"] == "skill-org-1-cl-1-latency-inventory-service"


def test_skill_and_incident_memory_cannot_disagree():
    """#72 in one assertion: whenever the outcome is the one act_phase records
    a skill on, incident memory must be written too."""
    eligibility = _eligibility()
    assert eligibility["eligible_for_success"] is True  # act_phase records a skill

    memory = _RecordingMemory()
    assert (
        _promote_incident_memory(
            _state(),
            _payload(),
            eligibility=eligibility,
            incident_id="inc-72",
            metadata=_state()["metadata"],
            memory=memory,
        )
        is True
    )
    assert memory.calls, "skill written but incident memory skipped -- this is #72"


def test_dry_run_writes_nothing():
    memory = _RecordingMemory()
    eligibility = _eligibility(live_results=[], verification_outcome=None)
    assert eligibility["eligible_for_success"] is False

    stored = _promote_incident_memory(
        _state(),
        {**_payload(), "live_results": [], "verification": None},
        eligibility=eligibility,
        incident_id="inc-72",
        metadata=_state()["metadata"],
        memory=memory,
    )
    assert stored is False
    assert memory.calls == []


def test_ablation_freeze_blocks_the_write(monkeypatch):
    """Arms run sequentially against one cluster; a write here would hand the
    next arm a corpus this one never had."""
    monkeypatch.setattr(graph_builder, "current_ablation", lambda: _Ablation(False))
    memory = _RecordingMemory()
    stored = _promote_incident_memory(
        _state(),
        _payload(),
        eligibility=_eligibility(),
        incident_id="inc-72",
        metadata=_state()["metadata"],
        memory=memory,
    )
    assert stored is False
    assert memory.calls == []


def test_unavailable_store_reports_false_rather_than_pretending():
    memory = _RecordingMemory(available=False)
    stored = _promote_incident_memory(
        _state(),
        _payload(),
        eligibility=_eligibility(),
        incident_id="inc-72",
        metadata=_state()["metadata"],
        memory=memory,
    )
    assert stored is False
    assert memory.calls == []


def test_missing_incident_id_is_refused_not_raised():
    memory = _RecordingMemory()
    for bad in (None, "", "   "):
        assert (
            _promote_incident_memory(
                _state(),
                _payload(),
                eligibility=_eligibility(),
                incident_id=bad,
                metadata=_state()["metadata"],
                memory=memory,
            )
            is False
        )
    assert memory.calls == []


def test_a_failed_write_is_reported_as_failure():
    memory = _RecordingMemory(result=False)
    stored = _promote_incident_memory(
        _state(),
        _payload(),
        eligibility=_eligibility(),
        incident_id="inc-72",
        metadata=_state()["metadata"],
        memory=memory,
    )
    assert stored is False
    assert len(memory.calls) == 1


# ── the wiring, not just the helper ──────────────────────────────────────────
# The tests above pin the promotion function's behaviour. These two run the
# real `_act_gate_node` and assert it actually calls the thing: #72 was not a
# broken function, it was a function that was never reached from the ACT path.

_runtime = pytest.importorskip("sre_agent.agent_state", reason="full runtime stack")
from sre_agent.agent_state import (  # noqa: E402
    AlertContext,
    RemediationAction,
    RemediationPlan,
)

try:
    from sre_agent.graph_builder import _act_gate_node  # noqa: E402
except Exception as exc:  # pragma: no cover - env-dependent
    pytest.skip(f"full runtime stack unavailable: {exc}", allow_module_level=True)

import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_generated_runbooks(monkeypatch, tmp_path):
    monkeypatch.setenv("RUNBOOKS_DIR", str(tmp_path))


def _calm_alert():
    return AlertContext(
        alert_name="InventorySlowQueries",
        severity="warning",
        labels={
            "service": "inventory-service",
            "namespace": "demo-app",
            "error_rate": "0.02",
            "slo_burn_rate": "0.5",
            "saturation": "0.1",
            "affected_services": "1",
            "affected_pods": "1",
            "dependency_count": "0",
            "duration_seconds": "60",
            "customer_scope": "single",
            "slo_breached": "false",
            "still_escalating": "false",
            "error_rate_slope": "0",
        },
        annotations={},
    )


def _autonomous_state(metadata):
    plan = RemediationPlan(
        plan_id="p-72",
        hypothesis="connection pool exhausted",
        actions=[
            RemediationAction(
                action_type="restart",
                target="inventory-service",
                parameters={"namespace": "demo-app"},
                safety_check="ok",
                rollback_plan=None,
            )
        ],
        estimated_duration="2m",
        risk_level="low",
        requires_approval=False,
        verification_metrics=["error_rate"],
    )
    return {
        "alert_context": _calm_alert(),
        "remediation_plan": plan,
        "agent_results": {"metrics_agent": "ok"},
        "reflector_analysis": {
            "confidence": 0.9,
            "confidence_calibrated": True,
            "hypothesis": "Connection pool exhausted on inventory-service",
        },
        "incident_id": "inc-72-wiring",
        "metadata": metadata,
    }


def _verified_success_skill_learning(monkeypatch):
    """Make the skill phase report exactly what it reports on a verified,
    approval-gated success -- a recorded skill and an eligible verdict."""
    import sre_agent.act_phase
    from sre_agent.confidence_calibration import CalibratedConfidence

    def fake_calibrated(raw, task, *args, **kwargs):
        return CalibratedConfidence(
            task="diagnosis" if task == "hypothesis" else "remediation",
            raw_confidence=0.9 if raw is None else raw,
            calibrated_probability=0.99,
            artifact_version="mock",
            artifact_sha256="mock",
            autonomy_threshold=0.8,
        )

    monkeypatch.setattr(sre_agent.act_phase, "_configured_confidence", fake_calibrated)
    monkeypatch.setattr(
        sre_agent.act_phase,
        "_configured_remediation_confidence",
        lambda *a, **k: (0.9, fake_calibrated(0.9, "remediation")),
    )
    monkeypatch.setattr(
        sre_agent.act_phase,
        "apply_skill_learning",
        lambda *a, **k: {
            "recorded_skill": {"skill_id": "skill-72", "name": "restart-inventory"},
            "learning_eligibility": _eligibility(),
        },
    )


def test_act_node_writes_incident_memory_when_it_records_a_skill(monkeypatch):
    """#72: the node recorded the skill and wrote nothing to incident memory.

    Fails before the fix -- `_act_gate_node` had the verdict in hand and never
    reached a memory write, which is why `sre_incidents_v2` held zero points
    while `sre_skills_v1` filled up.
    """
    import sre_agent.memory_store as memory_store

    _verified_success_skill_learning(monkeypatch)
    memory = _RecordingMemory()
    monkeypatch.setattr(memory_store, "get_memory_store", lambda: memory)

    state = _autonomous_state({"organization_id": "org-1", "cluster_id": "cl-1"})
    report = asyncio.run(_act_gate_node(state))["metadata"]["act_report"]

    assert report["recorded_skill"] is not None, "premise: the skill was recorded"
    assert report.get("stored_incident_memory") is True, (
        "a run that records a skill must also write incident memory -- this is #72"
    )
    assert len(memory.calls) == 1
    assert memory.calls[0]["incident_id"] == "inc-72-wiring"
    assert memory.calls[0]["organization_id"] == "org-1"
    assert memory.calls[0]["cluster_id"] == "cl-1"


def test_scope_falls_back_to_the_execution_context(monkeypatch):
    """An unscoped row is stored and then never found: recall is tenant-filtered.
    Resumed and benchmark runs rebuild state without these metadata keys."""
    import sre_agent.memory_store as memory_store

    _verified_success_skill_learning(monkeypatch)
    memory = _RecordingMemory()
    monkeypatch.setattr(memory_store, "get_memory_store", lambda: memory)

    async def unresolved(**_kwargs):
        return False

    from sre_agent import approval_flow

    monkeypatch.setattr(approval_flow, "incident_is_resolved", unresolved)

    state = _autonomous_state({})  # no organization_id / cluster_id
    context = SimpleNamespace(
        organization_id="org-ctx", cluster_id="cl-ctx", environment="production"
    )
    asyncio.run(_act_gate_node(state, context))

    assert len(memory.calls) == 1
    assert memory.calls[0]["organization_id"] == "org-ctx"
    assert memory.calls[0]["cluster_id"] == "cl-ctx"
