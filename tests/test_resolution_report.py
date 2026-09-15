#!/usr/bin/env python3
"""Unit tests for the resolution report."""

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "sre_agent" / "resolution_report.py"
_spec = importlib.util.spec_from_file_location("resolution_report", _MODULE_PATH)
rr = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rr
_spec.loader.exec_module(rr)


@dataclass
class FakeAlert:
    alert_name: str
    labels: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeReflector:
    hypothesis: str = "A bad deploy raised the error rate."


def _state():
    return {
        "alert_context": FakeAlert("CheckoutHighErrorRate", {"service": "checkout-service"}),
        "reflector_analysis": FakeReflector(),
    }


def test_report_includes_issue_root_cause_and_actions():
    act_report = {
        "severity": "SEV2",
        "aggregate_decision": "autonomous",
        "executed": [{"action_type": "rollback", "target": "checkout-service", "command": "kubectl rollout undo ..."}],
    }
    verification = {"status": "RESOLVED", "detail": "current 0.01 < threshold 0.05"}
    report = rr.build_resolution_report(_state(), act_report, verification=verification)
    md = report["markdown"]
    assert "CheckoutHighErrorRate" in md
    assert "bad deploy" in md.lower()
    assert "rollback" in md
    assert "✅ RESOLVED" in md
    assert report["resolved"] is True


def test_report_includes_sandbox_tested_code_fix():
    act_report = {"severity": "SEV2", "aggregate_decision": "autonomous", "executed": []}
    code_fix = {"status": "TESTED_PASS", "diff": "--- a/app.py\n+++ b/app.py\n-bug\n+fix"}
    report = rr.build_resolution_report(_state(), act_report, verification={"status": "RESOLVED", "detail": ""}, code_fix=code_fix)
    md = report["markdown"]
    assert "Suggested code fix" in md
    assert "sandbox-tested ✅ PASS" in md
    assert "apply on your side" in md.lower()
    assert "```diff" in md


def test_report_high_severity_held_for_approval():
    act_report = {"severity": "SEV1", "aggregate_decision": "requires_approval", "executed": []}
    report = rr.build_resolution_report(_state(), act_report, verification=None)
    assert "Held for human approval" in report["markdown"]
    assert report["resolved"] is False


def test_externally_cleared_report_does_not_present_dry_run_as_a_proposal():
    act_report = {
        "severity": "SEV2",
        "aggregate_decision": "requires_approval",
        "executed": [
            {
                "action_type": "restart",
                "target": "checkout-service",
                "command": "kubectl rollout restart deployment/checkout-service",
            }
        ],
        "remediation_suppressed": {"reason": "incident_resolved"},
    }

    report = rr.build_resolution_report(_state(), act_report)

    assert report["actions_applied"] == []
    assert "no approval was requested and no live write ran" in report["markdown"]
    assert "Held for human approval" not in report["markdown"]


# ---------------------------------------------------------------------------
# A code fix that produced no patch
#
# Live on 2026-09-14, incident f8ca9a54: the root cause was a genuine
# ValueError at app.py:147, the sandbox returned INCONCLUSIVE with an empty
# diff, and the report rendered
#
#     **Suggested code fix (sandbox verification ℹ️ INCONCLUSIVE) — apply on
#     your side:**
#
#     **Next steps:** Please review — the incident may need manual attention.
#
# — a header promising a patch, followed by nothing. It reads as a truncated
# message and sends the on-call looking for a diff that was never written.
# The `detail` field already held the reason and was simply never rendered.
# ---------------------------------------------------------------------------

_NO_PATCH_REASON = (
    "Proposed fix is missing sandbox verification parameters "
    "(runner image/failure signature, or a patch without matching "
    "baseline/candidate commands); skipping sandbox run."
)


def _no_patch_report(status="INCONCLUSIVE", detail=_NO_PATCH_REASON, resolved=False):
    act_report = {"severity": "UNKNOWN", "aggregate_decision": "blocked", "executed": []}
    verification = {"status": "RESOLVED", "detail": ""} if resolved else None
    return rr.build_resolution_report(
        _state(),
        act_report,
        verification=verification,
        code_fix={"status": status, "detail": detail, "diff": ""},
    )["markdown"]


def test_an_empty_code_fix_does_not_promise_a_patch():
    md = _no_patch_report()
    assert "apply on your side" not in md.lower()
    assert "no patch produced" in md.lower()


def test_the_recorded_reason_for_having_no_patch_is_shown():
    """`detail` is the only place the system explains itself here."""
    md = _no_patch_report()
    assert "missing sandbox verification parameters" in md


def test_next_steps_does_not_point_at_a_fix_that_is_not_there():
    md = _no_patch_report()
    assert "Review the suggested code fix above" not in md
    assert "needs a human to write the fix" in md


def test_a_resolved_incident_with_no_patch_is_not_told_to_apply_one():
    md = _no_patch_report(resolved=True)
    assert "System state is back to normal." in md
    assert "apply it to prevent recurrence" not in md


def test_a_patch_that_does_exist_is_still_offered_for_the_human_to_apply():
    """The fix must not suppress the case it was never about."""
    act_report = {"severity": "SEV2", "aggregate_decision": "autonomous", "executed": []}
    md = rr.build_resolution_report(
        _state(),
        act_report,
        verification={"status": "RESOLVED", "detail": ""},
        code_fix={"status": "TESTED_PASS", "diff": "--- a/app.py\n+fix"},
    )["markdown"]
    assert "Suggested code fix" in md
    assert "apply it to prevent recurrence" in md
    assert "no patch produced" not in md.lower()


def test_an_in_flight_sandbox_run_still_says_it_is_running():
    md = _no_patch_report(status="VERIFYING", detail="")
    assert "in progress" in md
    assert "isolated sandbox" in md


def test_the_pipeline_statuses_are_not_rendered_as_raw_tokens():
    """graph_builder emits these two; they were missing from the label map."""
    assert "awaiting start-fix approval" in _no_patch_report(status="AWAITING_START_FIX")
    assert "generating a patch" in _no_patch_report(status="GENERATING_PATCH")


def test_no_recorded_reason_says_so_rather_than_going_blank():
    md = _no_patch_report(status="INCONCLUSIVE", detail="")
    assert "No reason was recorded" in md
    assert "a human has to write this fix" in md


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
