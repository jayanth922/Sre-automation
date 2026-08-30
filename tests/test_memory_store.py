"""Tests for sre_agent.memory_store.

Covers:
- the org_id/cluster_id tenant filter (cross-tenant leak fix)
- structured, separately-embedded fields (symptoms/root_cause/resolution)
- recency-decayed ranking
- cross-incident back-links computed at store time
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from sre_agent.memory_store import MemoryStore, _point_id, _recency_decay


def _store_with_mocks():
    """Build a MemoryStore bypassing __init__'s real Qdrant/fastembed calls."""
    store = MemoryStore.__new__(MemoryStore)
    store.collection_name = "sre_incidents_v2"
    store.client = MagicMock()
    store.embedding_available = True
    return store


def _point(incident_id, score, payload_extra=None, stored_at=None):
    payload = {"incident_id": incident_id, "incident_text": f"text for {incident_id}"}
    if stored_at is not None:
        payload["stored_at"] = stored_at
    if payload_extra:
        payload.update(payload_extra)
    return MagicMock(score=score, payload=payload)


@patch("sre_agent.memory_store.embed_text", return_value=[0.1, 0.2])
def test_store_incident_writes_structured_fields_and_tenant_ids(mock_embed):
    store = _store_with_mocks()
    store.client.query_points.return_value = MagicMock(points=[])  # no related incidents

    store.store_incident(
        "inc-1",
        symptoms="pods crashlooping",
        root_cause="OOM in payments service",
        resolution="raised memory limit",
        metadata={"alert_name": "PodCrashLooping"},
        organization_id="org-a",
        cluster_id="cluster-1",
    )

    upsert_kwargs = store.client.upsert.call_args.kwargs
    point = upsert_kwargs["points"][0]
    assert point.id == _point_id("inc-1")
    assert point.vector == {
        "symptoms": [0.1, 0.2],
        "root_cause": [0.1, 0.2],
        "resolution": [0.1, 0.2],
    }
    assert point.payload["organization_id"] == "org-a"
    assert point.payload["cluster_id"] == "cluster-1"
    assert point.payload["symptoms"] == "pods crashlooping"
    assert point.payload["root_cause"] == "OOM in payments service"
    assert point.payload["resolution"] == "raised memory limit"
    assert point.payload["alert_name"] == "PodCrashLooping"
    assert "Symptoms:" in point.payload["incident_text"]
    assert point.payload["related_incident_ids"] == []


@patch("sre_agent.memory_store.embed_text", return_value=[0.1, 0.2])
def test_store_incident_omits_tenant_ids_when_not_provided(mock_embed):
    store = _store_with_mocks()
    store.client.query_points.return_value = MagicMock(points=[])

    store.store_incident("inc-1", symptoms="s", root_cause="r", resolution="res")

    point = store.client.upsert.call_args.kwargs["points"][0]
    assert "organization_id" not in point.payload
    assert "cluster_id" not in point.payload


@patch("sre_agent.memory_store.embed_text", return_value=[0.1, 0.2])
def test_store_incident_does_not_mutate_caller_metadata_dict(mock_embed):
    store = _store_with_mocks()
    store.client.query_points.return_value = MagicMock(points=[])
    metadata = {"resolution": "note"}

    store.store_incident(
        "inc-1", symptoms="s", root_cause="r", resolution="res", metadata=metadata
    )

    assert metadata == {"resolution": "note"}


@patch("sre_agent.memory_store.embed_text", return_value=[0.1, 0.2])
def test_store_incident_finds_and_backlinks_related_incidents(mock_embed):
    store = _store_with_mocks()
    store.client.query_points.return_value = MagicMock(
        points=[_point("inc-old", 0.9), _point("inc-1", 0.99)]  # self must be excluded
    )
    store.client.retrieve.return_value = [
        MagicMock(payload={"related_incident_ids": []})
    ]

    store.store_incident("inc-1", symptoms="s", root_cause="r", resolution="res")

    # self-match filtered out of related_incident_ids
    point = store.client.upsert.call_args.kwargs["points"][0]
    assert point.payload["related_incident_ids"] == ["inc-old"]

    # back-link written onto the related incident's own payload
    set_payload_kwargs = store.client.set_payload.call_args.kwargs
    assert set_payload_kwargs["points"] == [_point_id("inc-old")]
    assert set_payload_kwargs["payload"]["related_incident_ids"] == ["inc-1"]


