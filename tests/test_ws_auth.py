#!/usr/bin/env python3
"""Focused tests for authenticated, organization-scoped WebSockets (T03)."""

from pathlib import Path

import asyncio

from sre_agent.ws_auth import event_visible_to_org, org_id_matches, validate_ws_ticket

_ROOT = Path(__file__).resolve().parents[1]


def test_missing_ticket_is_rejected_without_decoding():
    def decoder(_ticket):
        raise AssertionError("missing tickets must not be decoded")

    assert validate_ws_ticket(None, decoder=decoder) is None


def test_expired_or_invalid_ticket_is_rejected():
    assert validate_ws_ticket("expired", decoder=lambda _ticket: None) is None


def test_non_websocket_access_token_is_rejected():
    payload = {"purpose": "access", "org_id": "org-a"}
    assert validate_ws_ticket("token", decoder=lambda _ticket: payload) is None


def test_ticket_without_organization_is_rejected():
    payload = {"purpose": "ws"}
    assert validate_ws_ticket("token", decoder=lambda _ticket: payload) is None


def test_valid_websocket_ticket_returns_claims():
    payload = {"purpose": "ws", "org_id": "org-a", "user_id": "user-a"}
    assert validate_ws_ticket("token", decoder=lambda _ticket: payload) == payload


def test_organization_match_fails_closed():
    assert org_id_matches("org-a", "org-a") is True
    assert org_id_matches("org-b", "org-a") is False
    assert org_id_matches(None, "org-a") is False


def test_global_feed_event_filter_enforces_organization_scope():
    async def incident_org(incident_id):
        return {"inc-a": "org-a", "inc-b": "org-b"}.get(incident_id)

    async def cluster_org(cluster_id):
        return {"cluster-a": "org-a", "cluster-b": "org-b"}.get(cluster_id)

    async def visible(event):
        return await event_visible_to_org(
            event, "org-a", incident_org, cluster_org
        )

    assert asyncio.run(visible({"org_id": "org-a"})) is True
    assert asyncio.run(visible({"payload": {"org_id": "org-b"}})) is False
    assert asyncio.run(visible({"incident_id": "inc-a"})) is True
    assert asyncio.run(visible({"incident_id": "inc-b"})) is False
    assert asyncio.run(visible({"payload": {"cluster_id": "cluster-a"}})) is True
    assert asyncio.run(visible({"payload": {"cluster_id": "cluster-b"}})) is False
    assert asyncio.run(visible({"type": "unscoped"})) is False


def test_ticket_endpoint_is_authenticated_short_lived_and_not_cached():
    src = (_ROOT / "sre_agent" / "api" / "v1" / "ws_tickets.py").read_text()
    assert "dependencies=[Depends(get_current_user_and_org)]" in src
    assert "WS_TICKET_TTL_SECONDS = 45" in src
    assert '"purpose": WS_TICKET_PURPOSE' in src
    assert 'response.headers["Cache-Control"] = "no-store"' in src


def test_runtime_authenticates_all_websocket_handlers_and_filters_global_feeds():
    src = (_ROOT / "sre_agent" / "agent_runtime.py").read_text()
    for function_name in ("ws_incident", "ws_insights", "ws_incidents"):
        start = src.index(f"async def {function_name}(")
        next_route = src.find("\n@app.", start)
        block = src[start : next_route if next_route != -1 else len(src)]
        assert "await _authenticate_websocket(websocket)" in block
    assert "await _incident_org_id(incident_id)" in src
    assert src.count("event_visible_to_org(") >= 2


def test_dashboard_mints_a_fresh_ticket_inside_every_connect_attempt():
    src = (_ROOT / "dashboard" / "lib" / "useLiveStream.ts").read_text()
    connect_start = src.index("const connect = async () =>")
    connect_end = src.index("\n        void connect()", connect_start)
    ticket_call = src.index('api.post<WsTicketResponse>("/ws-tickets")')
    websocket_call = src.index("new WebSocket(`", ticket_call)
    assert connect_start < ticket_call < websocket_call < connect_end
    assert "encodeURIComponent(ticket)" in src[ticket_call:connect_end]


def test_helm_rejects_missing_or_placeholder_signing_key():
    src = (
        _ROOT / "deploy" / "helm" / "sentinel" / "templates" / "secret.yaml"
    ).read_text()
    assert 'eq $secretKey "change-me-to-a-long-random-string"' in src
    assert 'fail "secrets.secretKey must be set' in src
