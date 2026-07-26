#!/usr/bin/env python3
"""Unit tests for the Severity Engine (ACT phase). Pure logic, no infra deps."""

import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "sre_agent" / "severity_engine.py"
_spec = importlib.util.spec_from_file_location("severity_engine", _MODULE_PATH)
severity_engine = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = severity_engine
_spec.loader.exec_module(severity_engine)

IncidentSignals = severity_engine.IncidentSignals
Severity = severity_engine.Severity
classify_severity = severity_engine.classify_severity
is_low_severity = severity_engine.is_low_severity


def test_total_outage_is_sev1():
    """High impact + high urgency = the most critical band."""
    signals = IncidentSignals(
        affected_services=5, user_facing=True, revenue_impacting=True,
        error_rate=0.9, slo_breached=True,
        slo_burn_rate=20.0, saturation=0.9, still_escalating=True,
    )
    assert classify_severity(signals).severity is Severity.SEV1


def test_minor_contained_blip_is_low_severity():
    """Low impact + low urgency = SEV4 (autonomously remediable band)."""
    signals = IncidentSignals(
        affected_services=1, user_facing=False, error_rate=0.02, slo_breached=False,
        slo_burn_rate=0.5, saturation=0.1, still_escalating=False,
    )
    a = classify_severity(signals)
    assert a.severity is Severity.SEV4
    assert is_low_severity(a.severity)


def test_high_impact_low_urgency_is_mid():
    """Big blast radius but not escalating → moderate, not critical."""
    signals = IncidentSignals(
        affected_services=5, user_facing=True, revenue_impacting=True,
        error_rate=0.8, slo_breached=True,
        slo_burn_rate=0.0, saturation=0.0, still_escalating=False,
    )
    a = classify_severity(signals)
    assert a.impact_bucket == "high"
    assert a.urgency_bucket == "low"
    assert a.severity is Severity.SEV3


def test_low_confidence_rounds_severity_up():
    """'When unsure, round up': low hypothesis confidence escalates severity."""
    base_signals = IncidentSignals(
        affected_services=2, error_rate=0.3, slo_breached=False,
        slo_burn_rate=5.0, saturation=0.4,
        hypothesis_confidence=1.0,
    )
    confident = classify_severity(base_signals)

    unsure = classify_severity(
        IncidentSignals(**{**base_signals.__dict__, "hypothesis_confidence": 0.2})
    )
    assert unsure.rounded_up is True
    assert int(unsure.severity) == int(confident.severity) - 1  # one level more critical


def test_roundup_clamps_at_sev1():
    """Escalation never goes past SEV1."""
    signals = IncidentSignals(
        affected_services=5, user_facing=True, revenue_impacting=True,
        error_rate=1.0, slo_breached=True,
        slo_burn_rate=30.0, saturation=1.0, still_escalating=True,
        hypothesis_confidence=0.1,
    )
    assert classify_severity(signals).severity is Severity.SEV1


def test_scores_are_in_unit_interval():
    a = classify_severity(IncidentSignals(error_rate=0.5, slo_burn_rate=7.0))
    assert 0.0 <= a.impact_score <= 1.0
    assert 0.0 <= a.urgency_score <= 1.0


def test_autonomy_band():
    """SEV3/SEV4 are autonomous-eligible; SEV1/SEV2 are not (default threshold)."""
    assert is_low_severity(Severity.SEV4)
    assert is_low_severity(Severity.SEV3)
    assert not is_low_severity(Severity.SEV2)
    assert not is_low_severity(Severity.SEV1)


def test_partial_signals_do_not_crash():
    """Engine degrades gracefully on empty/partial input."""
    a = classify_severity(IncidentSignals())
    assert a.severity in (Severity.SEV3, Severity.SEV4)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