@patch("sre_agent.memory_store.embed_text", return_value=[0.1, 0.2])
def test_store_incident_related_limit_zero_skips_lookup(mock_embed):
    store = _store_with_mocks()

    store.store_incident(
        "inc-1", symptoms="s", root_cause="r", resolution="res", related_limit=0
    )

    store.client.query_points.assert_not_called()
    point = store.client.upsert.call_args.kwargs["points"][0]
    assert point.payload["related_incident_ids"] == []


@patch("sre_agent.memory_store.embed_text", return_value=[0.1, 0.2])
def test_search_similar_incidents_builds_tenant_filter_for_every_field(mock_embed):
    store = _store_with_mocks()
    store.client.query_points.return_value = MagicMock(points=[])

    store.search_similar_incidents("crashloop", organization_id="org-a", cluster_id="cluster-1")

    assert store.client.query_points.call_count == 3
    used_fields = set()
    for call in store.client.query_points.call_args_list:
        used_fields.add(call.kwargs["using"])
        query_filter = call.kwargs["query_filter"]
        conditions = {c.key: c.match.value for c in query_filter.must}
        assert conditions == {"organization_id": "org-a", "cluster_id": "cluster-1"}
    assert used_fields == {"symptoms", "root_cause", "resolution"}


@patch("sre_agent.memory_store.embed_text", return_value=[0.1, 0.2])
def test_search_similar_incidents_no_filter_without_tenant_ids(mock_embed):
    store = _store_with_mocks()
    store.client.query_points.return_value = MagicMock(points=[])

    store.search_similar_incidents("crashloop")

    for call in store.client.query_points.call_args_list:
        assert call.kwargs["query_filter"] is None


@patch("sre_agent.memory_store.embed_text", return_value=[0.1, 0.2])
def test_search_similar_incidents_dedups_by_incident_id_keeping_best_score(mock_embed):
    store = _store_with_mocks()
    now = datetime.now(timezone.utc).isoformat()
    store.client.query_points.side_effect = [
        MagicMock(points=[_point("inc-1", 0.7, stored_at=now)]),  # symptoms
        MagicMock(points=[_point("inc-1", 0.95, stored_at=now)]),  # root_cause (best)
        MagicMock(points=[_point("inc-1", 0.6, stored_at=now)]),  # resolution
    ]

    results = store.search_similar_incidents("crashloop")

    assert len(results) == 1
    assert results[0]["incident_id"] == "inc-1"
    assert results[0]["similarity_score"] == 0.95


@patch("sre_agent.memory_store.embed_text", return_value=[0.1, 0.2])
def test_search_similar_incidents_recency_decay_reorders_equal_scores(mock_embed):
    store = _store_with_mocks()
    now = datetime.now(timezone.utc)
    fresh = now.isoformat()
    stale = (now - timedelta(days=365)).isoformat()
    store.client.query_points.side_effect = [
        MagicMock(points=[_point("inc-stale", 0.8, stored_at=stale)]),
        MagicMock(points=[_point("inc-fresh", 0.8, stored_at=fresh)]),
        MagicMock(points=[]),
    ]

    results = store.search_similar_incidents("crashloop", limit=2)

    assert [r["incident_id"] for r in results] == ["inc-fresh", "inc-stale"]


@patch("sre_agent.memory_store.embed_text", return_value=[0.1, 0.2])
def test_search_similar_incidents_surfaces_related_incident_ids(mock_embed):
    store = _store_with_mocks()
    store.client.query_points.side_effect = [
        MagicMock(points=[_point("inc-1", 0.9, payload_extra={"related_incident_ids": ["inc-0"]})]),
        MagicMock(points=[]),
        MagicMock(points=[]),
    ]

    results = store.search_similar_incidents("crashloop")

    assert results[0]["related_incident_ids"] == ["inc-0"]


def test_point_id_is_stable_across_calls():
    assert _point_id("inc-1") == _point_id("inc-1")
    assert _point_id("inc-1") != _point_id("inc-2")


def test_recency_decay_bounds():
    now = datetime.now(timezone.utc)
    assert _recency_decay(None, now, 30) == 1.0
    assert _recency_decay(now.isoformat(), now, 30) == 1.0
    half_life_ago = (now - timedelta(days=30)).isoformat()
    assert abs(_recency_decay(half_life_ago, now, 30) - 0.5) < 1e-9


def test_format_similar_incidents_includes_related_incidents():
    store = MemoryStore.__new__(MemoryStore)
    formatted = store.format_similar_incidents_for_prompt([
        {
            "incident_id": "inc-1",
            "incident_text": "desc",
            "similarity_score": 0.9,
            "related_incident_ids": ["inc-0"],
            "metadata": {"resolution": "restarted pod"},
        }
    ])
    assert "**Related Incidents**: inc-0" in formatted
    assert "**Resolution**: restarted pod" in formatted
