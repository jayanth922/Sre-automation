"""Runbooks are the solution book: no runbook, no automated fix.

The planner may diagnose freely, but every action that changes the cluster has
to be one a reviewed runbook prescribes for the firing alert, all from the same
branch. Anything else is replaced by an escalation that tells the operator
automated remediation is not possible.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from sre_agent import graph_builder, model_router
from sre_agent.agent_state import AlertContext, ReflectorAnalysis, RemediationPlan
from sre_agent.approval_flow import format_approval_request
from sre_agent.runbook_authority import (
    authorize_plan,
    parse_runbook,
    runbook_only_remediation,
)

_RUNBOOKS = Path(__file__).resolve().parents[1] / "examples" / "meridian" / "runbooks"


def _example(name):
    return parse_runbook(name, (_RUNBOOKS / f"{name}.md").read_text())


def _act(action_type, target, **parameters):
    return SimpleNamespace(action_type=action_type, target=target, parameters=parameters)


@pytest.fixture(scope="module")
def library():
    return [
        _example(n)
        for n in ("downstream-dependency-failure", "high-error-rate", "high-latency", "oom-memory")
    ]


def test_the_shipped_runbooks_parse_into_branches_with_their_alerts(library):
    oom = next(rb for rb in library if rb.title == "oom-memory")

    assert oom.addresses("CheckoutServiceOOMKilled")
    assert not oom.addresses("PaymentProviderDown")
    assert [b.heading for b in oom.branches] == [
        "Branch A — Action: none, close the incident",
        "Branch B — Action: replace the process, then remove the cause",
    ]
    assert oom.branches[1].covers("restart_deployment", "checkout-service")
    assert not oom.draft


def test_a_prescribed_fix_is_authorized_and_names_its_branch(library):
    verdict = authorize_plan(
        [_act("restart", "deployment/checkout-service")], library, "CheckoutServiceOOMKilled"
    )

    assert verdict.authorized
    assert verdict.reference == (
        "oom-memory → Branch B — Action: replace the process, then remove the cause"
    )


def test_an_action_no_runbook_prescribes_is_refused(library):
    verdict = authorize_plan(
        [_act("scale", "checkout-service", replicas=4)], library, "CheckoutServiceOOMKilled"
    )

    assert not verdict.authorized
    assert verdict.unprescribed == ("scale checkout-service",)


def test_a_runbook_for_another_alert_authorizes_nothing(library):
    """Search ranks on any shared token; retrieval is not applicability."""
    verdict = authorize_plan(
        [_act("restart", "checkout-service")], library, "PaymentProviderDown"
    )

    assert not verdict.authorized


def test_an_alert_no_runbook_lists_is_not_remediable(library):
    verdict = authorize_plan([_act("restart", "cart-service")], library, "CartQueueBacklog")

    assert not verdict.authorized
    assert verdict.reason.startswith("No runbook addresses CartQueueBacklog.")


def test_actions_from_different_branches_are_not_a_prescription(library):
    """Branch B restarts checkout, Branch E restarts inventory; nobody wrote both."""
    verdict = authorize_plan(
        [_act("restart", "checkout-service"), _act("restart", "inventory-service")],
        library,
        "CheckoutHighLatency",
    )

    assert not verdict.authorized
    assert verdict.unprescribed == ()
    assert "no single runbook branch" in verdict.reason


def test_an_env_prescription_does_not_license_a_memory_patch(library):
    latency = [rb for rb in library if rb.title == "high-latency"]

    env = authorize_plan(
        [_act("config_change", "inventory-service", env={"DB_POOL_SIZE": "40"})],
        latency,
        "InventorySlowQueries",
    )
    memory = authorize_plan(
        [_act("config_change", "inventory-service", memory="1Gi")],
        latency,
        "InventorySlowQueries",
    )

    assert env.authorized
    assert not memory.authorized


def test_a_placeholder_target_means_the_affected_service(library):
    verdict = authorize_plan(
        [_act("rollback", "inventory-service")], library, "InventoryHighErrorRate"
    )

    assert verdict.authorized
    assert "Branch E" in verdict.branch


def test_a_negated_call_prescribes_nothing():
    rb = parse_runbook(
        "rb",
        "Alert Name: X\n\n## Action\n\nDo not run scale_deployment(name=\"api\").\n"
        "restart_deployment(name=\"api\")\n",
    )

    assert authorize_plan([_act("restart", "api")], [rb], "X").authorized
    assert not authorize_plan([_act("scale", "api")], [rb], "X").authorized


def test_calls_outside_an_action_section_prescribe_nothing():
    rb = parse_runbook(
        "rb", "Alert Name: X\n\n## Diagnosis\n\nrestart_deployment(name=\"api\")\n"
    )

    assert not authorize_plan([_act("restart", "api")], [rb], "X").authorized


@pytest.mark.parametrize(
    "title, banner",
    [
        ("RB-AUTO-oom-api | api | Oom", ""),
        ("OOM fix", "> Auto-generated from incident 1234. Review and promote.\n\n"),
    ],
)
def test_an_auto_generated_runbook_is_a_draft_until_a_human_promotes_it(title, banner):
    rb = parse_runbook(
        title, f"{banner}Alert Name: X\n\n## Action\n\nrestart_deployment(name=\"api\")\n"
    )

    verdict = authorize_plan([_act("restart", "api")], [rb], "X")

    assert rb.draft
    assert not verdict.authorized
    assert "unreviewed auto-generated drafts" in verdict.reason


def test_escalating_or_inspecting_needs_no_prescription(library):
    plan = [_act("inspect", "checkout-service"), _act("escalate", "checkout-service")]

    assert authorize_plan(plan, library, "CheckoutHighErrorRate").authorized


def test_runbook_only_is_the_default(monkeypatch):
    monkeypatch.delenv("RUNBOOK_ONLY_REMEDIATION", raising=False)
    assert runbook_only_remediation()
    monkeypatch.setenv("RUNBOOK_ONLY_REMEDIATION", "false")
    assert not runbook_only_remediation()


# ---------------------------------------------------------------------------
# The planner node, end to end with stub tools and a stub model.
# ---------------------------------------------------------------------------


class _Tool:
    def __init__(self, name, fn):
        self.name = name
        self.calls = []
        self._fn = fn

    async def ainvoke(self, args):
        self.calls.append(args)
        return self._fn(args)


def _notion(pages):
    """search_runbooks / get_runbook_content over {page_id: (title, markdown)}."""

    def search(args):
        results = [
            {"runbook_id": pid, "title": title, "path": f"https://notion.so/{pid}"}
            for pid, (title, _) in pages.items()
        ]
        return json.dumps({"query": args.get("query"), "count": len(results), "results": results})

    def content(args):
        title, markdown = pages[args["page_id"]]
        return [{"type": "text", "text": json.dumps({"title": title, "content": markdown})}]

    return [_Tool("search_runbooks", search), _Tool("get_runbook_content", content)]


def _state(alert="CheckoutServiceOOMKilled", service="checkout-service"):
    return {
        "metadata": {},
        "alert_context": AlertContext(
            alert_name=alert, severity="critical", labels={"service": service}
        ),
        "reflector_analysis": ReflectorAnalysis(
            hypothesis="checkout-service leaks memory until OOMKilled",
            confidence=0.9,
            reasoning="working set climbs to the limit on every pod",
        ),
    }


def _model(monkeypatch, actions):
    seen = {"prompts": []}

    class Structured:
        async def ainvoke(self, messages):
            seen["prompts"].append(messages[-1].content)
            return RemediationPlan(
                plan_id="x",
                hypothesis="checkout-service leaks memory until OOMKilled",
                actions=actions,
                estimated_duration="5 minutes",
                risk_level="medium",
                runbook_reference="model's own claim",
            )

    class LLM:
        def with_structured_output(self, schema, method=None, **kwargs):
            return Structured()

    monkeypatch.setattr(model_router, "route_llm", lambda *a, **k: LLM())
    return seen


@pytest.fixture
def oom_notion():
    return _notion({"p-oom": ("OOM / Memory", (_RUNBOOKS / "oom-memory.md").read_text())})


def test_the_planner_keeps_a_prescribed_plan_and_cites_the_branch(monkeypatch, oom_notion):
    monkeypatch.delenv("RUNBOOK_ONLY_REMEDIATION", raising=False)
    seen = _model(monkeypatch, [
        {"action_type": "restart", "target": "checkout-service", "safety_check": "rollout"},
    ])

    result = asyncio.run(graph_builder._planner_node(_state(), oom_notion))
    plan = result["remediation_plan"]

    assert [a.action_type for a in plan.actions] == ["restart"]
    assert plan.runbook_gap is None
    assert plan.runbook_reference.startswith("OOM / Memory → Branch B")
    assert plan.source_runbook_url == "https://notion.so/p-oom"
    # The model was shown the full runbook, not a search excerpt.
    assert "Branch B — Action: replace the process" in seen["prompts"][0]
    assert "THE RUNBOOKS ABOVE ARE THE ONLY PERMITTED FIXES" in seen["prompts"][0]
    search, content = oom_notion
    assert search.calls == [{
        "query": "CheckoutServiceOOMKilled",
        "alert_name": "CheckoutServiceOOMKilled",
        "service": "checkout-service",
    }]
    assert content.calls == [{"page_id": "p-oom"}]


def test_the_planner_discards_a_fix_the_runbook_does_not_prescribe(monkeypatch, oom_notion):
    monkeypatch.delenv("RUNBOOK_ONLY_REMEDIATION", raising=False)
    _model(monkeypatch, [
        {"action_type": "restart", "target": "checkout-service", "safety_check": "rollout"},
        {"action_type": "scale", "target": "checkout-service", "safety_check": "more pods"},
    ])

    plan = asyncio.run(graph_builder._planner_node(_state(), oom_notion))["remediation_plan"]

    assert [(a.action_type, a.target) for a in plan.actions] == [("escalate", "checkout-service")]
    assert "No runbook for CheckoutServiceOOMKilled prescribes scale checkout-service" in plan.runbook_gap
    assert "Automated remediation is not possible" in plan.runbook_gap
    assert plan.actions[0].parameters["reason"] == plan.runbook_gap
    # The diagnosis survives; only the invented remedy is dropped.
    assert plan.hypothesis == "checkout-service leaks memory until OOMKilled"


def test_with_no_runbook_for_the_alert_the_model_is_never_asked(monkeypatch, oom_notion):
    monkeypatch.delenv("RUNBOOK_ONLY_REMEDIATION", raising=False)
    seen = _model(monkeypatch, [])

    result = asyncio.run(
        graph_builder._planner_node(_state("CartQueueBacklog", "cart-service"), oom_notion)
    )
    plan = result["remediation_plan"]

    assert seen["prompts"] == []
    assert [(a.action_type, a.target) for a in plan.actions] == [("escalate", "cart-service")]
    assert plan.runbook_gap.startswith("No runbook addresses CartQueueBacklog.")
    assert result["approval_status"] == "APPROVED"


def test_the_approval_message_leads_with_the_gap():
    text = format_approval_request(
        {
            "severity": "SEV2",
            "aggregate_decision": "requires_approval",
            "action_reports": [],
            "runbook_gap": "No runbook addresses CartQueueBacklog. Automated remediation is not possible.",
        },
        datetime.now(timezone.utc),
    )

    assert "Automated remediation is not possible.* No runbook addresses CartQueueBacklog." in text
