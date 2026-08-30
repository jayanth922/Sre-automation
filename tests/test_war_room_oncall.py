"""Unit coverage for war-room on-call mention wiring (P10 integration of oncall)."""

from __future__ import annotations

import os

from sre_agent.war_room_service import _opening_text


def test_opening_text_includes_oncall_mention(monkeypatch):
    monkeypatch.setenv("ONCALL_ROTATION", "alice,bob")
    monkeypatch.setenv("ONCALL_PERIOD_HOURS", "24")
    monkeypatch.delenv("PAGERDUTY_API_KEY", raising=False)
    text = _opening_text("checkout errors")
    assert "Incident opened" in text
    assert "checkout errors" in text
    assert "On-call:" in text
    assert "@alice" in text or "@bob" in text


def test_opening_text_works_without_oncall(monkeypatch):
    monkeypatch.delenv("ONCALL_ROTATION", raising=False)
    monkeypatch.delenv("PAGERDUTY_API_KEY", raising=False)
    text = _opening_text("noise")
    assert "noise" in text
    assert "On-call:" not in text
