"""Unit tests for the manual Slack bot-token path (sre_agent/multitenant/slack_oauth.py)."""
from types import SimpleNamespace

from sre_agent.multitenant import slack_oauth


def test_resolve_slack_bot_token_prefers_org_token(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-static")
    org = SimpleNamespace(slack_bot_token="xoxb-org-specific")
    assert slack_oauth.resolve_slack_bot_token(org) == "xoxb-org-specific"


def test_resolve_slack_bot_token_no_env_fallback(monkeypatch):
    # No process-environment fallback: an org with no stored token has none,
    # even if SLACK_BOT_TOKEN happens to be set in the process environment.
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-static")
    org = SimpleNamespace(slack_bot_token=None)
    assert slack_oauth.resolve_slack_bot_token(org) is None
