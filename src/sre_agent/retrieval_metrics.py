#!/usr/bin/env python3
"""
Retrieval quality: measuring what the memory, skill, and runbook indexes return.

Sentinel's pitch is that it learns — past incidents, verified skills, and
runbooks are embedded and recalled into each investigation. Until now nothing
measured whether recall actually *worked*. A store that was unreachable, a
tenant filter that matched nothing, an embedding model that silently changed
dimensions: all three degrade to "returns zero results", and zero results is
indistinguishable from "nothing relevant exists" at every call site. The
learning claim was unfalsifiable.

Two distinct things live here, and conflating them would be the easy lie:

* **Runtime instrumentation** (:class:`RetrievalRecorder`). Label-free. Counts
  calls, empty results, store unavailability, result counts, score
  distribution, and latency per store. It cannot tell you whether what came
  back was *relevant* — nobody labeled production traffic — but it does tell
  you the index is alive, scoped, and returning things at plausible scores.
  Most real retrieval failures are visible here.
* **Offline ranking metrics** (:func:`evaluate_query` and friends). Require
  labeled queries: a query plus the ids that genuinely should come back.
  These give recall@k, precision@k, MRR and nDCG@k — the numbers that support
  a quality claim — and are only as honest as the labels behind them.

Deliberately separated because the second is expensive to produce truthfully.
`evals/benchmarks/retrieval_eval.py` supplies labels that are *derived* (exact
self-retrieval, tenant isolation, distractor rejection) rather than asserted,
so nothing here depends on hand-waved relevance judgments.

One case needs naming because standard formulations get it wrong: a query with
**no** relevant documents. Recall and precision are undefined there, and
scoring it as 0.0 or skipping it both mislead. For an SRE agent it is the most
important case — retrieving a confident, irrelevant past incident is worse
than retrieving nothing, because the model will reason from it. Those queries
are tracked separately as :attr:`RetrievalReport.false_positive_rate`.

Pure stdlib, no I/O, no provider imports.
"""

from __future__ import annotations

import logging
import math
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

# Store names. Fixed set so dashboards and the release artifact agree on keys.
MEMORY_STORE = "incident_memory"
SKILL_STORE = "verified_skills"
RUNBOOK_INDEX = "runbooks"


# ==========================================================================
# Offline ranking metrics (labeled)
# ==========================================================================


def _rank_of_first_relevant(retrieved: Sequence[str], relevant: Iterable[str]) -> Optional[int]:
    relevant_set = set(relevant)
    for position, item in enumerate(retrieved, start=1):
        if item in relevant_set:
            return position
    return None


def recall_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """Fraction of the relevant items that appear in the top k.

    Undefined with no relevant items; returns 0.0 so callers that ignore the
    empty case cannot silently score it as perfect. Use
    :func:`evaluate_query`, which routes those queries to the false-positive
    rate instead.
    """
    relevant_set = set(relevant)
    if not relevant_set:
        return 0.0
    found = relevant_set & set(retrieved[:k])
    return len(found) / len(relevant_set)


def precision_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """Fraction of the top k that is relevant.

    The denominator is what was actually returned, not k. Scoring a retriever
    that honestly returned 2 results as 2/10 would punish it for abstaining,
    which is the behavior we want when little is relevant.
    """
    window = retrieved[:k]
    if not window:
        return 0.0
    return len(set(window) & set(relevant)) / len(window)


def reciprocal_rank(retrieved: Sequence[str], relevant: Iterable[str]) -> float:
    """1/rank of the first relevant hit; 0.0 if none.

    The metric that matters most in practice: a specialist reads the top result
    far more carefully than the fifth.
    """
    rank = _rank_of_first_relevant(retrieved, relevant)
    return 1.0 / rank if rank else 0.0


