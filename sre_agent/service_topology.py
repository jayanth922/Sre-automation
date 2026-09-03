#!/usr/bin/env python3
"""
Service-adjacency inference for the correlation gate.

`sre_agent/incident_correlation.py::correlate` accepts an optional
`adjacency: Dict[str, Sequence[str]]` map (service name -> neighbor service
names) used as a secondary, weaker correlation signal alongside same-service
matching. This module builds that map from Kubernetes metadata already
present in-cluster, rather than a hand-maintained config file:

- `app.kubernetes.io/part-of` labels on Services/Deployments group workloads
  that belong to the same logical application.
- NetworkPolicy ingress/egress rules resolved via label matching give
  stronger, explicit edges between services.

This module intentionally keeps all cluster I/O out of
`incident_correlation.py`, which is deliberately pure and DB/network free.
Any failure here (k8s timeout, relay error, missing MCP endpoint, Redis
unavailable) must be swallowed and degrade to no adjacency signal — this is a
shadow-mode correlation aid, not a critical path.
"""

import json
import logging
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

_PART_OF_LABEL = "app.kubernetes.io/part-of"
_TOPOLOGY_CACHE_TTL_SECONDS = 300


def _service_name_from_labels(labels: Dict[str, Any]) -> Optional[str]:
    name = labels.get("app.kubernetes.io/name") or labels.get("app")
    return str(name).strip().lower() if name else None


def _selector_matches(selector: Optional[Dict[str, str]], labels: Dict[str, str]) -> bool:
    """Whether every key/value in `selector` is present in `labels` (an empty
    or missing selector matches everything, per k8s NetworkPolicy semantics).
    """
    if not selector:
        return True
    return all(labels.get(key) == value for key, value in selector.items())


def build_adjacency_map(
    services: Sequence[Dict[str, Any]],
    deployments: Sequence[Dict[str, Any]],
    network_policies: Sequence[Dict[str, Any]],
) -> Dict[str, List[str]]:
    """Pure builder: turn raw k8s Service/Deployment/NetworkPolicy dicts (as
    returned by the `k8s` MCP server's `list_services` / `list_deployments` /
    `list_network_policies` tools) into a service-name adjacency map.

    Keys and values are lowercased service names, matching
    `incident_correlation.extract_service`'s convention so the map can be
    passed straight into `correlate(..., adjacency=...)`.
    """
    adjacency: Dict[str, set] = {}

    def _link(a: str, b: str) -> None:
        if not a or not b or a == b:
            return
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)

    # 1. `app.kubernetes.io/part-of` grouping across Services and Deployments.
    part_of_groups: Dict[str, set] = {}
    for entry in (*services, *deployments):
        labels = entry.get("labels") or {}
        part_of = labels.get(_PART_OF_LABEL)
        name = _service_name_from_labels(labels) or entry.get("name")
        if not part_of or not name:
            continue
        part_of_groups.setdefault(str(part_of).strip().lower(), set()).add(str(name).strip().lower())

    for members in part_of_groups.values():
        members_list = sorted(members)
        for i, a in enumerate(members_list):
            for b in members_list[i + 1 :]:
                _link(a, b)

    # 2. NetworkPolicy ingress/egress edges, resolved via label matching
    # against known Service selectors (a policy's pod_selector picks out the
    # "owning" service; each peer's pod_selector picks out the neighbor).
    service_by_selector = [
        (dict(svc.get("selector") or {}), svc.get("namespace"), _service_name_from_labels(svc.get("labels") or {}) or svc.get("name"))
        for svc in services
        if svc.get("selector")
    ]

    def _services_matching(selector: Optional[Dict[str, str]], namespace: Optional[str]) -> List[str]:
        matches = []
        for svc_selector, svc_namespace, svc_name in service_by_selector:
            if namespace and svc_namespace and svc_namespace != namespace:
                continue
            if svc_name and _selector_matches(selector, svc_selector) and _selector_matches(svc_selector, selector or {}):
                matches.append(str(svc_name).strip().lower())
        return matches

    for policy in network_policies:
        namespace = policy.get("namespace")
        owner_names = _services_matching(policy.get("pod_selector"), namespace) or [
            str(policy.get("name", "")).strip().lower()
        ]
        peers: List[Dict[str, Any]] = []
        for rule in policy.get("ingress") or []:
            peers.extend(rule.get("from") or [])
        for rule in policy.get("egress") or []:
            peers.extend(rule.get("to") or [])

        for peer in peers:
            peer_selector = peer.get("pod_selector")
            if not peer_selector:
                continue
            neighbor_names = _services_matching(peer_selector, namespace)
            for owner in owner_names:
                for neighbor in neighbor_names:
                    _link(owner, neighbor)

    return {service: sorted(neighbors) for service, neighbors in adjacency.items()}


async def get_adjacency_map(cluster, execution_context=None) -> Optional[Dict[str, List[str]]]:
    """Fetch (or build and cache) the service-adjacency map for `cluster`.

    Non-fatal by design: any failure (missing k8s endpoint, MCP call error,
    malformed response, Redis unavailable) logs and returns `None`, so callers
    can fall back to `correlate(...)` with no adjacency signal exactly as
    before this feature existed.
    """
    from .redis_state_store import get_state_store

    cluster_id = str(getattr(cluster, "id", "")) or None
    store = get_state_store()
    if cluster_id:
        cached = store.get_topology_cache(cluster_id)
        if cached is not None:
            return cached

    try:
        from .execution_context import ExecutionContext
        from .executor import build_k8s_tool_caller

        context = execution_context or ExecutionContext.from_cluster(cluster)
        caller = await build_k8s_tool_caller(context)

        def _parsed(raw: Any) -> Dict[str, Any]:
            data = json.loads(raw) if isinstance(raw, str) else raw
            return data if isinstance(data, dict) else {}

        services_raw = _parsed(await caller("list_services", {"limit": 2000}))
        deployments_raw = _parsed(await caller("list_deployments", {"limit": 2000}))
        policies_raw = _parsed(await caller("list_network_policies", {"limit": 2000}))

        adjacency = build_adjacency_map(
            services_raw.get("services") or [],
            deployments_raw.get("deployments") or [],
            policies_raw.get("network_policies") or [],
        )
    except Exception as e:
        logger.warning("Service-topology fetch failed for cluster %s: %s", cluster_id, e)
        return None

    if cluster_id:
        store.set_topology_cache(cluster_id, adjacency, _TOPOLOGY_CACHE_TTL_SECONDS)
    return adjacency
