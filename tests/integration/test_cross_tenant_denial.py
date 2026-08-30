"""Cross-tenant denial for cluster/incident ownership helpers."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from sre_agent.ws_auth import event_visible_to_org, org_id_matches


def _deny_cross_tenant_cluster(cluster, caller_org_id: str) -> bool:
    """Mirror the ownership check used by CRUD update/delete paths."""
    return bool(cluster) and str(cluster.org_id) == str(caller_org_id)


@pytest.mark.integration
def test_cross_tenant_cluster_mutation_is_denied(twin_tenants, org_scoped_incident):
    cluster_a = SimpleNamespace(id=twin_tenants.a.cluster_id, org_id=twin_tenants.a.org_id)
    assert _deny_cross_tenant_cluster(cluster_a, twin_tenants.a.org_id) is True
    assert _deny_cross_tenant_cluster(cluster_a, twin_tenants.b.org_id) is False

    incident_a = org_scoped_incident(twin_tenants.a)
    assert org_id_matches(incident_a.org_id, twin_tenants.a.org_id)
    assert not org_id_matches(incident_a.org_id, twin_tenants.b.org_id)


@pytest.mark.integration
def test_cross_tenant_websocket_events_are_filtered(twin_tenants):
    async def incident_org(incident_id):
        return {
            "inc-a": twin_tenants.a.org_id,
            "inc-b": twin_tenants.b.org_id,
        }.get(incident_id)

    async def cluster_org(cluster_id):
        return {
            twin_tenants.a.cluster_id: twin_tenants.a.org_id,
            twin_tenants.b.cluster_id: twin_tenants.b.org_id,
        }.get(cluster_id)

    async def visible_to_a(event):
        return await event_visible_to_org(
            event, twin_tenants.a.org_id, incident_org, cluster_org
        )

    assert asyncio.run(visible_to_a({"org_id": twin_tenants.a.org_id})) is True
    assert asyncio.run(visible_to_a({"org_id": twin_tenants.b.org_id})) is False
    assert asyncio.run(visible_to_a({"incident_id": "inc-b"})) is False
    assert asyncio.run(
        visible_to_a({"payload": {"cluster_id": twin_tenants.b.cluster_id}})
    ) is False
    assert asyncio.run(visible_to_a({"type": "unscoped"})) is False
