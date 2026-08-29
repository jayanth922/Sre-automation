#!/usr/bin/env python3
"""Tests for Alertmanager resolved-alert reconciliation (R11)."""

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _load(name, rel_path):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_ar = _load("alert_resolution_under_test", "sre_agent/alert_resolution.py")
reconcile_resolved_alert = _ar.reconcile_resolved_alert
is_active_incident_status = _ar.is_active_incident_status


@pytest.mark.parametrize(
    "status,mark_resolved,masked,new_status",
    [
        ("open", True, False, "resolved"),
        ("investigating", True, False, "resolved"),
        ("investigated", True, False, "resolved"),
        ("awaiting_approval", True, False, "resolved"),
        ("remediation_in_progress", True, False, "resolved"),
        ("verification_unknown", True, False, "resolved"),
        ("remediation_failed", False, True, "remediation_failed"),
        ("resolved", False, False, None),
    ],
)
def test_reconcile_resolved_alert(status, mark_resolved, masked, new_status):
    decision = reconcile_resolved_alert(status)
    assert decision.mark_resolved is mark_resolved
    assert decision.masked_failed_remediation is masked
    assert decision.new_status == new_status
    if status == "resolved":
        assert decision.matched is False
    elif status == "remediation_failed":
        assert decision.matched is True
        assert decision.reason == "alert_cleared_but_remediation_failed"
    else:
        assert decision.matched is True


def test_active_status_helper():
    assert is_active_incident_status("awaiting_approval")
    assert is_active_incident_status("remediation_failed")
    assert not is_active_incident_status("resolved")
    assert not is_active_incident_status("not-a-status")