def ndcg_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """Binary-gain nDCG@k — rank-sensitive, unlike recall@k.

    Distinguishes "the right incident was first" from "the right incident was
    fifth, under four confident distractors", which recall@5 scores
    identically.
    """
    relevant_set = set(relevant)
    if not relevant_set:
        return 0.0
    dcg = sum(
        1.0 / math.log2(position + 1)
        for position, item in enumerate(retrieved[:k], start=1)
        if item in relevant_set
    )
    ideal = sum(
        1.0 / math.log2(position + 1)
        for position in range(1, min(k, len(relevant_set)) + 1)
    )
    return dcg / ideal if ideal else 0.0


@dataclass
class LabeledQuery:
    """One query with its ground truth.

    `relevant_ids` empty means "nothing should come back" — a distractor probe,
    not a missing label. Say so explicitly with ``expects_empty=True`` so a
    genuinely unlabeled query can never be scored by accident.
    """

    query_id: str
    text: str
    relevant_ids: List[str] = field(default_factory=list)
    expects_empty: bool = False
    organization_id: Optional[str] = None
    cluster_id: Optional[str] = None
    note: str = ""

    def __post_init__(self) -> None:
        if self.expects_empty and self.relevant_ids:
            raise ValueError(
                f"{self.query_id}: expects_empty is set but relevant_ids is non-empty"
            )
        if not self.expects_empty and not self.relevant_ids:
            raise ValueError(
                f"{self.query_id}: no relevant_ids and expects_empty is not set — "
                "an unlabeled query must not be scored as a miss"
            )


@dataclass
class QueryResult:
    query_id: str
    retrieved_ids: List[str]
    recall: Optional[float] = None
    precision: Optional[float] = None
    reciprocal_rank: Optional[float] = None
    ndcg: Optional[float] = None
    expects_empty: bool = False
    false_positive: bool = False

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "query_id": self.query_id,
            "retrieved": len(self.retrieved_ids),
            "expects_empty": self.expects_empty,
        }
        if self.expects_empty:
            payload["false_positive"] = self.false_positive
        else:
            payload.update(
                recall=round(self.recall or 0.0, 4),
                precision=round(self.precision or 0.0, 4),
                reciprocal_rank=round(self.reciprocal_rank or 0.0, 4),
                ndcg=round(self.ndcg or 0.0, 4),
            )
        return payload


def evaluate_query(query: LabeledQuery, retrieved_ids: Sequence[str], k: int = 5) -> QueryResult:
    """Score one query, routing the no-relevant-documents case correctly."""
    retrieved = list(retrieved_ids)
    if query.expects_empty:
        return QueryResult(
            query_id=query.query_id,
            retrieved_ids=retrieved,
            expects_empty=True,
            false_positive=bool(retrieved[:k]),
        )
    return QueryResult(
        query_id=query.query_id,
        retrieved_ids=retrieved,
        recall=recall_at_k(retrieved, query.relevant_ids, k),
        precision=precision_at_k(retrieved, query.relevant_ids, k),
        reciprocal_rank=reciprocal_rank(retrieved, query.relevant_ids),
        ndcg=ndcg_at_k(retrieved, query.relevant_ids, k),
    )


@dataclass
class RetrievalReport:
    """Macro-averaged scores over a labeled query set.

    Macro, not micro: every query counts once regardless of how many relevant
    documents it has, so one incident with twelve near-duplicates cannot carry
    the score for a set where everything else fails.
    """

    store: str
    k: int
    scored_queries: int
    empty_probe_queries: int
    recall_at_k: float
    precision_at_k: float
    mrr: float
    ndcg_at_k: float
    hit_rate: float
    false_positive_rate: float
    per_query: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "store": self.store,
            "k": self.k,
            "scored_queries": self.scored_queries,
            "empty_probe_queries": self.empty_probe_queries,
            "recall_at_k": round(self.recall_at_k, 4),
            "precision_at_k": round(self.precision_at_k, 4),
            "mrr": round(self.mrr, 4),
            "ndcg_at_k": round(self.ndcg_at_k, 4),
            "hit_rate": round(self.hit_rate, 4),
            "false_positive_rate": round(self.false_positive_rate, 4),
            "per_query": list(self.per_query),
        }


