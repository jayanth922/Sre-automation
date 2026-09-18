#!/usr/bin/env python3
"""Severity says it prefers measured telemetry. It has to actually read some.

`extract_incident_signals` carries the comment "Prefer structured metrics from
investigation results over labels" and walks `agent_results` looking for
`error_rate`, `slo_burn_rate`, `affected_pods` and friends. `_walk_metrics`
only descends into dicts and lists — but `agent_nodes.py` stores
`agent_results[agent_key] = agent_response`, the *text* of the specialist's
final message. Probed on 2026-09-15 with a real metrics narration:

    walk(real prose) : {}
    walk(dict shape) : {'error_rate': 0.24, 'slo_burn_rate': 14.2, ...}

The dict shape occurs only in fixtures. So the branch never ran in production
and every severity decision came from alert labels alone, while the evidence
links implied telemetry had been consulted.

The numbers were never missing, just somewhere else: the specialist's raw tool
results. Production now projects their measured values into compact checkpoint
metadata and stores the full transcript as an artifact; the legacy
`metadata[f"{agent}_trace"]` shape below remains the compatibility and storage-
failure path. Both use the measurement itself rather than the model's retelling,
with provenance naming the agent, the tool and the path inside the payload.

The live case this came from: incident `033d433c` ([inventory-service]
InventoryHighErrorRate, 02:33) whose Performance Metrics Agent found the
alert's "error rate" was entirely 404s. Nothing it measured could reach the
severity gate.
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from sre_agent.act_phase import (
    _walk_metrics,
    _walk_tool_outputs,
    extract_incident_signals,
)

PROSE = (
    "No 5xx errors here — the alert's error rate is entirely 404s "
    "(error_type=not_found) on `/items/{item_id}`. error_rate is 0.24 and the "
    "SLO burn rate is 14.2x with 3 affected pods."
)


def _tool(payload, *, name="prometheus_query", tool_call_id="tc1", status=None):
    kwargs = {"status": status} if status else {}
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return ToolMessage(content=body, name=name, tool_call_id=tool_call_id, **kwargs)


def _state(trace, *, labels=None, agent="metrics_agent", results=None):
    return {
        "agent_results": results if results is not None else {agent: PROSE},
        "metadata": {f"{agent}_trace": trace},
        "alert_context": {"labels": labels or {}},
    }


def _sources(signals, prefix):
    return [link.source for link in signals.evidence if (link.source or "").startswith(prefix)]


# ---------------------------------------------------------------------------
# The defect itself
# ---------------------------------------------------------------------------

def test_prose_findings_yield_nothing_and_that_is_still_true():
    """Unchanged on purpose: the fix does not try to parse numbers out of
    English. Guessing at prose is how a severity gate starts inventing."""
    assert _walk_metrics({"metrics_agent": PROSE}) == {}


def test_measured_values_now_reach_the_severity_gate():
    signals = extract_incident_signals(
        _state([AIMessage(content=PROSE), _tool({"error_rate": 0.24, "slo_burn_rate": 14.2, "affected_pods": 3})])
    )

    assert signals.error_rate == 0.24
    assert signals.slo_burn_rate == 14.2
    assert signals.affected_pods == 3


def test_the_provenance_names_the_agent_the_tool_and_the_path():
    """An unattributable number is not evidence."""
    signals = extract_incident_signals(
        _state([_tool({"data": {"result": [{"error_rate": 0.24}]}})])
    )

    assert _sources(signals, "tool") == [
        "tool:metrics_agent:prometheus_query:data.result[0].error_rate"
    ]


def test_nested_prometheus_shaped_payloads_are_traversed():
    signals = extract_incident_signals(
        _state([_tool({"data": {"result": [
            {"metric": {"job": "inventory"}, "error_rate": 0.42, "affected_pods": 7}
        ]}})])
    )

    assert signals.error_rate == 0.42
    assert signals.affected_pods == 7


# ---------------------------------------------------------------------------
# What must NOT become evidence
# ---------------------------------------------------------------------------

def test_a_failed_tool_contributes_no_metrics():
    """A failed call's content is an error string. Severity computed from one
    would be worse than no severity at all. This only became reliable once the
    tool-failure contract started marking these — see
    tests/test_tool_failure_contract.py."""
    signals = extract_incident_signals(
        _state([_tool({"error_rate": 0.99}, tool_call_id="tc2", status="error")])
    )

    assert signals.error_rate is None


def test_an_assistant_message_in_the_trace_is_ignored():
    """AIMessages have no tool_call_id. If the model happens to emit JSON that
    mentions error_rate, that is still the model talking."""
    signals = extract_incident_signals(
        _state([AIMessage(content=json.dumps({"error_rate": 0.77}))])
    )

    assert signals.error_rate is None


def test_non_json_tool_output_is_skipped_quietly():
    signals = extract_incident_signals(
        _state([_tool("pod/inventory-service-abc123 restarted 4 times")])
    )

    assert signals.error_rate is None


def test_malformed_json_does_not_break_the_walk():
    signals = extract_incident_signals(
        _state([_tool('{"error_rate": 0.5'), _tool({"error_rate": 0.31}, tool_call_id="tc2")])
    )

    assert signals.error_rate == 0.31


def test_a_missing_trace_leaves_everything_unknown():
    signals = extract_incident_signals({"agent_results": {}, "alert_context": {"labels": {}}})

    assert signals.error_rate is None
    assert signals.slo_burn_rate is None


@pytest.mark.parametrize("metadata", [None, "not-a-dict", 42, []])
def test_a_junk_metadata_field_is_survivable(metadata):
    assert _walk_tool_outputs({"metadata": metadata}) == {}


def test_a_trace_that_is_not_a_list_is_ignored():
    assert _walk_tool_outputs({"metadata": {"metrics_agent_trace": "oops"}}) == {}


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------

def test_tool_output_outranks_the_alert_label():
    """The label is what the alert rule asserted; the tool output is what the
    cluster actually reported."""
    signals = extract_incident_signals(
        _state([_tool({"error_rate": 0.24})], labels={"error_rate": "0.99"})
    )

    assert signals.error_rate == 0.24


def test_a_label_still_stands_when_no_tool_measured_it():
    signals = extract_incident_signals(
        _state([_tool({"affected_pods": 3})], labels={"error_rate": "0.5"})
    )

    assert signals.error_rate == 0.5
    assert signals.affected_pods == 3


def test_structured_agent_results_still_work():
    """The fixture shape keeps working — this fix adds a source, it does not
    remove one."""
    signals = extract_incident_signals(
        {
            "agent_results": {"metrics_agent": {"error_rate": 0.17}},
            "alert_context": {"labels": {}},
        }
    )

    assert signals.error_rate == 0.17


def test_a_superseded_value_leaves_the_evidence_ledger():
    """The ledger must name the value the gate actually used. A stale link for
    a number that lost is the same dishonesty in miniature."""
    signals = extract_incident_signals(
        {
            "agent_results": {"metrics_agent": {"error_rate": 0.17}},
            "alert_context": {"labels": {}},
            "metadata": {"metrics_agent_trace": [_tool({"error_rate": 0.24})]},
        }
    )

    error_rate_links = [link for link in signals.evidence if link.field == "error_rate"]
    assert [link.value for link in error_rate_links] == [0.24]
    assert error_rate_links[0].source.startswith("tool:")
    assert signals.error_rate == 0.24


def test_the_disagreement_is_logged_not_swallowed(caplog):
    with caplog.at_level("INFO", logger="sre_agent.act_phase"):
        extract_incident_signals(
            {
                "agent_results": {"metrics_agent": {"error_rate": 0.17}},
                "alert_context": {"labels": {}},
                "metadata": {"metrics_agent_trace": [_tool({"error_rate": 0.24})]},
            }
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any("supersedes 0.17" in message and "0.24" in message for message in messages)


def test_agreement_is_not_logged_as_a_conflict():
    signals = extract_incident_signals(
        {
            "agent_results": {"metrics_agent": {"error_rate": 0.24}},
            "alert_context": {"labels": {}},
            "metadata": {"metrics_agent_trace": [_tool({"error_rate": 0.24})]},
        }
    )

    assert len([link for link in signals.evidence if link.field == "error_rate"]) == 1


def test_the_first_tool_to_measure_something_wins():
    """Deterministic ordering matters more than which one is 'right' — two
    tools disagreeing is a real condition and the provenance records which was
    used."""
    signals = extract_incident_signals(
        _state([
            _tool({"error_rate": 0.24}, name="prometheus_query"),
            _tool({"error_rate": 0.88}, name="loki_query", tool_call_id="tc2"),
        ])
    )

    assert signals.error_rate == 0.24
    assert _sources(signals, "tool") == ["tool:metrics_agent:prometheus_query:error_rate"]


def test_several_specialists_each_contribute():
    state = {
        "agent_results": {},
        "alert_context": {"labels": {}},
        "metadata": {
            "metrics_agent_trace": [_tool({"error_rate": 0.24})],
            "k8s_agent_trace": [_tool({"affected_pods": 5}, name="kubectl_get", tool_call_id="tc2")],
        },
    }

    signals = extract_incident_signals(state)

    assert signals.error_rate == 0.24
    assert signals.affected_pods == 5


def test_burn_rate_is_normalised_to_slo_burn_rate():
    signals = extract_incident_signals(_state([_tool({"burn_rate": 9.5})]))

    assert signals.slo_burn_rate == 9.5


def test_boolean_and_scope_metrics_survive_the_round_trip():
    signals = extract_incident_signals(
        _state([_tool({"slo_breached": True, "customer_scope": "all-tenants"})])
    )

    assert signals.slo_breached is True
    assert signals.customer_scope == "all-tenants"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
