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
import re
from typing import Any, Dict, Tuple

ALLOWED_ACTIONS = {
    "restart",
    "scale",
    "rollback",
    "patch_resource_limits",
    "patch_deployment_env",
    "recreate_pod",
    # Read-only. Allow-listed like the rest so the envelope stays one list, but
    # it mutates nothing — see get_deployment_config in server.py.
    "get_deployment_config",
}

# Environment variables whose *name* says they carry a credential. The agent
# rewrites runtime behaviour (feature flags, fault-injection toggles, log
# levels); it must never rewrite a secret, because a wrong value there is an
# outage or a leak rather than a revertible config change, and because a
# prompt-injected "set DATABASE_PASSWORD=…" must die at the execution boundary
# and not depend on the reasoning side having been careful.
_CREDENTIAL_KEY_RE = re.compile(
    r"(SECRET|PASSWORD|PASSWD|TOKEN|CREDENTIAL|PRIVATE_KEY|API_?KEY|_KEY$|^KEY$|AUTH|SESSION|SALT|CERT)",
    re.IGNORECASE,
)
# A k8s env var name: C_IDENTIFIER, per the EnvVar API validation.
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def allowed_namespaces() -> set[str]:
    raw = os.getenv("EXECUTOR_ALLOWED_NAMESPACES", "demo-app")
    return {n.strip() for n in raw.split(",") if n.strip()}


def min_replicas() -> int:
    try:
        return int(os.getenv("EXECUTOR_MIN_REPLICAS", "1"))
    except ValueError:
        return 1


def allowed_env_keys() -> set[str]:
    """Operator's optional narrowing allow-list for settable env var names.

    Unset (the default) means "any non-credential key": the credential denylist
    below always applies, so the tool is usable out of the box without being
    unbounded. Set ``EXECUTOR_ALLOWED_ENV_KEYS`` to pin it to a known set.
    """
    raw = os.getenv("EXECUTOR_ALLOWED_ENV_KEYS", "")
    return {k.strip() for k in raw.split(",") if k.strip()}


def max_env_keys() -> int:
    """Blast-radius cap: how many env vars one action may rewrite at once."""
    try:
        return int(os.getenv("EXECUTOR_MAX_ENV_KEYS", "10"))
    except ValueError:
        return 10


def max_env_value_chars() -> int:
    try:
        return int(os.getenv("EXECUTOR_MAX_ENV_VALUE_CHARS", "1024"))
    except ValueError:
        return 1024


def check_env_payload(env: Any) -> Tuple[bool, str]:
    """Validate a patch_deployment_env payload. Pure, so it unit-tests bare."""
    if not isinstance(env, dict) or not env:
        return False, "patch_deployment_env requires a non-empty 'env' object of KEY: value"

    cap = max_env_keys()
    if len(env) > cap:
        return False, (
            f"refusing to rewrite {len(env)} env vars in one action: above the "
            f"maximum-keys cap ({cap}); blast-radius guard"
        )

    narrowing = allowed_env_keys()
    value_cap = max_env_value_chars()
    for key, value in env.items():
        key = str(key)
        if not _ENV_KEY_RE.match(key):
            return False, f"env key '{key}' is not a valid environment variable name"
        if _CREDENTIAL_KEY_RE.search(key):
            return False, (
                f"refusing to set '{key}': its name identifies a credential, and "
                "the executor never rewrites secrets"
            )
        if narrowing and key not in narrowing:
            return False, (
                f"env key '{key}' is not in the executor allow-list {sorted(narrowing)}"
            )
        if isinstance(value, (dict, list)):
            return False, f"env value for '{key}' must be a scalar, got {type(value).__name__}"
        if len(str(value)) > value_cap:
            return False, (
                f"env value for '{key}' is {len(str(value))} chars, above the "
                f"{value_cap}-char cap"
            )
    return True, "ok"


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

    if action == "patch_deployment_env":
        return check_env_payload(params.get("env"))

    return True, "ok"