def _mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def aggregate(store: str, results: Sequence[QueryResult], k: int = 5) -> RetrievalReport:
    """Roll query results into a report, keeping the two populations apart."""
    scored = [r for r in results if not r.expects_empty]
    probes = [r for r in results if r.expects_empty]
    return RetrievalReport(
        store=store,
        k=k,
        scored_queries=len(scored),
        empty_probe_queries=len(probes),
        recall_at_k=_mean([r.recall or 0.0 for r in scored]),
        precision_at_k=_mean([r.precision or 0.0 for r in scored]),
        mrr=_mean([r.reciprocal_rank or 0.0 for r in scored]),
        ndcg_at_k=_mean([r.ndcg or 0.0 for r in scored]),
        hit_rate=_mean([1.0 if (r.reciprocal_rank or 0.0) > 0 else 0.0 for r in scored]),
        false_positive_rate=_mean([1.0 if r.false_positive else 0.0 for r in probes]),
        per_query=[r.as_dict() for r in results],
    )


# ==========================================================================
# Runtime instrumentation (label-free)
# ==========================================================================


@dataclass
class RetrievalEvent:
    """One retrieval call.

    Carries no query text, no document text, and no ids — those are tenant
    data, and a metrics ring buffer is the wrong place for them (see the audit
    redaction work). Everything here is a shape or a number.
    """

    store: str
    available: bool
    returned: int
    requested: int
    latency_ms: float
    top_score: Optional[float] = None
    min_score: Optional[float] = None
    threshold: Optional[float] = None
    tenant_scoped: bool = False
    error: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def as_dict(self) -> Dict[str, Any]:
        return {
            "store": self.store,
            "available": self.available,
            "returned": self.returned,
            "requested": self.requested,
            "latency_ms": round(self.latency_ms, 2),
            "top_score": round(self.top_score, 4) if self.top_score is not None else None,
            "min_score": round(self.min_score, 4) if self.min_score is not None else None,
            "threshold": self.threshold,
            "tenant_scoped": self.tenant_scoped,
            "error": self.error,
            "timestamp": self.timestamp,
        }


