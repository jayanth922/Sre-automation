#!/usr/bin/env python3
"""Unit tests for on-call routing (design slice #3)."""

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "sre_agent" / "oncall.py"
_spec = importlib.util.spec_from_file_location("oncall", _MODULE_PATH)
oc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = oc
_spec.loader.exec_module(oc)


def test_current_oncall_rotates_by_period():
    rotation = ["alice", "bob", "carol"]
    anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # day 0 → alice, day 1 → bob, day 2 → carol, day 3 → alice (wraps)
    assert oc.current_oncall(rotation, datetime(2026, 1, 1, 6, tzinfo=timezone.utc), 24, anchor) == "alice"
    assert oc.current_oncall(rotation, datetime(2026, 1, 2, 6, tzinfo=timezone.utc), 24, anchor) == "bob"
    assert oc.current_oncall(rotation, datetime(2026, 1, 3, 6, tzinfo=timezone.utc), 24, anchor) == "carol"
    assert oc.current_oncall(rotation, datetime(2026, 1, 4, 6, tzinfo=timezone.utc), 24, anchor) == "alice"


def test_current_oncall_empty_is_none():
    assert oc.current_oncall([], datetime.now(timezone.utc)) is None


def test_resolve_oncall_from_env(monkeypatch):
    monkeypatch.delenv("PAGERDUTY_API_KEY", raising=False)
    monkeypatch.setenv("ONCALL_ROTATION", "alice, bob")
    monkeypatch.setenv("ONCALL_PERIOD_HOURS", "24")
    who = oc.resolve_oncall(now=datetime(2026, 1, 2, 6, tzinfo=timezone.utc))
    assert who == "bob"


def test_format_slack_mention():
    assert oc.format_slack_mention("U01ABC2DEF") == "<@U01ABC2DEF>"  # member id
    assert oc.format_slack_mention("alice") == "@alice"
    assert oc.format_slack_mention("<@U999>") == "<@U999>"
    assert oc.format_slack_mention(None) == "@on-call"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
