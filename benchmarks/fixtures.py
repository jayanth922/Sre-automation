"""Runtime credential fixtures for benchmarks (P11).

Benchmarks must not ship static cluster tokens or shared demo passwords.
Provide credentials via environment, or bootstrap them against a running
platform when ``BENCH_BOOTSTRAP=1``.
"""

from __future__ import annotations

import os
import secrets
import uuid
from dataclasses import dataclass
from typing import Optional

import httpx


class BenchConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class BenchCredentials:
    base_url: str
    admin_email: str
    admin_password: str
    cluster_id: str
    cluster_token: str


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise BenchConfigError(
            f"{name} is required. Export it from a freshly seeded environment, "
            "or set BENCH_BOOTSTRAP=1 to create ephemeral credentials at runtime."
        )
    return value


def load_credentials() -> BenchCredentials:
    """Load bench credentials from the environment (preferred for CI)."""
    return BenchCredentials(
        base_url=os.getenv("BENCH_BASE_URL", "http://localhost:8080").rstrip("/"),
        admin_email=_require("BENCH_ADMIN_EMAIL"),
        admin_password=_require("BENCH_ADMIN_PASSWORD"),
        cluster_id=_require("BENCH_CLUSTER_ID"),
        cluster_token=_require("BENCH_CLUSTER_TOKEN"),
    )


async def bootstrap_credentials(
    client: httpx.AsyncClient,
    *,
    base_url: Optional[str] = None,
) -> BenchCredentials:
    """Create an ephemeral admin + cluster against a running API."""
    base = (base_url or os.getenv("BENCH_BASE_URL", "http://localhost:8080")).rstrip("/")
    email = f"bench-{uuid.uuid4().hex[:10]}@example.com"
    password = secrets.token_urlsafe(18)
    org = f"bench-org-{uuid.uuid4().hex[:8]}"

    register = await client.post(
        f"{base}/auth/register",
        json={
            "email": email,
            "password": password,
            "full_name": "Bench Runner",
            "org_name": org,
        },
    )
    if register.status_code not in (200, 201):
        if os.getenv("BENCH_ADMIN_EMAIL") and os.getenv("BENCH_ADMIN_PASSWORD"):
            return load_credentials()
        raise BenchConfigError(
            f"bootstrap register failed ({register.status_code}): {register.text}"
        )

    token_resp = await client.post(
        f"{base}/auth/token",
        data={"username": email, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    token_resp.raise_for_status()
    jwt = token_resp.json()["access_token"]

    cluster_resp = await client.post(
        f"{base}/api/v1/clusters",
        headers={"Authorization": f"Bearer {jwt}"},
        json={"name": f"bench-{uuid.uuid4().hex[:8]}"},
    )
    cluster_resp.raise_for_status()
    cluster = cluster_resp.json()
    return BenchCredentials(
        base_url=base,
        admin_email=email,
        admin_password=password,
        cluster_id=str(cluster["id"]),
        cluster_token=str(cluster["token"]),
    )


async def resolve_credentials(client: httpx.AsyncClient) -> BenchCredentials:
    if os.getenv("BENCH_BOOTSTRAP", "").lower() in {"1", "true", "yes"}:
        return await bootstrap_credentials(client)
    return load_credentials()
