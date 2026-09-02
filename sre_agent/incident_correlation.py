#!/usr/bin/env python3
"""Correlate concurrently-firing incidents into bundles — or keep them apart.

Phase 5 (`docs/ai/PHASE5_DETERMINISTIC_PIPELINE_PLAN.md`), step "Correlation
gate". Multiple alerts firing near-simultaneously do **not** imply one root
cause; conflating them would misdirect the fix pipeline at the wrong service.
Equally, treating every alert as independent misses the common case of one
regression cascading across dependent services.

This module is deliberately **not** another LLM call per incoming alert —
that would reintroduce exactly the improvisation the deterministic-pipeline
request objects to (see PHASE5 plan, "Industry research"). It is a bounded,
pure scoring function over three signals also used by PagerDuty's Intelligent
Alert Grouping and Datadog Watchdog: time-window proximity, service-topology
adjacency, and text similarity between the alert descriptions. Same
"pure logic, unit-testable without a DB" shape as `alert_resolution.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, FrozenSet, List, Optional, Sequence

DEFAULT_WINDOW_MINUTES = 15
DEFAULT_SIMILARITY_THRESHOLD = 0.35
# A same/adjacent-service match is strong correlation evidence on its own;
# text similarity only needs to clear a much lower bar to confirm it. An
# unrelated service pair needs the text itself to carry the case.
_TOPOLOGY_MATCH_FLOOR = 0.10

_TITLE_SERVICE_RE = re.compile(r"^\[([^\]]+)\]")
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "is", "are", "was", "were", "on", "in", "of", "to",
        "for", "and", "or", "at", "by", "with", "has", "have", "had", "this",
        "that", "it", "its", "labels", "service",
    }
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def extract_service(title: str) -> str:
    """Pull the service name out of the `[{service}] {alertname}` convention
    (`sre_agent/api/v1/alerts.py::_incident_title`). Falls back to the whole
    title, lowercased, when an incident predates or bypasses that convention.
    """
    match = _TITLE_SERVICE_RE.match(title or "")
    return (match.group(1) if match else (title or "")).strip().lower()


def _tokens(text: str) -> FrozenSet[str]:
    return frozenset(t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOPWORDS and len(t) > 2)


def jaccard_similarity(a: FrozenSet[str], b: FrozenSet[str]) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


@dataclass(frozen=True)
class CorrelationCandidate:
    """One incident to score against — either the newly-firing one or an
    already-open one it might bundle with."""

    incident_id: str
    cluster_id: str
    title: str
    description: str = ""
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class CorrelationMatch:
    incident_id: str
    score: float
    reasons: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class CorrelationResult:
    """Verdict for one newly-firing candidate against the currently-open
    incidents in its cluster."""

    decision: str  # "bundle" | "separate"
    bundle_with: Optional[str]  # incident_id, when decision == "bundle"
    best_score: float
    matches: List[CorrelationMatch] = field(default_factory=list)


def _service_adjacency_score(
    candidate_service: str, other_service: str, adjacency: Optional[Dict[str, Sequence[str]]]
) -> tuple[float, Optional[str]]:
    if candidate_service and candidate_service == other_service:
        return 1.0, f"same service ({candidate_service})"
    if adjacency:
        neighbors = set(adjacency.get(candidate_service, ())) | set(adjacency.get(other_service, ()))
        if other_service in neighbors or candidate_service in neighbors:
            return 0.6, f"adjacent services ({candidate_service} ↔ {other_service})"
    return 0.0, None


def correlate(
    candidate: CorrelationCandidate,
    open_incidents: Sequence[CorrelationCandidate],
    *,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    adjacency: Optional[Dict[str, Sequence[str]]] = None,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> CorrelationResult:
    """Score `candidate` against every other open incident in the same
    cluster and decide bundle-vs-separate.

    Caller's responsibility: `open_incidents` should already be scoped to the
    same cluster and to active-lifecycle statuses (see
    `crud.list_active_incidents_for_cluster`) and should exclude `candidate`
    itself. This function does not touch a database or an LLM — it is a pure
    scoring pass so its behavior is fully deterministic and unit-testable.
    """
    if candidate.created_at is None:
        window = None
    else:
        window = timedelta(minutes=window_minutes)

    candidate_service = extract_service(candidate.title)
    candidate_tokens = _tokens(f"{candidate.title} {candidate.description}")

    matches: List[CorrelationMatch] = []
    for other in open_incidents:
        if other.incident_id == candidate.incident_id:
            continue
        if other.cluster_id != candidate.cluster_id:
            continue
        if window is not None and other.created_at is not None:
            if abs(candidate.created_at - other.created_at) > window:
                continue

        other_service = extract_service(other.title)
        topology_score, topology_reason = _service_adjacency_score(candidate_service, other_service, adjacency)
        text_score = jaccard_similarity(candidate_tokens, _tokens(f"{other.title} {other.description}"))

        # Topology match is corroborated by even weak text overlap; with no
        # topology signal at all, text similarity has to carry the case alone.
        if topology_score > 0:
            combined = max(topology_score, text_score) if text_score >= _TOPOLOGY_MATCH_FLOOR else topology_score
        else:
            combined = text_score

        reasons = []
        if topology_reason:
            reasons.append(topology_reason)
        if text_score > 0:
            reasons.append(f"text similarity {text_score:.2f}")

        if combined >= similarity_threshold or topology_score >= 1.0:
            matches.append(CorrelationMatch(incident_id=other.incident_id, score=combined, reasons=reasons))

    if not matches:
        return CorrelationResult(decision="separate", bundle_with=None, best_score=0.0, matches=[])

    matches.sort(key=lambda m: m.score, reverse=True)
    best = matches[0]
    return CorrelationResult(decision="bundle", bundle_with=best.incident_id, best_score=best.score, matches=matches)
