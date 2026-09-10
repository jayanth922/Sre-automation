"""Slack bot-token connection: manual paste only.

An admin pastes their Slack app's Bot User OAuth Token directly (``POST
/organization/slack/bot-token``, verified via ``verify_bot_token`` below) —
stored on ``Organization`` (``slack_bot_token``/``slack_team_id``). There is
no OAuth install flow and no process-environment fallback: an organization's
Slack connection is always a token someone at that org explicitly pasted and
had verified against Slack.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)


class SlackOAuthError(RuntimeError):
    """The pasted Slack bot token failed verification."""


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
    """This organization's bot token, set explicitly via the manual-token
    Settings field. No env-var fallback: a token silently inherited from
    process environment would connect the org to whichever Slack workspace
    that variable happens to point at, without anyone at the org having
    consciously chosen it."""
    return getattr(organization, "slack_bot_token", None)
