"""Authenticated WebSocket ticket isolation across twin tenants."""

from __future__ import annotations

import pytest

from sre_agent.ws_auth import validate_ws_ticket


@pytest.mark.integration
def test_ws_ticket_binds_org_and_rejects_foreign_org(twin_tenants, ws_ticket_for):
    ticket_a = ws_ticket_for(twin_tenants.a)
    claims = validate_ws_ticket("tok-a", decoder=lambda _t: ticket_a)
    assert claims is not None
    assert claims["org_id"] == twin_tenants.a.org_id
    assert claims["org_id"] != twin_tenants.b.org_id


@pytest.mark.integration
def test_access_token_cannot_impersonate_websocket_ticket(twin_tenants):
    payload = {
        "purpose": "access",
        "org_id": twin_tenants.a.org_id,
        "user_id": twin_tenants.a.user_id,
    }
    assert validate_ws_ticket("tok", decoder=lambda _t: payload) is None


@pytest.mark.integration
def test_missing_org_on_ws_ticket_fails_closed():
    assert validate_ws_ticket("tok", decoder=lambda _t: {"purpose": "ws"}) is None
