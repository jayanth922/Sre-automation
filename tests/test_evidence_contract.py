"""The typed evidence contract, and the three drifts it was written to stop."""

from __future__ import annotations

import pytest

from sre_agent.act_phase import (
    _walk_metrics,
    extract_incident_signals,
    measured_evidence_for_trace,
)
from sre_agent.evidence_contract import (
    ALIASES_OF,
    METRIC_ALIASES,
    METRIC_NAMES,
    SEVERITY_METRICS,
    EvidenceContractError,
    EvidenceRecord,
    canonical_metric,
    coerce_metric,
)


def _tool_message(payload, *, name="prometheus_query", status="success"):
    return {
        "tool_call_id": "call-1",
        "name": name,
        "status": status,
        "content": payload,
    }


# --------------------------------------------------------------------------
# The registry is one definition
# --------------------------------------------------------------------------


def test_the_walker_collects_exactly_what_the_contract_can_type():
    """The old failure: a name collected here but unknown to the coercer.

    `_walk_metrics` used to keep its own set. A metric added there and not to
    `_absorb`'s type dispatch was collected, failed to coerce, and vanished
    with no log line.
    """
    assert _walk_metrics.__globals__["METRIC_NAMES"] is METRIC_NAMES
    for name in METRIC_NAMES:
        assert canonical_metric(name) in SEVERITY_METRICS


def test_every_alias_resolves_to_a_real_metric():
    for alias, canonical in METRIC_ALIASES.items():
        assert canonical in SEVERITY_METRICS
        assert canonical_metric(alias) == canonical
        assert alias in ALIASES_OF[canonical]


def test_an_unknown_metric_is_not_evidence():
    assert canonical_metric("cpu_vibes") is None
    assert coerce_metric("cpu_vibes", 3) is None
    with pytest.raises(EvidenceContractError):
        EvidenceRecord(metric="cpu_vibes", value=3, agent="a", tool="t")


@pytest.mark.parametrize(
    "metric,raw,expected",
    [
        ("error_rate", "0.24", 0.24),
        ("slo_burn_rate", 14.2, 14.2),
        ("burn_rate", "3", 3.0),  # alias coerces as its canonical metric
        ("affected_pods", "3.0", 3),
        ("dependency_count", 7, 7),
        ("slo_breached", "true", True),
        ("slo_breached", "no", False),
        ("still_escalating", False, False),
        ("customer_scope", "  enterprise ", "enterprise"),
    ],
)
def test_coercion_follows_the_declared_type(metric, raw, expected):
    assert coerce_metric(metric, raw) == expected


@pytest.mark.parametrize("raw", [None, "", "not-a-number", {}, []])
def test_unusable_values_are_none_never_a_calm_zero(raw):
    """None means UNKNOWN. Defaulting to 0.0 would fabricate a healthy signal."""
    assert coerce_metric("error_rate", raw) is None


# --------------------------------------------------------------------------
# A record has to be attributable
# --------------------------------------------------------------------------


def test_a_record_keeps_the_tool_call_in_parts_and_joins_it_for_the_ledger():
    record = EvidenceRecord(
        metric="error_rate",
        value="0.24",
        agent="metrics_agent",
        tool="prometheus_query",
        pointer="data.result[0].value",
    )
    assert record.value == 0.24
    assert record.agent == "metrics_agent"
    assert record.tool == "prometheus_query"
    assert record.source == "metrics_agent:prometheus_query:data.result[0].value"
    assert record.observed_at


def test_a_record_with_no_pointer_still_names_the_tool():
    record = EvidenceRecord(metric="saturation", value=0.9, agent="k8s", tool="top")
    assert record.source == "k8s:top"


@pytest.mark.parametrize("missing", ["agent", "tool"])
def test_an_unattributed_number_is_refused(missing):
    kwargs = {
        "metric": "error_rate",
        "value": 0.2,
        "agent": "metrics_agent",
        "tool": "prometheus_query",
    }
    kwargs[missing] = "   "
    with pytest.raises(EvidenceContractError):
        EvidenceRecord(**kwargs)


def test_a_value_that_will_not_coerce_is_refused_at_construction():
    with pytest.raises(EvidenceContractError):
        EvidenceRecord(
            metric="affected_pods", value="many", agent="k8s", tool="get_pods"
        )


