#!/usr/bin/env python3
"""
Executor edge-side guardrails — defense in depth.

The agent-side Policy Gate (severity × reversibility) already decides *whether* an
action may run autonomously. These guardrails are a second, independent safety
layer enforced at the execution boundary itself, so a bug or prompt-injection on
the reasoning side can never make the executor do something outside a hard,
operator-controlled envelope:

- only an explicit allow-list of action types may run,
- only inside an allow-list of namespaces (default: the demo namespace),
- never scale a deployment below a floor (scale-to-0 / outage guard).

All limits are environment variables so the operator — not the LLM — owns them.
Pure stdlib, so this is unit-testable without a cluster.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Tuple

ALLOWED_ACTIONS = {"restart", "scale", "rollback", "patch_resource_limits", "recreate_pod"}


def allowed_namespaces() -> set[str]:
    raw = os.getenv("EXECUTOR_ALLOWED_NAMESPACES", "demo-app")
    return {n.strip() for n in raw.split(",") if n.strip()}


def min_replicas() -> int:
    try:
        return int(os.getenv("EXECUTOR_MIN_REPLICAS", "1"))
    except ValueError:
        return 1


def guardrail_check(action: str, namespace: str, params: Dict[str, Any] | None = None) -> Tuple[bool, str]:
    """Return (allowed, reason). A False result must hard-refuse the action."""
    params = params or {}
    action = (action or "").lower()

    if action not in ALLOWED_ACTIONS:
        return False, f"action '{action}' is not in the executor allow-list {sorted(ALLOWED_ACTIONS)}"

    ns_allow = allowed_namespaces()
    if namespace not in ns_allow:
        return False, f"namespace '{namespace}' is not in the executor allow-list {sorted(ns_allow)}"

    if action == "scale":
        replicas = params.get("replicas")
        if replicas is None:
            return False, "scale requires a 'replicas' parameter"
        try:
            r = int(replicas)
        except (TypeError, ValueError):
            return False, f"replicas must be an integer, got {replicas!r}"
        floor = min_replicas()
        if r < floor:
            return False, (
                f"refusing to scale to {r}: below the minimum-replicas floor "
                f"({floor}); scale-to-0 / outage guard"
            )

    return True, "ok"
