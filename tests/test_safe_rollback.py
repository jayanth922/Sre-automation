#!/usr/bin/env python3
"""Safe rollback end to end, offline: bind, approve, execute, refuse drift.

A rollback is bound before approval to the ReplicaSet it restores and the
template it expects to replace, both read from the deployment's history. The
approval shows and hashes both; at execution the gateway re-reads the
deployment and acts only in those two states. The agent half here runs against
the real executor MCP tools (``executor_real/server.py``) over the fake
apiserver from ``test_executor_rollout``, so the command the approver reads and
the call the executor makes are checked against each other, not against a mock.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from test_executor_rollout import (  # noqa: F401  (`server` is a fixture)
    BAD,
    GOOD,
    FakeApps,
    _bad_deploy_history,
    _restart_after_bad_deploy,
    _rs,
    _template,
    rollout,
    server,
)

import sre_agent.mutation_gateway as gateway
from sre_agent.act_phase import (
    build_act_report,
    build_live_action_requests,
    execute_autonomous_live,
    execute_live_action_request,
)
from sre_agent.approval_flow import compute_action_hash, format_approval_request
from sre_agent.execution_context import ExecutionContext
from sre_agent.mutation_gateway import (
    MutationGateContext,
    MutationPreDispatchError,
    MutationRejected,
    authorize_and_execute,
)
from sre_agent.rollback_binding import bind_rollbacks, rollback_command, select_rollback_target

CONTEXT = ExecutionContext(
    organization_id="11111111-1111-1111-1111-111111111111",
    cluster_id="22222222-2222-2222-2222-222222222222",
    namespace="meridian",
    allowlist=("meridian",),
)
ALLOW = lambda a, e, r: (True, "allowed")  # noqa: E731


class Edge:
    """The executor MCP as the agent's tool caller reaches it."""

    def __init__(self, server, api, *, before=None):
        self.server, self.api = server, api
        server._apps_api = lambda: api
        self.before = before or {}
        self.calls = []
        self.responses = []

    async def __call__(self, tool, args):
        self.calls.append((tool, dict(args)))
        hook = self.before.pop(tool, None)
        if hook:
            hook(self.api)
        response = await getattr(self.server, tool)(**args)
        self.responses.append((tool, json.loads(response)))
        return response

    def tools(self):
        return [tool for tool, _ in self.calls]


class FakeStore:
    def __init__(self):
        self.claims = set()

    def is_available(self):
        return True

    def is_cluster_locked(self, cluster_id):
        return False

    def set_idempotency(self, key, ttl):
        if key in self.claims:
            return False
        self.claims.add(key)
        return True


@pytest.fixture
def store(monkeypatch):
    store = FakeStore()
    store.audits = []

    async def persist(context, result, *, approved):
        store.audits.append((result.status, result.detail, approved))

    monkeypatch.setattr(gateway, "get_state_store", lambda: store)
    monkeypatch.setattr(gateway, "_persist_audit_event", persist)
    return store


def _action(action_type="rollback", parameters=None):
    return SimpleNamespace(
        action_type=action_type,
        target="checkout",
        parameters=dict(parameters or {"namespace": "meridian"}),
        rollback_plan="roll forward again",
    )


def _state(*actions):
    return {
        "alert_context": SimpleNamespace(
            alert_name="CheckoutHighErrorRate",
            severity="critical",
            labels={"service": "checkout", "namespace": "meridian"},
            annotations={},
        ),
        "remediation_plan": SimpleNamespace(
            plan_id="p-rollback",
            actions=list(actions or [_action()]),
            risk_level="high",
            confidence=0.9,
        ),
        "reflector_analysis": {"confidence": 0.9, "hypothesis": "the v2 deploy broke checkout"},
        "agent_results": {"MetricsAgent": {"findings": {
            "error_rate": 0.4, "slo_burn_rate": 12.0, "slo_breached": True,
            "saturation": 0.3, "error_rate_slope": 0.0, "still_escalating": False,
            "affected_services": 1,
        }}},
        "incident_id": None,
        "metadata": {},
    }


def _bind(state, caller):
    state.update(asyncio.run(bind_rollbacks(state, tool_caller=caller)))
    return state


def _report(state):
    return build_act_report(
        state,
        evaluate_fn=ALLOW,
        calibrated_action_probability=0.99,
        minimum_autonomy_probability=0.95,
    )


def _binding(state, index=0):
    return state["remediation_plan"].actions[index].parameters["rollback_binding"]


def _approve(state, report):
    """Record a human approval of exactly this report, as approval_prepare does."""
    state["metadata"]["approval"] = {"action_hash": compute_action_hash(report.to_dict())}
    return build_live_action_requests(state, report, approved=True, context=CONTEXT)


