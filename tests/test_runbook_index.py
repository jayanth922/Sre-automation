"""Tests for sre_agent.runbook_index.

Covers:
- the org_id/cluster_id tenant filter (mirrors the memory_store leak fix)
- indexing writes the expected payload/vector
- search result shape and score threshold plumbing
"""

from unittest.mock import MagicMock, patch

from sre_agent.runbook_index import RunbookIndex, _point_id, format_runbooks_for_prompt


def _index_with_mocks():
    """Build a RunbookIndex bypassing __init__'s real Qdrant/fastembed calls."""
    index = RunbookIndex.__new__(RunbookIndex)
    index.collection_name = "sre_runbooks_v1"
    index.client = MagicMock()
    index.embedding_available = True
    return index


def _point(runbook_id, score, payload_extra=None):
    payload = {"runbook_id": runbook_id, "title": f"title for {runbook_id}", "content": "body"}
    if payload_extra:
        payload.update(payload_extra)
    return MagicMock(score=score, payload=payload)


@patch("sre_agent.runbook_index.embed_text", return_value=[0.1, 0.2])
def test_index_runbook_writes_payload_and_tenant_ids(mock_embed):
    index = _index_with_mocks()

    index.index_runbook(
        "RB-AUTO-oom-payments",
        title="OOM remediation for payments",
        content="## Summary\n...",
        service="payments",
        incident_type="oom",
        severity="SEV2",
        url="https://notion.so/xyz",
        organization_id="org-a",
        cluster_id="cluster-1",
    )

    upsert_kwargs = index.client.upsert.call_args.kwargs
    point = upsert_kwargs["points"][0]
    assert point.id == _point_id("RB-AUTO-oom-payments")
    assert point.vector == [0.1, 0.2]
    assert point.payload["runbook_id"] == "RB-AUTO-oom-payments"
    assert point.payload["title"] == "OOM remediation for payments"
    assert point.payload["organization_id"] == "org-a"
    assert point.payload["cluster_id"] == "cluster-1"
    assert point.payload["url"] == "https://notion.so/xyz"


@patch("sre_agent.runbook_index.embed_text", return_value=[0.1, 0.2])
def test_index_runbook_omits_tenant_ids_when_not_provided(mock_embed):
    index = _index_with_mocks()

    index.index_runbook("rb-1", title="t", content="c")

    point = index.client.upsert.call_args.kwargs["points"][0]
    assert "organization_id" not in point.payload
    assert "cluster_id" not in point.payload


def test_index_runbook_unavailable_returns_false_without_touching_client():
    index = _index_with_mocks()
    index.embedding_available = False

    assert index.index_runbook("rb-1", title="t", content="c") is False
    index.client.upsert.assert_not_called()


@patch("sre_agent.runbook_index.embed_text", return_value=[0.1, 0.2])
def test_search_builds_tenant_filter(mock_embed):
    index = _index_with_mocks()
    index.client.query_points.return_value = MagicMock(points=[])

    index.search("crashloop", organization_id="org-a", cluster_id="cluster-1")

    query_filter = index.client.query_points.call_args.kwargs["query_filter"]
    conditions = {c.key: c.match.value for c in query_filter.must}
    assert conditions == {"organization_id": "org-a", "cluster_id": "cluster-1"}


@patch("sre_agent.runbook_index.embed_text", return_value=[0.1, 0.2])
def test_search_no_filter_without_tenant_ids(mock_embed):
    index = _index_with_mocks()
    index.client.query_points.return_value = MagicMock(points=[])

    index.search("crashloop")

    assert index.client.query_points.call_args.kwargs["query_filter"] is None


@patch("sre_agent.runbook_index.embed_text", return_value=[0.1, 0.2])
def test_search_returns_expected_shape(mock_embed):
    index = _index_with_mocks()
    index.client.query_points.return_value = MagicMock(
        points=[_point("rb-1", 0.83, {"service": "payments"})]
    )

    results = index.search("oom in payments")

    assert len(results) == 1
    assert results[0]["runbook_id"] == "rb-1"
    assert results[0]["similarity_score"] == 0.83
    assert results[0]["service"] == "payments"


def test_search_unavailable_returns_empty_without_touching_client():
    index = _index_with_mocks()
    index.embedding_available = False

    assert index.search("q") == []
    index.client.query_points.assert_not_called()


def test_point_id_is_stable_across_calls():
    assert _point_id("rb-1") == _point_id("rb-1")
    assert _point_id("rb-1") != _point_id("rb-2")


def test_format_runbooks_for_prompt_empty():
    assert format_runbooks_for_prompt([]) == ""


def test_format_runbooks_for_prompt_includes_title_and_content():
    formatted = format_runbooks_for_prompt(
        [{"title": "OOM remediation", "content": "raise memory limit", "similarity_score": 0.9}]
    )
    assert "OOM remediation" in formatted
    assert "raise memory limit" in formatted
    assert "90.00%" in formatted
