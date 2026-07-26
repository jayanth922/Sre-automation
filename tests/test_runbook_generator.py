#!/usr/bin/env python3
"""Unit tests for generative runbooks (project #5)."""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sre_agent.runbook_generator import (  # noqa: E402
    RunbookInput,
    generate_runbook_markdown,
    input_from_act,
    runbook_filename,
    runbook_id,
    write_runbook,
)


def _inp(**kw):
    base = dict(
        alert_name="CheckoutHighErrorRate", service="checkout-service",
        failure_class="high_error_rate", severity="SEV2", severity_label="critical",
        hypothesis="Bad deploy raised the error rate.", confidence=0.82,
        actions=[{"action_type": "rollback", "target": "checkout-service"}],
        namespace="demo-app", incident_id="inc-1", skill_id="skill-high_error_rate-checkout-service",
    )
    base.update(kw)
    return RunbookInput(**base)


def test_runbook_id_and_filename_stable():
    inp = _inp()
    assert runbook_id(inp) == "RB-AUTO-high_error_rate-checkout-service"
    assert runbook_filename(inp) == "RB-AUTO-high_error_rate-checkout-service.md"


def test_frontmatter_is_valid_yaml_with_search_keys():
    md = generate_runbook_markdown(_inp())
    assert md.startswith("---\n")
    fm_text = md.split("---", 2)[1]
    fm = yaml.safe_load(fm_text)
    # The runbooks server indexes these keys for search.
    assert fm["alert_name"] == "CheckoutHighErrorRate"
    assert fm["service"] == "checkout-service"
    assert fm["status"] == "Auto-generated"
    assert "high_error_rate" in fm["tags"]


def test_body_has_expected_sections_and_command():
    md = generate_runbook_markdown(_inp())
    for section in ("## Summary", "## Symptoms", "## Root cause", "## Remediation", "## Verification", "## Prevention"):
        assert section in md
    assert "kubectl rollout undo deployment/checkout-service -n demo-app" in md
    assert "skill-high_error_rate-checkout-service" in md


def test_confidence_optional():
    md = generate_runbook_markdown(_inp(confidence=None))
    assert "## Root cause" in md  # still renders without confidence


def test_no_actions_renders_manual_step():
    md = generate_runbook_markdown(_inp(actions=[]))
    assert "investigate manually" in md.lower()


# ── input_from_act extraction ────────────────────────────────────────────────

@dataclass
class FakeAlert:
    severity: str
    labels: Dict[str, Any] = field(default_factory=dict)
    alert_name: str = "CheckoutHighErrorRate"


@dataclass
class FakeReport:
    severity: str = "SEV2"
    executed: List[Dict[str, Any]] = field(default_factory=list)
    action_reports: List[Dict[str, Any]] = field(default_factory=list)


def test_input_from_act_extracts_fields():
    state = {
        "alert_context": FakeAlert("critical", {"service": "checkout-service", "namespace": "demo-app"}),
        "reflector_analysis": {"hypothesis": "bad deploy", "confidence": 0.9},
        "incident_id": "inc-9",
    }
    report = FakeReport(severity="SEV2", executed=[{"action_type": "rollback", "target": "checkout-service"}])
    inp = input_from_act(state, report, skill_id="skill-x")
    assert inp.service == "checkout-service"
    assert inp.failure_class == "high_error_rate"
    assert inp.severity == "SEV2"
    assert inp.actions[0]["action_type"] == "rollback"
    assert inp.skill_id == "skill-x"


def test_write_runbook_to_tmp(tmp_path):
    path = write_runbook(_inp(), target_dir=tmp_path)
    assert path.exists()
    fm = yaml.safe_load(path.read_text().split("---", 2)[1])
    assert fm["runbook_id"] == "RB-AUTO-high_error_rate-checkout-service"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