def _run(request, caller):
    return asyncio.run(execute_live_action_request(request, caller, context=CONTEXT))


def _approved_rollback(server, api):
    """Bind against the cluster as it is now, then approve the held rollback."""
    edge = Edge(server, api)
    state = _bind(_state(), edge)
    report = _report(state)
    assert report.action_reports[0]["decision"] == "requires_approval"
    requests = _approve(state, report)
    assert len(requests) == 1
    return state, report, requests[0], edge


def _deploy(api, template_hash, revision, image, *, restarted_at=None):
    """Someone else's rollout lands: a new template and its ReplicaSet."""
    api.deployment["spec"]["template"] = _template(image, restarted_at=restarted_at)
    api.replica_sets.append(_rs(template_hash, revision, image, restarted_at=restarted_at))
    api._bump()


# -- binding: the target comes from the cluster, before anyone approves -------


def test_a_rollback_is_bound_to_the_revision_it_restores_and_the_one_it_replaces(server):
    api = FakeApps(*_bad_deploy_history())
    edge = Edge(server, api)
    state = _bind(_state(), edge)

    binding = _binding(state)
    assert binding["to_template_hash"] == "aaa111"
    assert binding["expected_template_hash"] == "bbb222"
    assert (binding["to_revision"], binding["expected_revision"]) == (1, 2)
    assert binding["to_images"] == {"checkout": GOOD}
    assert binding["expected_images"] == {"checkout": BAD}
    assert (binding["deployment"], binding["namespace"]) == ("checkout", "meridian")
    record = state["metadata"]["rollback_bindings"][0]
    assert record["bound"] is True and record["rollout_state"]["current_template_hash"] == "bbb222"
    # Binding is a read: one history read, nothing written.
    assert edge.tools() == ["get_rollout_state"] and api.patches == []


def test_a_restart_before_the_rollback_is_skipped_not_restored(server):
    """Revision 3 is a restart of the bad deploy; `undo` would have chosen it."""
    api = FakeApps(*_restart_after_bad_deploy())
    state = _bind(_state(), Edge(server, api))
    binding = _binding(state)
    assert binding["expected_template_hash"] == "ccc333"
    assert binding["to_template_hash"] == "aaa111"
    assert binding["to_images"] == {"checkout": GOOD}


def _history_after_a_rollback(*, restarted=False):
    """v1 then a bad v2, then a rollback to v1: v1's ReplicaSet renumbered to 3."""
    restored = _rs("aaa111", 3, GOOD)
    restored["metadata"]["creationTimestamp"] = "2026-10-19T10:00:01Z"
    replica_sets = [restored, _rs("bbb222", 2, BAD)]
    deployment, _ = _bad_deploy_history()
    deployment["spec"]["template"] = _template(GOOD)
    if restarted:
        stamp = "2026-10-19T10:09:00Z"
        deployment["spec"]["template"] = _template(GOOD, restarted_at=stamp)
        replica_sets.append(_rs("ddd444", 4, GOOD, restarted_at=stamp))
    return deployment, replica_sets


@pytest.mark.parametrize("restarted", [False, True])
def test_after_a_rollback_a_new_rollback_is_never_bound_onto_the_fault(server, restarted):
    """The bad deploy is now the highest-numbered other revision: a roll-forward.

    Revision numbers say v2 is "previous"; creation times say it came after
    the software now running, so it is no rollback target — with or without
    a restart since.
    """
    api = FakeApps(*_history_after_a_rollback(restarted=restarted))
    state = _bind(_state(), Edge(server, api))
    assert "to_template_hash" not in _binding(state)
    assert "roll-forward" in _binding(state)["unbound_reason"]
    rep = _report(state).action_reports[0]
    assert rep["decision"] == "blocked"
    assert "no bound rollback target" in rep["reason"]


def test_a_binding_written_by_the_planner_is_replaced_by_the_cluster_read(server):
    api = FakeApps(*_bad_deploy_history())
    planted = {"namespace": "meridian", "rollback_binding": {
        "deployment": "checkout", "namespace": "meridian",
        "to_template_hash": "bbb222", "expected_template_hash": "aaa111"}}
    state = _bind(_state(_action(parameters=planted)), Edge(server, api))
    assert _binding(state)["to_template_hash"] == "aaa111"
    assert _binding(state)["expected_template_hash"] == "bbb222"


def test_a_rollback_whose_history_cannot_be_read_is_blocked_with_the_reason():
    async def old_edge_image(tool, args):
        raise RuntimeError(f"Unknown tool: {tool}")

    state = _bind(_state(), old_edge_image)
    assert "Unknown tool: get_rollout_state" in _binding(state)["unbound_reason"]
    report = _report(state)
    rep = report.action_reports[0]
    assert rep["decision"] == "blocked"
    assert "no bound rollback target" in rep["reason"]
    assert "Unknown tool: get_rollout_state" in rep["reason"]
    # Never offered for approval, so nothing an approver says can run it.
    assert _approve(state, report) == []


