#!/usr/bin/env python3
"""Tests for k8s-label-inferred service adjacency (Phase 5 correlation gate)."""

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _load(name, rel_path):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_st = _load("service_topology_under_test", "sre_agent/service_topology.py")
build_adjacency_map = _st.build_adjacency_map


def _service(name, namespace="default", labels=None, selector=None):
    return {
        "name": name,
        "namespace": namespace,
        "labels": labels or {},
        "selector": selector or {},
    }


def _deployment(name, namespace="default", labels=None):
    return {"name": name, "namespace": namespace, "labels": labels or {}}


def _network_policy(name, namespace, pod_selector, ingress=None, egress=None):
    return {
        "name": name,
        "namespace": namespace,
        "pod_selector": pod_selector,
        "ingress": ingress or [],
        "egress": egress or [],
    }


def test_part_of_label_groups_services_bidirectionally():
    services = [
        _service("checkout", labels={"app.kubernetes.io/name": "checkout", "app.kubernetes.io/part-of": "shop"}),
        _service("payments", labels={"app.kubernetes.io/name": "payments", "app.kubernetes.io/part-of": "shop"}),
        _service("unrelated", labels={"app.kubernetes.io/name": "unrelated"}),
    ]

    adjacency = build_adjacency_map(services, [], [])

    assert adjacency["checkout"] == ["payments"]
    assert adjacency["payments"] == ["checkout"]
    assert "unrelated" not in adjacency


def test_part_of_label_groups_three_or_more_services():
    services = [
        _service(n, labels={"app.kubernetes.io/name": n, "app.kubernetes.io/part-of": "shop"})
        for n in ("checkout", "payments", "cart")
    ]

    adjacency = build_adjacency_map(services, [], [])

    assert adjacency["checkout"] == ["cart", "payments"]
    assert adjacency["payments"] == ["cart", "checkout"]
    assert adjacency["cart"] == ["checkout", "payments"]


def test_deployments_contribute_to_part_of_grouping_too():
    services = [_service("checkout", labels={"app.kubernetes.io/name": "checkout", "app.kubernetes.io/part-of": "shop"})]
    deployments = [_deployment("payments", labels={"app.kubernetes.io/name": "payments", "app.kubernetes.io/part-of": "shop"})]

    adjacency = build_adjacency_map(services, deployments, [])

    assert "payments" in adjacency.get("checkout", [])


def test_network_policy_links_owner_to_peer_via_label_selector():
    services = [
        _service("checkout", selector={"app": "checkout"}),
        _service("payments", selector={"app": "payments"}),
    ]
    policies = [
        _network_policy(
            "checkout-egress",
            "default",
            pod_selector={"app": "checkout"},
            egress=[{"to": [{"pod_selector": {"app": "payments"}}]}],
        )
    ]

    adjacency = build_adjacency_map(services, [], policies)

    assert "payments" in adjacency["checkout"]
    assert "checkout" in adjacency["payments"]


def test_no_signal_yields_empty_map():
    services = [_service("checkout"), _service("payments")]

    adjacency = build_adjacency_map(services, [], [])

    assert adjacency == {}


def test_network_policy_across_namespaces_is_not_linked():
    services = [
        _service("checkout", namespace="ns-a", selector={"app": "checkout"}),
        _service("payments", namespace="ns-b", selector={"app": "payments"}),
    ]
    policies = [
        _network_policy(
            "checkout-egress",
            "ns-a",
            pod_selector={"app": "checkout"},
            egress=[{"to": [{"pod_selector": {"app": "payments"}}]}],
        )
    ]

    adjacency = build_adjacency_map(services, [], policies)

    assert "checkout" not in adjacency
