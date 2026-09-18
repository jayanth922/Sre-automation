#!/usr/bin/env python3
"""Retrieval quality metrics and the runtime instrumentation around the stores.

Two things are worth pinning here beyond arithmetic: that the
no-relevant-documents case is routed to the false-positive rate instead of
being scored as a miss, and that a store which swallows its own exception (all
three of them do, so a dead index degrades an investigation instead of killing
it) still produces a recorded failure.
"""

import pytest

from sre_agent import retrieval_metrics as rm


# ---------------------------------------------------------------- ranking


def test_recall_and_precision_use_the_returned_window():
    retrieved = ["a", "b", "c"]
    assert rm.recall_at_k(retrieved, ["a", "z"], k=3) == pytest.approx(0.5)
    # Denominator is what came back, not k: abstaining is not punished.
    assert rm.precision_at_k(["a"], ["a"], k=5) == pytest.approx(1.0)
    assert rm.precision_at_k(retrieved, ["a"], k=3) == pytest.approx(1 / 3)


def test_recall_at_k_respects_the_cutoff():
    assert rm.recall_at_k(["x", "y", "a"], ["a"], k=2) == 0.0
    assert rm.recall_at_k(["x", "y", "a"], ["a"], k=3) == 1.0


def test_reciprocal_rank_rewards_the_top_position():
    assert rm.reciprocal_rank(["a", "b"], ["a"]) == pytest.approx(1.0)
    assert rm.reciprocal_rank(["b", "a"], ["a"]) == pytest.approx(0.5)
    assert rm.reciprocal_rank(["b", "c"], ["a"]) == 0.0


def test_ndcg_separates_rank_one_from_rank_five():
    first = rm.ndcg_at_k(["a", "b", "c", "d", "e"], ["a"], k=5)
    fifth = rm.ndcg_at_k(["b", "c", "d", "e", "a"], ["a"], k=5)
    assert first == pytest.approx(1.0)
    assert 0.0 < fifth < first
    # recall@5 cannot tell these apart, which is why nDCG is also reported.
    assert rm.recall_at_k(["a", "b", "c", "d", "e"], ["a"], k=5) == rm.recall_at_k(
        ["b", "c", "d", "e", "a"], ["a"], k=5
    )


def test_an_unlabeled_query_cannot_be_constructed():
    with pytest.raises(ValueError, match="unlabeled query"):
        rm.LabeledQuery(query_id="q", text="t")
    with pytest.raises(ValueError, match="expects_empty"):
        rm.LabeledQuery(query_id="q", text="t", relevant_ids=["a"], expects_empty=True)


def test_empty_probe_is_scored_as_a_false_positive_not_a_miss():
    probe = rm.LabeledQuery(query_id="p", text="t", expects_empty=True)
    clean = rm.evaluate_query(probe, [])
    dirty = rm.evaluate_query(probe, ["leaked"])

    assert clean.false_positive is False
    assert dirty.false_positive is True
    # Recall/precision are undefined here and must not be published as zeros.
    assert "recall" not in clean.as_dict()
    assert clean.as_dict()["expects_empty"] is True


def test_aggregate_keeps_the_two_populations_apart():
    scored = rm.evaluate_query(
        rm.LabeledQuery(query_id="s", text="t", relevant_ids=["a"]), ["a"]
    )
    missed = rm.evaluate_query(
        rm.LabeledQuery(query_id="m", text="t", relevant_ids=["a"]), ["z"]
    )
    leaked = rm.evaluate_query(
        rm.LabeledQuery(query_id="p", text="t", expects_empty=True), ["z"]
    )

    report = rm.aggregate("store", [scored, missed, leaked])

    assert report.scored_queries == 2
    assert report.empty_probe_queries == 1
    assert report.hit_rate == pytest.approx(0.5)
    assert report.mrr == pytest.approx(0.5)
    # The leaked probe does not dilute recall, and the misses do not dilute
    # the false-positive rate.
    assert report.recall_at_k == pytest.approx(0.5)
    assert report.false_positive_rate == pytest.approx(1.0)


