#!/usr/bin/env python3
"""Tests for concurrent-incident correlation/bundling (Phase 5, correlation gate)."""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _load(name, rel_path):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_ic = _load("incident_correlation_under_test", "sre_agent/incident_correlation.py")
CorrelationCandidate = _ic.CorrelationCandidate
correlate = _ic.correlate
extract_service = _ic.extract_service
jaccard_similarity = _ic.jaccard_similarity

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)


def _incident(id_, title, description="", minutes_ago=0, cluster="c1"):
    return CorrelationCandidate(
        incident_id=id_,
        cluster_id=cluster,
        title=title,
        description=description,
        created_at=NOW - timedelta(minutes=minutes_ago),
    )


def test_extract_service_parses_bracket_convention():
    assert extract_service("[checkout-service] HighErrorRate") == "checkout-service"


def test_extract_service_falls_back_to_whole_title():
    assert extract_service("legacy incident with no brackets") == "legacy incident with no brackets"


def test_jaccard_similarity_identical_and_disjoint():
    assert jaccard_similarity(frozenset({"a", "b"}), frozenset({"a", "b"})) == 1.0
    assert jaccard_similarity(frozenset({"a"}), frozenset({"b"})) == 0.0
    assert jaccard_similarity(frozenset(), frozenset()) == 0.0


def test_same_service_within_window_bundles():
    candidate = _incident("new", "[inventory-service] InventoryMemoryApproachingLimit", minutes_ago=0)
    existing = _incident("old", "[inventory-service] InventoryMemoryApproachingLimit", minutes_ago=2)
    result = correlate(candidate, [existing])
    assert result.decision == "bundle"
    assert result.bundle_with == "old"
    assert any("same service" in r for r in result.matches[0].reasons)


def test_different_service_different_hypothesis_stays_separate():
    candidate = _incident("new", "[checkout-service] HighErrorRate", "5xx spike on checkout API", minutes_ago=0)
    existing = _incident("old", "[payment-service] LatencyBudgetBreached", "p99 latency on payment gateway", minutes_ago=1)
    result = correlate(candidate, [existing])
    assert result.decision == "separate"
    assert result.bundle_with is None


def test_outside_time_window_stays_separate_even_if_same_service():
    candidate = _incident("new", "[checkout-service] HighErrorRate", minutes_ago=0)
    existing = _incident("old", "[checkout-service] HighErrorRate", minutes_ago=60)
    result = correlate(candidate, [existing], window_minutes=15)
    assert result.decision == "separate"


def test_adjacent_service_via_topology_map_bundles():
    candidate = _incident("new", "[checkout-service] HighErrorRate", minutes_ago=0)
    existing = _incident("old", "[payment-service] DownstreamTimeout", minutes_ago=3)
    adjacency = {"checkout-service": ["payment-service", "inventory-service"]}
    result = correlate(candidate, [existing], adjacency=adjacency)
    assert result.decision == "bundle"
    assert result.bundle_with == "old"
    assert any("adjacent services" in r for r in result.matches[0].reasons)


def test_unrelated_services_no_adjacency_no_text_overlap_stays_separate_despite_topology_map():
    candidate = _incident("new", "[checkout-service] HighErrorRate", minutes_ago=0)
    existing = _incident("old", "[notifications-service] EmailQueueBacklog", minutes_ago=1)
    adjacency = {"checkout-service": ["payment-service"]}  # notifications-service not listed
    result = correlate(candidate, [existing], adjacency=adjacency)
    assert result.decision == "separate"


def test_strong_text_similarity_alone_bundles_unrelated_services():
    candidate = _incident(
        "new", "[gateway-service] UnboundedAnalyticsBuffer",
        "unbounded analytics buffer growth caused by commit 851b565 in gateway-service", minutes_ago=0,
    )
    existing = _incident(
        "old", "[edge-service] UnboundedAnalyticsBuffer",
        "unbounded analytics buffer growth caused by commit 851b565 in edge-service", minutes_ago=1,
    )
    result = correlate(candidate, [existing])
    assert result.decision == "bundle"
    assert result.bundle_with == "old"


def test_different_cluster_never_bundles():
    candidate = _incident("new", "[checkout-service] HighErrorRate", minutes_ago=0, cluster="c1")
    existing = _incident("old", "[checkout-service] HighErrorRate", minutes_ago=1, cluster="c2")
    result = correlate(candidate, [existing])
    assert result.decision == "separate"


def test_self_is_excluded_from_matches():
    candidate = _incident("same-id", "[checkout-service] HighErrorRate", minutes_ago=0)
    result = correlate(candidate, [candidate])
    assert result.decision == "separate"


def test_picks_highest_scoring_match_among_multiple_open_incidents():
    candidate = _incident("new", "[checkout-service] HighErrorRate", "5xx spike checkout", minutes_ago=0)
    weak = _incident("weak", "[payment-service] LatencyBudgetBreached", "latency payment gateway", minutes_ago=2)
    strong = _incident("strong", "[checkout-service] HighErrorRate", "5xx spike checkout", minutes_ago=1)
    result = correlate(candidate, [weak, strong], adjacency={"checkout-service": ["payment-service"]})
    assert result.decision == "bundle"
    assert result.bundle_with == "strong"


def test_no_created_at_skips_time_filter_gracefully():
    candidate = CorrelationCandidate("new", "c1", "[checkout-service] HighErrorRate", "", created_at=None)
    existing = CorrelationCandidate("old", "c1", "[checkout-service] HighErrorRate", "", created_at=None)
    result = correlate(candidate, [existing])
    assert result.decision == "bundle"


@pytest.mark.parametrize("similarity_threshold", [0.1, 0.9])
def test_similarity_threshold_is_configurable(similarity_threshold):
    candidate = _incident("new", "[svc-a] Alpha", "some words overlap partially here", minutes_ago=0)
    existing = _incident("old", "[svc-b] Beta", "some words overlap partially there", minutes_ago=1)
    result = correlate(candidate, [existing], similarity_threshold=similarity_threshold)
    if similarity_threshold <= 0.5:
        assert result.decision == "bundle"
    else:
        assert result.decision == "separate"
