#!/usr/bin/env python3
"""A tool-call array leaked in XML parameter form still validates."""

from sre_agent.agent_state import ReflectorAnalysis


def test_reflector_lists_leaked_as_parameter_elements_are_decoded():
    """2026-09-29, incident 3fec162e: three list fields arrived as
    `<parameter name="0">…` strings, the ValidationError discarded a correct
    diagnosis, and the resolution read "Unable to analyze findings
    automatically"."""
    analysis = ReflectorAnalysis.model_validate({
        "hypothesis": "The external payment provider is down",
        "confidence": 0.9,
        "reasoning": "provider_down errors on every charge",
        "discrepancies": (
            '\n<parameter name="0">Kubernetes shows the pod healthy</parameter>'
            '\n<parameter name="1">A recent release is a live suspect.'
        ),
        "causal_chain": (
            '\n<parameter name="0">{"cause": "provider outage", '
            '"effect": "charges fail"}</parameter>'
            '\n<parameter name="1">{"cause": "charges fail",\n'
            '"effect": "dependency-outage signal"}'
        ),
        "evidence": (
            '\n<parameter name="0">{"source": "loki", "reference": "q", '
            '"claim": "provider_down", "observed_at": "2026-09-29T23:12:20Z"}'
        ),
    })
    assert analysis.discrepancies == [
        "Kubernetes shows the pod healthy",
        "A recent release is a live suspect.",
    ]
    assert [link.cause for link in analysis.causal_chain] == [
        "provider outage",
        "charges fail",
    ]
    assert analysis.evidence[0].observed_at == "2026-09-29T23:12:20Z"


def test_a_plain_string_is_still_rejected_where_a_list_is_required():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ReflectorAnalysis.model_validate({
            "hypothesis": "h", "confidence": 0.5, "reasoning": "r",
            "discrepancies": "just prose",
        })