# ------------------------------------------------------------- instrumentation


def test_recorder_separates_unavailable_from_empty():
    recorder = rm.RetrievalRecorder()
    recorder.record(
        rm.RetrievalEvent(store="s", available=True, returned=2, requested=5, latency_ms=1.0)
    )
    recorder.record(
        rm.RetrievalEvent(store="s", available=False, returned=0, requested=5, latency_ms=1.0)
    )
    recorder.record(
        rm.RetrievalEvent(
            store="s", available=True, returned=0, requested=5, latency_ms=1.0, error="boom"
        )
    )

    bucket = recorder.summary()["stores"]["s"]

    assert bucket["calls"] == 3
    assert bucket["empty_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert bucket["unavailable_rate"] == pytest.approx(1 / 3, abs=1e-3)
    assert bucket["error_rate"] == pytest.approx(1 / 3, abs=1e-3)


def test_recorder_ring_buffer_is_bounded():
    recorder = rm.RetrievalRecorder(max_events=3)
    for _ in range(10):
        recorder.record(
            rm.RetrievalEvent(
                store="s", available=True, returned=1, requested=1, latency_ms=0.1
            )
        )
    assert len(recorder.events()) == 3


def test_track_retrieval_records_scores_and_tenant_scope():
    recorder = rm.RetrievalRecorder()
    with rm.track_retrieval(
        "s", requested=5, threshold=0.5, tenant_scoped=True, recorder=recorder
    ) as observed:
        observed["scores"] = [0.9, 0.4]

    event = recorder.events()[0]
    assert event.returned == 2
    assert event.top_score == pytest.approx(0.9)
    assert event.min_score == pytest.approx(0.4)
    assert event.tenant_scoped is True
    assert event.error is None


def test_track_retrieval_counts_a_swallowed_failure():
    recorder = rm.RetrievalRecorder()
    with rm.track_retrieval("s", requested=5, recorder=recorder) as observed:
        observed["error"] = "RuntimeError: index gone"

    event = recorder.events()[0]
    assert event.error == "RuntimeError: index gone"
    assert event.returned == 0


def test_track_retrieval_records_then_reraises():
    recorder = rm.RetrievalRecorder()
    with pytest.raises(RuntimeError):
        with rm.track_retrieval("s", requested=5, recorder=recorder):
            raise RuntimeError("propagated")

    assert recorder.events()[0].error.startswith("RuntimeError: propagated")


def test_a_broken_recorder_cannot_fail_a_retrieval():
    class Exploding:
        def record(self, event):
            raise RuntimeError("metrics backend down")

    with rm.track_retrieval("s", requested=1, recorder=Exploding()) as observed:
        observed["scores"] = [0.5]
    # No exception: a metrics ring buffer must never take down an investigation.


def test_events_carry_no_query_or_document_text():
    recorder = rm.RetrievalRecorder()
    with rm.track_retrieval("s", requested=1, recorder=recorder) as observed:
        observed["scores"] = [0.5]

    payload = recorder.events()[0].as_dict()
    assert set(payload) == {
        "store",
        "available",
        "returned",
        "requested",
        "latency_ms",
        "top_score",
        "min_score",
        "threshold",
        "tenant_scoped",
        "error",
        "timestamp",
    }


# ----------------------------------------------------- store wiring (skills)


def _alert(name, service):
    return {"alert_name": name, "labels": {"service": service}}


def test_skill_store_recall_is_instrumented_once(monkeypatch):
    from sre_agent import skill_store as ss

    store = ss.InMemorySkillStore()
    ss.record_successful_remediation(
        store,
        _alert("CheckoutHighErrorRate", "checkout-service"),
        [{"action_type": "rollback", "target": "checkout-service"}],
        "inc-1",
        organization_id="org-a",
    )

    recorder = rm.RetrievalRecorder()
    monkeypatch.setattr(rm, "get_retrieval_recorder", lambda: recorder)
    hits = ss.propose_skills(
        store,
        _alert("CheckoutHighErrorRate", "checkout-service"),
        organization_id="org-a",
    )

    assert len(hits) == 1
    events = recorder.events()
    assert len(events) == 1, "semantic subclass must not double-count via super()"
    assert events[0].store == rm.SKILL_STORE
    assert events[0].returned == 1
    assert events[0].tenant_scoped is True


# ----------------------------------------------------- store wiring (memory)


def _memory_store_with_mocks():
    from unittest.mock import MagicMock

    from sre_agent.memory_store import MemoryStore

    store = MemoryStore.__new__(MemoryStore)
    store.collection_name = "sre_incidents_v2"
    store.client = MagicMock()
    store.embedding_available = True
    return store


def test_memory_search_records_scores_and_tenant_scope(monkeypatch):
    from unittest.mock import MagicMock

    from sre_agent import memory_store as ms

    recorder = rm.RetrievalRecorder()
    monkeypatch.setattr(rm, "get_retrieval_recorder", lambda: recorder)
    monkeypatch.setattr(ms, "embed_text", lambda text: [0.1, 0.2])

    store = _memory_store_with_mocks()
    store.client.query_points.return_value = MagicMock(
        points=[MagicMock(score=0.9, payload={"incident_id": "inc-1", "incident_text": "t"})]
    )

    store.search_similar_incidents("symptoms", organization_id="org-a")

    event = recorder.events()[0]
    assert event.store == rm.MEMORY_STORE
    assert event.available is True
    assert event.tenant_scoped is True
    assert event.returned == 1


def test_memory_search_records_an_unavailable_store(monkeypatch):
    recorder = rm.RetrievalRecorder()
    monkeypatch.setattr(rm, "get_retrieval_recorder", lambda: recorder)

    store = _memory_store_with_mocks()
    store.client = None

    assert store.search_similar_incidents("symptoms") == []

    event = recorder.events()[0]
    # The distinction this whole module exists for: "could not ask" is not
    # "asked and this incident is novel".
    assert event.available is False
    assert event.returned == 0


def test_memory_search_records_a_swallowed_query_failure(monkeypatch):
    from sre_agent import memory_store as ms

    recorder = rm.RetrievalRecorder()
    monkeypatch.setattr(rm, "get_retrieval_recorder", lambda: recorder)
    monkeypatch.setattr(ms, "embed_text", lambda text: [0.1, 0.2])

    store = _memory_store_with_mocks()
    store.client.query_points.side_effect = RuntimeError("collection vanished")

    assert store.search_similar_incidents("symptoms") == []

    event = recorder.events()[0]
    assert event.available is True
    assert event.error and "collection vanished" in event.error


# ---------------------------------------------------- store wiring (runbooks)


def test_runbook_search_is_instrumented(monkeypatch):
    from unittest.mock import MagicMock

    from sre_agent import runbook_index as ri

    recorder = rm.RetrievalRecorder()
    monkeypatch.setattr(rm, "get_retrieval_recorder", lambda: recorder)
    monkeypatch.setattr(ri, "embed_text", lambda text: [0.1, 0.2])

    index = ri.RunbookIndex.__new__(ri.RunbookIndex)
    index.collection_name = "sre_runbooks_v1"
    index.client = MagicMock()
    index.embedding_available = True
    index.client.query_points.return_value = MagicMock(
        points=[MagicMock(score=0.8, payload={"runbook_id": "rb-1", "title": "t"})]
    )

    index.search("checkout pods crashlooping", organization_id="org-a")

    event = recorder.events()[0]
    assert event.store == rm.RUNBOOK_INDEX
    assert event.returned == 1
    assert event.top_score == pytest.approx(0.8)
    assert event.tenant_scoped is True