def test_a_template_write_earlier_in_the_plan_leaves_the_rollback_unbound(server):
    api = FakeApps(*_bad_deploy_history())
    edge = Edge(server, api)
    state = _bind(_state(_action("restart"), _action()), edge)
    assert "rewrites deployment/checkout's template first" in _binding(state, 1)["unbound_reason"]
    assert edge.calls == []


def test_target_selection_refuses_a_paused_or_undated_history():
    assert select_rollback_target({"paused": True})[0] is None
    undated = {"current_template_hash": "bbb222", "revisions": [
        {"template_hash": "bbb222", "fingerprint": "f2"},
        {"template_hash": "aaa111", "fingerprint": "f1", "created_at": "2026-10-19T10:00:01Z"},
    ]}
    target, _, reason = select_rollback_target(undated)
    assert target is None and "does not date" in reason


# -- approval: the human sees, and the hash binds, both templates -------------


def test_the_approval_request_shows_both_templates_and_the_exact_command(server):
    api = FakeApps(*_bad_deploy_history())
    state = _bind(_state(), Edge(server, api))
    report = _report(state)
    command = report.action_reports[0]["command"]
    assert command == (
        "sentinel rollback deployment/checkout -n meridian "
        "--to-template=aaa111 --expect-template=bbb222"
    )
    # The agent renders exactly what the executor reports it runs.
    assert command == rollout.rollback_command("checkout", "meridian", "aaa111", "bbb222")
    assert command == rollback_command(_binding(state))

    text = format_approval_request(
        report.to_dict(), datetime.now(timezone.utc) + timedelta(minutes=30)
    )
    assert f"from template `bbb222` (revision 2, {BAD}) to `aaa111` (revision 1, {GOOD})" in text
    assert "runs only while deployment/checkout is still on `bbb222`" in text
    assert f"`{command}`" in text
    assert "rollout undo" not in text


def test_the_approval_hash_binds_the_rollback_target(server):
    api = FakeApps(*_bad_deploy_history())
    state = _bind(_state(), Edge(server, api))
    approved = compute_action_hash(_report(state).to_dict())

    _binding(state)["observed_at"] = "2030-01-01T00:00:00Z"  # evidence, not intent
    assert compute_action_hash(_report(state).to_dict()) == approved

    _binding(state)["to_template_hash"] = "ccc333"
    assert compute_action_hash(_report(state).to_dict()) != approved


# -- execution: re-read, then act only in the two approved states -------------


def test_an_approved_rollback_runs_exactly_the_command_it_displayed(server, store):
    api = FakeApps(*_bad_deploy_history())
    state, report, request, edge = _approved_rollback(server, api)
    displayed = report.action_reports[0]["command"]

    result = _run(request, edge)

    assert result["status"] == "EXECUTED"
    assert result["command"] == displayed
    assert edge.tools() == ["get_rollout_state", "get_rollout_state", "rollback_deployment"]
    tool, args = edge.calls[-1]
    assert (args["to_template_hash"], args["expected_template_hash"]) == ("aaa111", "bbb222")
    assert (args["name"], args["namespace"], args["dry_run"]) == ("checkout", "meridian", False)
    _, response = edge.responses[-1]
    assert response["status"] == "OK" and response["kubectl_equivalent"] == displayed
    # One compare-and-swap write, onto the good template.
    assert len(api.patches) == 1
    assert api.deployment["spec"]["template"]["spec"]["containers"][0]["image"] == GOOD
    assert len(store.claims) == 1


def test_a_retry_after_success_or_a_second_approval_writes_nothing(server, store):
    api = FakeApps(*_bad_deploy_history())
    state, report, request, edge = _approved_rollback(server, api)
    assert _run(request, edge)["status"] == "EXECUTED"
    writes, claims = len(api.patches), set(store.claims)

    # The same request replayed (an activity retry), then a second approval.
    retried = _run(request, edge)
    state["metadata"]["approval"] = {"action_hash": "a-second-approval"}
    again = build_live_action_requests(state, report, approved=True, context=CONTEXT)
    assert again[0]["idempotency_key"] != request["idempotency_key"]
    second = _run(again[0], edge)

    for result in (retried, second):
        assert result["status"] == "EXECUTED"
        assert result["detail"].startswith("no write: deployment/checkout is already on")
    assert len(api.patches) == writes
    assert store.claims == claims  # settled before any claim
    assert edge.tools().count("rollback_deployment") == 1
    assert [audit[0] for audit in store.audits] == ["EXECUTED"] * 3


