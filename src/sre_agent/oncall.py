#!/usr/bin/env python3
"""
On-call routing (design slice #3).

When the monitor flags something, notify the *right* person, not just a channel.
This resolves who's on call now and formats a Slack mention. Two backends: a
static rotation (env ``ONCALL_ROTATION``, rotated by ``ONCALL_PERIOD_HOURS``) and
an optional PagerDuty lookup (guarded — used only if configured). The rotation
math is pure and unit-tested.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import List, Optional

logger = logging.getLogger(__name__)

_ANCHOR = datetime(2026, 1, 1, tzinfo=timezone.utc)  # rotation epoch


def current_oncall(
    rotation: List[str], now: Optional[datetime] = None,
    period_hours: float = 24.0, anchor: Optional[datetime] = None,
) -> Optional[str]:
    """Who is on call now, given a rotation list and a rotation period. Pure."""
    if not rotation:
        return None
    now = now or datetime.now(timezone.utc)
    anchor = anchor or _ANCHOR
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    elapsed_hours = (now - anchor).total_seconds() / 3600.0
    idx = int(elapsed_hours // period_hours) % len(rotation)
    return rotation[idx]


def _rotation_from_env() -> List[str]:
    return [h.strip() for h in os.getenv("ONCALL_ROTATION", "").split(",") if h.strip()]


def resolve_oncall(now: Optional[datetime] = None) -> Optional[str]:
    """Resolve the current on-call handle: PagerDuty if configured, else rotation."""
    if os.getenv("PAGERDUTY_API_KEY") and os.getenv("PAGERDUTY_SCHEDULE_ID"):
        try:
            return _pagerduty_oncall()
        except Exception as e:  # pragma: no cover - network
            logger.warning(f"PagerDuty lookup failed ({e}); falling back to rotation")
    period = float(os.getenv("ONCALL_PERIOD_HOURS", "24"))
    return current_oncall(_rotation_from_env(), now=now, period_hours=period)


def _pagerduty_oncall() -> Optional[str]:  # pragma: no cover - requires PagerDuty
    import httpx

    key = os.getenv("PAGERDUTY_API_KEY")
    schedule_id = os.getenv("PAGERDUTY_SCHEDULE_ID")
    r = httpx.get(
        f"https://api.pagerduty.com/oncalls",
        params={"schedule_ids[]": schedule_id},
        headers={"Authorization": f"Token token={key}", "Accept": "application/json"},
        timeout=15,
    )
    r.raise_for_status()
    oncalls = r.json().get("oncalls", [])
    if oncalls:
        return oncalls[0].get("user", {}).get("summary")
    return None


import re as _re

# Slack member IDs look like U01ABC2DEF / W0123ABCD (start U or W, then uppercase alnum).
_SLACK_ID = _re.compile(r"^[UW][A-Z0-9]{7,}$")


def format_slack_mention(handle: Optional[str]) -> str:
    """Format an on-call handle as a Slack mention (member-id → <@id>, else @handle)."""
    if not handle:
        return "@on-call"
    if handle.startswith("<@") or handle.startswith("@"):
        return handle
    if _SLACK_ID.match(handle):
        return f"<@{handle}>"
    return f"@{handle}"
