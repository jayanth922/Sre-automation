#!/usr/bin/env python3
"""Sandbox edge-side guardrails — defense in depth for untrusted candidate code.

Every Job this server creates runs an AI-generated code fix that has not been
reviewed by a human. These guardrails are the hard, operator-controlled
envelope around that: only a fixed namespace, only an allow-listed set of
runner images, and mandatory resource/time bounds so a runaway or malicious
candidate can never become a denial-of-service against the cluster it runs on.

All limits are environment variables so the operator — not the LLM — owns
them. Pure stdlib, so this is unit-testable without a cluster.
"""

from __future__ import annotations

import os
from typing import Tuple


def sandbox_namespace() -> str:
    return os.getenv("SANDBOX_NAMESPACE", "sentinel-sandbox").strip() or "sentinel-sandbox"


def allowed_images() -> set[str]:
    raw = os.getenv("SANDBOX_ALLOWED_IMAGES", "")
    return {i.strip() for i in raw.split(",") if i.strip()}


def max_active_deadline_seconds() -> int:
    try:
        return int(os.getenv("SANDBOX_MAX_ACTIVE_DEADLINE_SECONDS", "600"))
    except ValueError:
        return 600


def resource_envelope() -> dict:
    return {
        "requests": {
            "cpu": os.getenv("SANDBOX_CPU_REQUEST", "250m"),
            "memory": os.getenv("SANDBOX_MEMORY_REQUEST", "256Mi"),
        },
        "limits": {
            "cpu": os.getenv("SANDBOX_CPU_LIMIT", "1"),
            "memory": os.getenv("SANDBOX_MEMORY_LIMIT", "1Gi"),
        },
    }


def guardrail_check(namespace: str, image: str, active_deadline_seconds: int) -> Tuple[bool, str]:
    """Return (allowed, reason). A False result must hard-refuse the Job."""
    expected_ns = sandbox_namespace()
    if namespace != expected_ns:
        return False, f"namespace '{namespace}' is not the configured sandbox namespace '{expected_ns}'"

    allow = allowed_images()
    if not allow:
        return False, "no sandbox runner images are allow-listed (SANDBOX_ALLOWED_IMAGES is empty)"
    if image not in allow:
        return False, f"image '{image}' is not in the sandbox allow-list {sorted(allow)}"

    ceiling = max_active_deadline_seconds()
    if active_deadline_seconds <= 0 or active_deadline_seconds > ceiling:
        return False, (
            f"activeDeadlineSeconds must be in (0, {ceiling}]; refusing an "
            "unbounded or excessive sandbox run"
        )

    return True, "ok"
