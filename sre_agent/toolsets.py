#!/usr/bin/env python3
"""
Toolset registry (competitive-audit upgrade #4: breadth like HolmesGPT).

HolmesGPT's edge is breadth — an iterative ReAct loop over 30+ observability
toolsets. Our MCP layer is the same shape but wraps fewer sources. This registry
makes coverage explicit: what we integrate today, and the candidate sources
(the kind HolmesGPT covers) that a new MCP server could add behind the same
interface. It's the extension map, not new servers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass
class Toolset:
    name: str
    kind: str            # metrics | logs | infra | code | runbooks | exec | traces | events | cloud | db | apm
    integrated: bool
    via: str             # the MCP server / mechanism (or a note for candidates)


# What we wrap today (the edge MCP servers) + high-value candidates to add.
REGISTRY: List[Toolset] = [
    Toolset("kubernetes", "infra", True, "edge_mcp_servers/k8s_real (:4000)"),
    Toolset("prometheus", "metrics", True, "edge_mcp_servers/prometheus_real (:4001)"),
    Toolset("loki", "logs", True, "edge_mcp_servers/loki_real (:4002)"),
    Toolset("github", "code", True, "edge_mcp_servers/github_real (:4003, read)"),
    Toolset("runbooks", "runbooks", True, "edge_mcp_servers/runbooks_notion (:4004)"),
    Toolset("k8s_executor", "exec", True, "edge_mcp_servers/executor_real (:4005)"),
    Toolset("github_exec", "code", True, "edge_mcp_servers/github_exec (:4006, revert PRs)"),
    # Candidates (HolmesGPT-style breadth) — add a new MCP server behind the same interface:
    Toolset("distributed_tracing", "traces", False, "candidate: Tempo/Jaeger MCP"),
    Toolset("k8s_events", "events", False, "candidate: kube events MCP"),
    Toolset("datadog", "apm", False, "candidate: Datadog MCP"),
    Toolset("cloudwatch", "metrics", False, "candidate: AWS CloudWatch MCP"),
    Toolset("postgres_db", "db", False, "candidate: DB slow-query MCP"),
    Toolset("grafana", "metrics", False, "candidate: Grafana MCP"),
    Toolset("opensearch", "logs", False, "candidate: OpenSearch/ELK MCP"),
]


def integrated() -> List[Toolset]:
    return [t for t in REGISTRY if t.integrated]


def candidates() -> List[Toolset]:
    return [t for t in REGISTRY if not t.integrated]


def coverage() -> Dict[str, int]:
    total = len(REGISTRY)
    have = len(integrated())
    return {"integrated": have, "candidates": total - have, "total": total}
