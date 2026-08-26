#!/usr/bin/env python3
"""WebSocket ticket validation (PR-T03).

Browsers can't attach an ``Authorization`` header to a WebSocket upgrade, so
the dashboard mints a short-lived, purpose-scoped ticket over a normal
authenticated HTTP request first (``POST /api/v1/ws-tickets``, see
``sre_agent/api/v1/ws_tickets.py``) and passes it as ``?ticket=...`` on the
WS URL. ``agent_runtime.py``'s WS handlers validate it here before accepting
the connection.

Pure logic only (plus the one ``decode_access_token`` call, the same JWT
codec ``auth_deps.get_current_user_and_org`` uses) — no FastAPI/DB coupling,
so it's directly unit-testable without importing the app.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional

WS_TICKET_PURPOSE = "ws"


def validate_ws_ticket(
    ticket: Optional[str],
    decoder: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
) -> Optional[Dict[str, Any]]:
    """Decode and validate a short-lived ws ticket.

    Returns the claims dict (guaranteed to include a non-empty ``org_id``) on
    success, or ``None`` if the ticket is missing, has an invalid or expired
    signature, or isn't purpose-scoped for websocket auth.
    """
    if not ticket:
        return None
    if decoder is None:
        # Keep the JWT dependency lazy so this module's validation logic can be
        # tested without importing the full authentication stack.
        from backend.auth import decode_access_token

        decoder = decode_access_token
    payload = decoder(ticket)
    if not payload:
        return None
    if payload.get("purpose") != WS_TICKET_PURPOSE:
        return None
    if not payload.get("org_id"):
        return None
    return payload


def org_id_matches(candidate_org_id: Optional[Any], org_id: str) -> bool:
    """Whether a resolved resource's org id matches the ticket holder's org.

    ``candidate_org_id`` is ``None`` when the resource (an incident, for a
    cluster-wide feed's event) couldn't be resolved at all — that must not
    match anything, since the caller's org is unknown either way.
    """
    return candidate_org_id is not None and str(candidate_org_id) == str(org_id)


async def event_visible_to_org(
    event: Dict[str, Any],
    org_id: str,
    incident_org_resolver: Callable[[Any], Awaitable[Optional[Any]]],
    cluster_org_resolver: Callable[[Any], Awaitable[Optional[Any]]],
) -> bool:
    """Fail-closed organization filter for process-global live-feed events."""
    if not isinstance(event, dict):
        return False
    payload = event.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    # Producers may stamp an already-authorized org directly. Existing
    # incident lifecycle events instead carry an incident id, resolved below.
    event_org_id = event.get("org_id") or payload.get("org_id")
    if event_org_id is not None:
        return org_id_matches(event_org_id, org_id)

    incident_id = event.get("incident_id") or payload.get("incident_id")
    if incident_id is not None:
        return org_id_matches(await incident_org_resolver(incident_id), org_id)

    cluster_id = event.get("cluster_id") or payload.get("cluster_id")
    if cluster_id is not None:
        return org_id_matches(await cluster_org_resolver(cluster_id), org_id)

    # Unscoped events must never leak through a tenant-scoped feed.
    return False