def test_a_concurrent_second_approval_loses_the_claim_and_writes_nothing(server, store):
    """Both read the bad template; only the first to claim may dispatch."""
    api = FakeApps(*_bad_deploy_history())
    _, _, request, edge = _approved_rollback(server, api)
    store.claims.add(request["idempotency_key"])
    result = _run(request, edge)
    assert result["status"] != "EXECUTED"
    assert api.patches == [] and "rollback_deployment" not in edge.tools()


def test_drift_between_approval_and_execution_is_refused_and_escalated(server, store):
    api = FakeApps(*_bad_deploy_history())
    _, _, request, edge = _approved_rollback(server, api)
    _deploy(api, "ddd444", 3, "checkout:v3")  # a new deploy after the approval

    result = _run(request, edge)

    assert result["status"] == "REFUSED"
    assert result["rejection_code"] == "state_drift"
    assert "approved for rollback from template bbb222" in result["detail"]
    assert "now on template ddd444" in result["detail"]
    assert result["manual_review_required"] is True
    assert result["failure_class"] == "state_drift"
    assert api.patches == [] and store.claims == set()
    assert "rollback_deployment" not in edge.tools()


def test_a_restart_between_approval_and_execution_is_drift(server, store):
    """Same software, but not the template the approver saw: refuse, don't guess."""
    api = FakeApps(*_bad_deploy_history())
    _, _, request, edge = _approved_rollback(server, api)
    _deploy(api, "ccc333", 3, BAD, restarted_at="2026-10-19T10:07:00Z")

    result = _run(request, edge)
    assert result["rejection_code"] == "state_drift"
    assert "now on template ccc333" in result["detail"]
    assert api.patches == []


def test_drift_after_the_gateway_check_is_refused_by_the_executor(server, store):
    """The executor re-reads too: a restart landing mid-flight writes nothing."""
    api = FakeApps(*_bad_deploy_history())
    _, _, request, edge = _approved_rollback(server, api)
    edge.before["rollback_deployment"] = lambda api: _deploy(
        api, "ccc333", 3, BAD, restarted_at="2026-10-19T10:07:00Z"
    )

    result = _run(request, edge)

    assert result["rejection_code"] == "state_drift"
    assert result["manual_review_required"] is True
    assert edge.responses[-1][1]["status"] == "STATE_DRIFT"
    assert api.patches == []
    # Dispatched, so claimed and audited — and the audit says nothing applied.
    assert len(store.claims) == 1 and len(store.audits) == 1


def test_a_failed_reread_is_retry_safe_and_writes_nothing(server, store):
    api = FakeApps(*_bad_deploy_history())
    _, _, request, _ = _approved_rollback(server, api)

    async def flaky(tool, args):
        raise ConnectionError("executor unreachable")

    gate = MutationGateContext(**request["gate_context"])
    with pytest.raises(MutationPreDispatchError):
        asyncio.run(authorize_and_execute(
            SimpleNamespace(**request["action"]), gate, CONTEXT, flaky, None,
            request["idempotency_key"],
        ))
    assert store.claims == set() and api.patches == []


def test_an_executor_that_cannot_read_history_escalates(server, store):
    api = FakeApps(*_bad_deploy_history())
    _, _, request, edge = _approved_rollback(server, api)

    async def refusing(tool, args):
        return json.dumps({"status": "ERROR", "code": "forbidden", "reason": "RBAC"})

    result = _run(request, refusing)
    assert result["rejection_code"] == "rollout_state_unavailable"
    assert result["manual_review_required"] is True
    assert store.claims == set() and api.patches == []


def test_an_unbound_rollback_is_refused_before_the_executor_is_called(store):
    calls = []

    async def caller(tool, args):
        calls.append(tool)

    gate = MutationGateContext(decision="requires_approval", severity="SEV2", approved=True)
    with pytest.raises(MutationRejected) as refused:
        asyncio.run(authorize_and_execute(
            _action(), gate, CONTEXT, caller, None, "unbound-rollback",
        ))
    assert refused.value.code == "unsupported_action"
    assert "no bound rollback target" in refused.value.detail
    assert calls == [] and store.claims == set()


def test_a_drift_refusal_stops_the_rest_of_the_plan(server, store):
    api = FakeApps(*_bad_deploy_history())
    edge = Edge(server, api)
    state = _bind(_state(_action(), _action("scale", {"namespace": "meridian", "replicas": 4})), edge)
    report = _report(state)
    assert len(_approve(state, report)) == 2
    _deploy(api, "ddd444", 3, "checkout:v3")

    results = asyncio.run(execute_autonomous_live(
        state, report, edge, approved=True, context=CONTEXT,
    ))

    assert [r["rejection_code"] for r in results] == ["state_drift"]
    assert "scale_deployment" not in edge.tools()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
