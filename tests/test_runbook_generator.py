#!/usr/bin/env python3
"""Unit tests for generative runbooks (project #5)."""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio  # noqa: E402

from sre_agent.runbook_generator import (  # noqa: E402
    RunbookInput,
    generate_runbook_llm,
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
    md = generate_runbook_markdown(_inp(verification_status="RESOLVED"))
    assert md.startswith("---\n")
    fm_text = md.split("---", 2)[1]
    fm = yaml.safe_load(fm_text)
    # The runbooks server indexes these keys for search.
    assert fm["alert_name"] == "CheckoutHighErrorRate"
    assert fm["service"] == "checkout-service"
    assert fm["status"] == "Verified success"
    assert "verified-success" in fm["tags"]

    negative = yaml.safe_load(
        generate_runbook_markdown(_inp(verification_status="dry_run")).split("---", 2)[1]
    )
    assert negative["status"] == "Negative exemplar"
    assert "negative-exemplar" in negative["tags"]
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


class _FakeLLM:
    def __init__(self, content, fail=False):
        self._content = content
        self._fail = fail

    async def ainvoke(self, messages):
        if self._fail:
            raise RuntimeError("llm down")
        class R:
            content = self._content
        return R()


def test_generate_runbook_llm_uses_model_body():
    body = "## Summary\nGenerated summary.\n## Root cause\nA bad deploy.\n## Remediation\nrollback."
    md = asyncio.run(generate_runbook_llm(_inp(), _FakeLLM(body)))
    assert "Generated summary." in md
    assert md.startswith("---\n")  # frontmatter preserved
    import yaml
    assert yaml.safe_load(md.split("---", 2)[1])["alert_name"] == "CheckoutHighErrorRate"


def test_generate_runbook_llm_falls_back_on_failure():
    md = asyncio.run(generate_runbook_llm(_inp(), _FakeLLM("", fail=True)))
    # Deterministic template fallback still produces a valid runbook.
    assert "## Summary" in md and "kubectl rollout undo" in md


@dataclass
class FakeExecutionContext:
    credentials: Dict[str, str] = field(default_factory=dict)


def test_write_runbook_skips_without_notion_config():
    published = asyncio.run(write_runbook(_inp(), execution_context=None))
    assert published is None

    published = asyncio.run(write_runbook(_inp(), execution_context=FakeExecutionContext()))
    assert published is None


def test_write_runbook_publishes_to_notion(monkeypatch):
    calls = []

    async def fake_upsert(api_key, database_id, *, title, markdown_body, service, incident_type, severity):
        calls.append(
            dict(
                api_key=api_key, database_id=database_id, title=title,
                service=service, incident_type=incident_type, severity=severity,
            )
        )
        return {"id": "notion-page-id", "path": "https://notion.so/notion-page-id"}

    monkeypatch.setattr("sre_agent.notion_runbooks.upsert_notion_runbook", fake_upsert)

    ctx = FakeExecutionContext(credentials={"notion_api_key": "secret", "notion_database_id": "db-1"})
    published = asyncio.run(write_runbook(_inp(), execution_context=ctx))

    assert published == "https://notion.so/notion-page-id"
    assert len(calls) == 1
    assert calls[0]["api_key"] == "secret"
    assert calls[0]["database_id"] == "db-1"
    assert calls[0]["service"] == "checkout-service"
    assert calls[0]["incident_type"] == "high_error_rate"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
