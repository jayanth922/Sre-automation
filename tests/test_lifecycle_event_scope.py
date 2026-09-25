#!/usr/bin/env python3
"""Incident lifecycle events must say which cluster they belong to.

`INCIDENTS_LIFECYCLE_CHANNEL` is shared across an organization. `org_id` gets
an event as far as the right tenant -- `ws_auth.event_visible_to_org` is
fail-closed and that part was never broken -- but an organization with two
clusters has one feed for both, and the console's incident toasts are mounted
on every cluster page. Without `cluster_id` in the payload there is nothing
for the client to filter on, so it showed a sibling cluster's alert name and
summary and linked to a URL that 404s.

These tests pin the payload. The client-side half is pinned in
`test_console_wiring.py`.
"""

import asyncio

from sre_agent import live_events


class _Recorder:
    """Stands in for the bus. Records what would have gone out."""

    def __init__(self):
        self.published = []

    async def publish(self, channel, event):
        self.published.append((channel, event))


def _publish(**kwargs):
    bus = _Recorder()
    asyncio.run(
        live_events.publish_lifecycle_event(
            "opened",
            incident_id="11111111-1111-1111-1111-111111111111",
            alert_name="HighErrorRate",
            bus=bus,
            **kwargs,
        )
    )
    assert len(bus.published) == 1
    channel, event = bus.published[0]
    return channel, event["payload"]


def test_a_lifecycle_event_carries_its_cluster():
    channel, payload = _publish(
        org_id="org-1", cluster_id="22222222-2222-2222-2222-222222222222"
    )
    assert channel == live_events.INCIDENTS_LIFECYCLE_CHANNEL
    assert payload["cluster_id"] == "22222222-2222-2222-2222-222222222222"


def test_a_uuid_cluster_id_is_stringified_for_the_wire():
    """Call sites pass `uuid.UUID`; JSON on the socket needs a string."""
    import uuid

    cid = uuid.uuid4()
    _, payload = _publish(org_id="org-1", cluster_id=cid)
    assert payload["cluster_id"] == str(cid)


def test_an_event_without_a_cluster_omits_the_key_rather_than_faking_one():
    """No key is honest; an empty string would silently match nothing forever.

    The console drops an event it cannot place, so the absence is what makes
    the client's fail-closed branch reachable and testable.
    """
    _, payload = _publish(org_id="org-1")
    assert "cluster_id" not in payload


def test_the_rest_of_the_payload_is_unchanged():
    """The toast reads alert_name and summary; the fix must not disturb them."""
    _, payload = _publish(org_id="org-1", cluster_id="c1")
    assert payload["incident_id"] == "11111111-1111-1111-1111-111111111111"
    assert payload["alert_name"] == "HighErrorRate"
    assert payload["summary"] == "Investigating alert: HighErrorRate"


def test_every_live_call_site_passes_a_cluster():
    """A producer that supports the field but is never given it fixes nothing.

    All three call sites take `cluster_id` as a required parameter, so this
    reads the source rather than the signature: the point is that the argument
    is actually threaded through, not merely available.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    sites = 0
    for rel in ("src/sre_agent/agent_runtime.py", "src/sre_agent/approval_flow.py"):
        tree = ast.parse((root / rel).read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "publish_lifecycle_event"
            ):
                sites += 1
                assert any(
                    k.arg == "cluster_id" for k in node.keywords
                ), f"{rel}:{node.lineno} publishes a lifecycle event with no cluster"
    assert sites == 3, f"expected 3 live call sites, found {sites}"
