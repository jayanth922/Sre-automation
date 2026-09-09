"""Slack OAuth ("Add to Slack") per-workspace bot token issuance.

Replaces the single global ``SLACK_BOT_TOKEN``/``SLACK_APP_TOKEN`` env-var
model — one Sentinel Slack app, hand-registered per self-hosted deployment —
with a real OAuth install flow: one Sentinel-owned Slack app, installed by
each customer's own workspace, each installation getting its own bot token
stored on ``Organization`` (``slack_bot_token``/``slack_team_id``) instead of
in process environment.

A self-hosted deployment that skips the OAuth flow instead pastes its Slack
app's bot token directly (``POST /organization/slack/bot-token``, verified via
``verify_bot_token`` below) — stored the same way, on ``Organization``. There
is no process-environment fallback: an organization's Slack connection is
always something someone at that org explicitly set.
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

SLACK_OAUTH_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
SLACK_OAUTH_ACCESS_URL = "https://slack.com/api/oauth.v2.access"

# Bot-token scopes: read mentions, post messages, read thread history in the
# channel types war-room threads use. Overridable for deployments that need
# a narrower or wider scope set approved by their Slack admin.
DEFAULT_SCOPES = "app_mentions:read,chat:write,channels:history,groups:history,im:history"


class SlackOAuthError(RuntimeError):
    """Slack app OAuth is not configured, or the exchange failed."""


def slack_oauth_configured() -> bool:
    return bool(
        os.getenv("SLACK_CLIENT_ID", "").strip()
        and os.getenv("SLACK_CLIENT_SECRET", "").strip()
    )


def generate_state() -> str:
    """An opaque, unguessable value the caller must persist and verify on
    callback — the standard OAuth CSRF defense."""
    return secrets.token_urlsafe(32)


def build_install_url(state: str, *, redirect_uri: Optional[str] = None) -> str:
    client_id = os.getenv("SLACK_CLIENT_ID", "").strip()
    if not client_id:
        raise SlackOAuthError("SLACK_CLIENT_ID is not configured")
    params: Dict[str, str] = {
        "client_id": client_id,
        "scope": os.getenv("SLACK_OAUTH_SCOPES", DEFAULT_SCOPES),
        "state": state,
    }
    if redirect_uri:
        params["redirect_uri"] = redirect_uri
    return f"{SLACK_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code_for_token(
    code: str, *, redirect_uri: Optional[str] = None
) -> Dict[str, Any]:
    """Exchange a one-time OAuth code for this workspace's bot token."""
    client_id = os.getenv("SLACK_CLIENT_ID", "").strip()
    client_secret = os.getenv("SLACK_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise SlackOAuthError("SLACK_CLIENT_ID / SLACK_CLIENT_SECRET are not configured")

    data = {"client_id": client_id, "client_secret": client_secret, "code": code}
    if redirect_uri:
        data["redirect_uri"] = redirect_uri

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(SLACK_OAUTH_ACCESS_URL, data=data)
        resp.raise_for_status()
        payload = resp.json()

    if not payload.get("ok"):
        raise SlackOAuthError(
            f"Slack OAuth exchange failed: {payload.get('error', 'unknown_error')}"
        )
    bot_token = payload.get("access_token")
    team = payload.get("team") or {}
    if not bot_token or not team.get("id"):
        raise SlackOAuthError("Slack OAuth response is missing access_token/team.id")
    return {"bot_token": bot_token, "team_id": team["id"], "team_name": team.get("name")}


async def verify_bot_token(bot_token: str) -> Dict[str, Any]:
    """Confirm a manually-pasted bot token actually authenticates, the same
    way the OAuth exchange's response is validated, so a typo or revoked
    token fails loudly at save time instead of silently at incident time."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {bot_token}"},
        )
        resp.raise_for_status()
        payload = resp.json()

    if not payload.get("ok"):
        raise SlackOAuthError(
            f"Slack rejected this token: {payload.get('error', 'unknown_error')}"
        )
    team_id = payload.get("team_id")
    if not team_id:
        raise SlackOAuthError("Slack auth.test response is missing team_id")
    return {"team_id": team_id, "team_name": payload.get("team")}


def resolve_slack_bot_token(organization: Any) -> Optional[str]:
    """This organization's bot token, set explicitly via the OAuth install
    flow or the manual-token Settings field. No env-var fallback: a token
    silently inherited from process environment would connect the org to
    whichever Slack workspace that variable happens to point at, without
    anyone at the org having consciously chosen it."""
    return getattr(organization, "slack_bot_token", None)