def _percentile(values: List[float], fraction: float) -> float:
    """Nearest-rank percentile. Exact on small samples, unlike interpolation."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


class RetrievalRecorder:
    """In-process ring of retrieval events plus a per-store rollup."""

    def __init__(self, max_events: int = 2000) -> None:
        self._events: List[RetrievalEvent] = []
        self._max = max_events

    def record(self, event: RetrievalEvent) -> None:
        self._events.append(event)
        if len(self._events) > self._max:
            self._events = self._events[-self._max :]

    def events(self) -> List[RetrievalEvent]:
        return list(self._events)

    def reset(self) -> None:
        self._events = []

    def summary(self, store: Optional[str] = None) -> Dict[str, Any]:
        """Per-store call counts, empty rate, availability, scores, latency.

        `empty_rate` is the number to watch. An index that silently stops
        matching — wrong tenant filter, re-embedded collection, dimension
        change — looks exactly like a quiet week of novel incidents at every
        call site. Here the two are distinguishable, because
        `unavailable_rate` and `error_rate` separate "could not ask" from
        "asked and got nothing".
        """
        source = [e for e in self._events if store is None or e.store == store]
        stores: Dict[str, Dict[str, Any]] = {}
        for event in source:
            bucket = stores.setdefault(
                event.store,
                {
                    "calls": 0,
                    "unavailable": 0,
                    "errors": 0,
                    "empty": 0,
                    "tenant_scoped": 0,
                    "_returned": [],
                    "_top_scores": [],
                    "_latency": [],
                },
            )
            bucket["calls"] += 1
            if not event.available:
                bucket["unavailable"] += 1
            if event.error:
                bucket["errors"] += 1
            if event.returned == 0:
                bucket["empty"] += 1
            if event.tenant_scoped:
                bucket["tenant_scoped"] += 1
            bucket["_returned"].append(float(event.returned))
            bucket["_latency"].append(event.latency_ms)
            if event.top_score is not None:
                bucket["_top_scores"].append(event.top_score)

        for bucket in stores.values():
            calls = bucket["calls"] or 1
            returned = bucket.pop("_returned")
            top_scores = bucket.pop("_top_scores")
            latency = bucket.pop("_latency")
            bucket["empty_rate"] = round(bucket["empty"] / calls, 3)
            bucket["unavailable_rate"] = round(bucket["unavailable"] / calls, 3)
            bucket["error_rate"] = round(bucket["errors"] / calls, 3)
            bucket["tenant_scoped_rate"] = round(bucket["tenant_scoped"] / calls, 3)
            bucket["avg_returned"] = round(_mean(returned), 2)
            bucket["avg_top_score"] = round(_mean(top_scores), 4) if top_scores else None
            bucket["p50_latency_ms"] = round(_percentile(latency, 0.50), 2)
            bucket["p95_latency_ms"] = round(_percentile(latency, 0.95), 2)

        total_calls = sum(b["calls"] for b in stores.values())
        total_empty = sum(b["empty"] for b in stores.values())
        return {
            "stores": stores,
            "total_calls": total_calls,
            "total_empty": total_empty,
            "empty_rate": round(total_empty / total_calls, 3) if total_calls else 0.0,
            "note": (
                "Call-shape metrics only. Relevance is not measured here — it "
                "needs labeled queries (evals/benchmarks/retrieval_eval.py)."
            ),
        }


_GLOBAL_RECORDER: Optional[RetrievalRecorder] = None


def get_retrieval_recorder() -> RetrievalRecorder:
    global _GLOBAL_RECORDER
    if _GLOBAL_RECORDER is None:
        _GLOBAL_RECORDER = RetrievalRecorder()
    return _GLOBAL_RECORDER


@contextmanager
def track_retrieval(
    store: str,
    *,
    requested: int,
    threshold: Optional[float] = None,
    tenant_scoped: bool = False,
    available: bool = True,
    recorder: Optional[RetrievalRecorder] = None,
):
    """Time a retrieval call and record its shape.

    Yields a small dict; set ``["scores"]`` to the returned similarity scores
    before the block exits::

        with track_retrieval(MEMORY_STORE, requested=5) as observed:
            hits = index.search(...)
            observed["scores"] = [h["similarity_score"] for h in hits]

    Every store here catches its own exceptions and returns ``[]`` so a dead
    index degrades an investigation instead of killing it — which is right, and
    is also exactly how a permanently broken index stays invisible. Set
    ``observed["error"]`` in that handler so the failure is still counted. An
    exception that does propagate is recorded and re-raised.

    Recording must never be able to fail a retrieval, so a broken recorder is
    swallowed: a metrics ring buffer that takes down an investigation is worse
    than no metrics.
    """
    sink = recorder if recorder is not None else get_retrieval_recorder()
    observed: Dict[str, Any] = {"scores": [], "error": None}
    start = time.perf_counter()

    def _emit(error: Optional[str]) -> None:
        try:
            error = error or observed.get("error")
            scores = [float(s) for s in (observed.get("scores") or [])]
            sink.record(
                RetrievalEvent(
                    store=store,
                    available=available,
                    returned=len(scores),
                    requested=requested,
                    latency_ms=(time.perf_counter() - start) * 1000.0,
                    top_score=max(scores) if scores else None,
                    min_score=min(scores) if scores else None,
                    threshold=threshold,
                    tenant_scoped=tenant_scoped,
                    error=error,
                )
            )
        except Exception:  # pragma: no cover - defensive
            logger.debug("retrieval metrics not recorded", exc_info=True)

    try:
        yield observed
    except Exception as exc:
        _emit(f"{type(exc).__name__}: {exc}")
        raise
    else:
        _emit(None)
