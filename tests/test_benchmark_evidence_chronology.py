"""The derived chronology has to actually be chronological.

``benchmark_evaluation.timeline`` is graded as a chronology by
``benchmarks.structured_grading._temporal``, but it is derived from
``ReflectorAnalysis.evidence``, which the model returns in the order it
argued its case. On 2026-09-22 that cost temporal_reasoning a FAIL --
"reported evidence timeline is not chronological" -- on evidence that was
itself correct and correctly stamped.
"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest

import sre_agent.supervisor as supervisor_module
from benchmarks.structured_grading import _temporal
from sre_agent.agent_state import (
    CausalLink,
    EvidenceReference,
    ReflectorAnalysis,
    RemediationAction,
    RemediationPlan,
)
from sre_agent.supervisor import SupervisorAgent, _observed_at_sort_key


def test_the_sort_key_orders_mixed_timestamp_spellings():
    """Specialists stamp evidence in whatever form their source used."""
    stamps = [
        "2026-09-22T22:57:44Z",
        "2026-09-22T22:51:23+00:00",
        "2026-09-22T23:05:00Z",
        "2026-09-22T22:57:23Z",
    ]

    assert sorted(stamps, key=_observed_at_sort_key) == [
        "2026-09-22T22:51:23+00:00",
        "2026-09-22T22:57:23Z",
        "2026-09-22T22:57:44Z",
        "2026-09-22T23:05:00Z",
    ]


def test_a_naive_stamp_is_read_as_utc_rather_than_breaking_the_sort():
    """Comparing a naive datetime with an aware one raises. The key
    normalises instead, so one sloppy stamp cannot take down the node."""
    stamps = ["2026-09-22T23:00:00", "2026-09-22T22:00:00Z"]

    assert sorted(stamps, key=_observed_at_sort_key) == [
        "2026-09-22T22:00:00Z",
        "2026-09-22T23:00:00",
    ]


def test_an_unparseable_stamp_sorts_after_every_real_one():
    stamps = ["yesterday", "2026-09-22T22:00:00Z", "2026-09-22T21:00:00Z"]

    ordered = sorted(stamps, key=_observed_at_sort_key)

    assert ordered == [
        "2026-09-22T21:00:00Z",
        "2026-09-22T22:00:00Z",
        "yesterday",
    ]


def _analysis(evidence):
    return ReflectorAnalysis(
        hypothesis="a slow query on inventory-service",
        reasoning="the db histogram rose while nothing else changed",
        confidence=0.8,
        affected_service="inventory-service",
        fault_mode="slow_query",
        causal_chain=[CausalLink(cause="a slow query", effect="p90 rose")],
        evidence=evidence,
        unknowns=[],
    )


def _plan():
    return RemediationPlan(
        plan_id="plan-1",
        hypothesis="a slow query on inventory-service",
        actions=[
            RemediationAction(
                action_type="restart",
                target="inventory-service",
                safety_check="policy gate",
            )
        ],
        estimated_duration="5 minutes",
        risk_level="low",
        confidence=0.7,
    )


# Argued newest-first, which is how a model explains a conclusion and then
# works backwards to its cause.
_OUT_OF_ORDER = [
    EvidenceReference(
        source="prometheus",
        reference="db_query_duration_seconds_bucket",
        claim="db p90 reached 1.895s",
        observed_at="2026-09-22T22:57:44Z",
    ),
    EvidenceReference(
        source="prometheus",
        reference="db_query_duration_seconds_bucket",
        claim="db p90 was 0.022s pre-fault",
        observed_at="2026-09-22T22:51:23Z",
    ),
    EvidenceReference(
        source="prometheus",
        reference="db_query_duration_seconds_bucket",
        claim="db p90 was 1.723s at the alert",
        observed_at="2026-09-22T22:57:23Z",
    ),
]


def _emitted_benchmark_evaluation(monkeypatch, evidence):
    """Drive the real aggregation path and return what it emitted."""
    payloads = []

    async def fake_emit(*args, **kwargs):
        payloads.append(kwargs.get("payload") or {})
        return SimpleNamespace(id=uuid.uuid4())

    async def fake_narrate(*args, **kwargs):
        return "## TL;DR\nA slow query on inventory-service."

    monkeypatch.setattr(supervisor_module, "emit_timeline_event", fake_emit)
    monkeypatch.setattr(supervisor_module, "narrate_supervisor_summary", fake_narrate)

    supervisor = SupervisorAgent.__new__(SupervisorAgent)
    supervisor.formatter = None
    supervisor.llm = None
    supervisor.system_prompt = ""

    asyncio.run(
        supervisor.aggregate_responses(
            {
                "current_query": "Investigate InventorySlowQueries",
                "alert_context": {
                    "alert_name": "InventorySlowQueries",
                    "labels": {"service": "inventory-service"},
                },
                "metadata": {},
                "agent_results": {"metrics_agent": "db p90 is 1.895s"},
                "thought_traces": {},
                "incident_id": str(uuid.uuid4()),
                "incident_status": "investigating",
                "reflector_analysis": _analysis(evidence),
                "remediation_plan": _plan(),
            }
        )
    )

    for payload in payloads:
        if "benchmark_evaluation" in payload:
            return payload["benchmark_evaluation"]
    pytest.fail("the aggregation path emitted no benchmark_evaluation")


def test_the_emitted_timeline_is_chronological_even_when_the_reflector_is_not(
    monkeypatch,
):
    evaluation = _emitted_benchmark_evaluation(monkeypatch, _OUT_OF_ORDER)

    assert [entry["observed_at"] for entry in evaluation["timeline"]] == [
        "2026-09-22T22:51:23Z",
        "2026-09-22T22:57:23Z",
        "2026-09-22T22:57:44Z",
    ]


def test_the_real_grader_accepts_the_timeline_this_path_emits(monkeypatch):
    """The criterion that FAILed on 2026-09-22, run against the fix."""
    evaluation = _emitted_benchmark_evaluation(monkeypatch, _OUT_OF_ORDER)

    grade = _temporal(evaluation)

    assert grade.state == "PASS", grade.rationale
    assert "not chronological" not in grade.rationale


def test_the_evidence_list_still_carries_the_reflectors_own_order(monkeypatch):
    """Only the chronology is sorted. ``evidence`` is an argument, and
    reordering it would scramble the reasoning it supports."""
    evaluation = _emitted_benchmark_evaluation(monkeypatch, _OUT_OF_ORDER)

    assert [item["claim"] for item in evaluation["evidence"]] == [
        "db p90 reached 1.895s",
        "db p90 was 0.022s pre-fault",
        "db p90 was 1.723s at the alert",
    ]


def test_evidence_without_a_stamp_stays_out_of_the_chronology(monkeypatch):
    """A GitHub "nothing shipped" finding is real evidence with no instant
    attached. It belongs in the argument, not on the clock."""
    evidence = list(_OUT_OF_ORDER) + [
        EvidenceReference(
            source="github",
            reference="no deploys since 2026-09-14",
            claim="no code change correlates",
        )
    ]

    evaluation = _emitted_benchmark_evaluation(monkeypatch, evidence)

    assert len(evaluation["evidence"]) == 4
    assert len(evaluation["timeline"]) == 3