# --------------------------------------------------------------------------
# The checkpoint boundary
# --------------------------------------------------------------------------


def test_a_record_round_trips_through_the_checkpoint_shape():
    original = EvidenceRecord(
        metric="affected_pods",
        value=3,
        agent="k8s_agent",
        tool="get_pods",
        pointer="items",
    )
    restored = EvidenceRecord.from_dict("affected_pods", original.to_dict())
    assert restored == original


def test_a_pre_contract_record_is_split_back_apart_not_discarded():
    """Checkpoints written before this contract carry only value and source.

    Those numbers came from real tool calls, so they are recovered rather than
    dropped — dropping them would silently downgrade a restored incident to
    alert labels alone.
    """
    restored = EvidenceRecord.from_dict(
        "error_rate",
        {"value": 0.24, "source": "metrics_agent:prometheus_query:error_rate"},
    )
    assert restored.agent == "metrics_agent"
    assert restored.tool == "prometheus_query"
    assert restored.pointer == "error_rate"
    assert restored.value == 0.24


def test_a_bare_source_prefix_says_only_what_it_knew():
    restored = EvidenceRecord.from_dict("error_rate", {"value": 1, "source": "tool"})
    assert restored.agent == "tool"
    assert restored.tool == "tool"
    assert restored.pointer == ""


@pytest.mark.parametrize(
    "payload",
    [
        "not-a-mapping",
        {"source": "a:b"},  # no value
        {"value": 0.2},  # no provenance at all
        {"value": 0.2, "source": "   "},
        {"value": "lots", "source": "a:b"},  # will not coerce
    ],
)
def test_an_unparseable_stored_record_is_rejected(payload):
    with pytest.raises(EvidenceContractError):
        EvidenceRecord.from_dict("error_rate", payload)


def test_malformed_stored_evidence_is_dropped_loudly(caplog):
    """A measurement missing from the ledger must never be silent."""
    state = {
        "alert_context": {"labels": {}},
        "agent_results": {},
        "metadata": {
            "measured_evidence": {
                "metrics_agent": {"error_rate": {"value": 0.24}},  # no provenance
            }
        },
    }
    with caplog.at_level("WARNING"):
        signals = extract_incident_signals(state)
    assert signals.error_rate is None
    assert any("unusable measured evidence" in r.message for r in caplog.records)


# --------------------------------------------------------------------------
# End to end through the severity gate
# --------------------------------------------------------------------------


def test_tool_results_reach_severity_as_typed_records():
    projection = measured_evidence_for_trace(
        "metrics_agent",
        [_tool_message({"error_rate": 0.24, "affected_pods": "3", "junk": 1})],
    )
    assert set(projection) == {"error_rate", "affected_pods"}
    assert projection["affected_pods"]["value"] == 3
    assert projection["affected_pods"]["tool"] == "prometheus_query"

    signals = extract_incident_signals(
        {
            "alert_context": {"labels": {}},
            "agent_results": {},
            "metadata": {"measured_evidence": {"metrics_agent": projection}},
        }
    )
    assert signals.error_rate == 0.24
    assert signals.affected_pods == 3


def test_a_failed_tool_call_contributes_no_evidence():
    projection = measured_evidence_for_trace(
        "metrics_agent",
        [_tool_message({"error_rate": 0.99}, status="error")],
    )
    assert projection == {}


def test_alert_labels_are_read_through_the_same_contract():
    """The third copy of the type rule lived here, inline and hand-written."""
    signals = extract_incident_signals(
        {
            "alert_context": {
                "labels": {"burn_rate": "14.2", "affected_pods": "3"},
                "annotations": {"slo_breached": "true"},
            },
            "agent_results": {},
            "metadata": {},
        }
    )
    assert signals.slo_burn_rate == 14.2
    assert signals.affected_pods == 3
    assert signals.slo_breached is True


def test_a_falsy_label_is_a_measurement_not_a_missing_value():
    """`labels.get(x) or annotations.get(x)` dropped 0 and False on the floor."""
    signals = extract_incident_signals(
        {
            "alert_context": {
                "labels": {"affected_pods": 0, "slo_breached": False},
                "annotations": {},
            },
            "agent_results": {},
            "metadata": {},
        }
    )
    assert signals.affected_pods == 0
    assert signals.slo_breached is False
