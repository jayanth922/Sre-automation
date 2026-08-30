"""GitHub App-based per-installation credential minting.

Replaces the long-lived personal-access-token model (``Cluster.github_token``,
stored indefinitely) with a platform-registered GitHub App: one app,
installed by each customer into their own repository, issuing short-lived
(~1h) installation access tokens minted on demand. A Cluster that has
installed the App (``github_app_installation_id`` set) gets a freshly minted
token per resolution; a Cluster without an installation keeps using its
stored ``github_token`` PAT — the fallback self-hosted single-tenant
deployments already rely on, unchanged.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Mapping, Optional

import httpx
from jose import jwt

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
# GitHub rejects App JWTs valid for more than 10 minutes; stay comfortably under.
_JWT_TTL_SECONDS = 540
_CLOCK_DRIFT_ALLOWANCE_SECONDS = 60


class GitHubAppError(RuntimeError):
    """The GitHub App is not configured, or a token could not be minted."""


def _private_key() -> Optional[str]:
    key = os.getenv("GITHUB_APP_PRIVATE_KEY", "")
    if key.strip():
        return key
    key_path = os.getenv("GITHUB_APP_PRIVATE_KEY_PATH", "").strip()
    if key_path and os.path.exists(key_path):
        with open(key_path, "r", encoding="utf-8") as fh:
            return fh.read()
    return None


def github_app_configured() -> bool:
    return bool(os.getenv("GITHUB_APP_ID", "").strip() and _private_key())


def build_app_jwt(*, now: Optional[int] = None) -> str:
    """Sign a short-lived JWT identifying the App itself (GitHub App auth, RS256)."""
    app_id = os.getenv("GITHUB_APP_ID", "").strip()
    key = _private_key()
    if not app_id or not key:
        raise GitHubAppError(
            "GITHUB_APP_ID / GITHUB_APP_PRIVATE_KEY(_PATH) are not configured"
        )
    issued_at = int(now if now is not None else time.time())
    payload = {
        "iat": issued_at - _CLOCK_DRIFT_ALLOWANCE_SECONDS,
        "exp": issued_at + _JWT_TTL_SECONDS,
        "iss": app_id,
    }
    return jwt.encode(payload, key, algorithm="RS256")


async def mint_installation_token(installation_id: str) -> str:
    """Exchange the App's JWT for a token scoped to one installation."""
    if not installation_id:
        raise GitHubAppError("installation_id is required")
    app_jwt = build_app_jwt()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        resp.raise_for_status()
        data = resp.json()
    token = data.get("token")
    if not token:
        raise GitHubAppError("GitHub did not return an installation token")
    return token


def install_url(state: str) -> str:
    """The customer-facing 'Install' link for the platform's GitHub App."""
    slug = os.getenv("GITHUB_APP_SLUG", "").strip()
    if not slug:
        raise GitHubAppError("GITHUB_APP_SLUG is not configured")
    return f"https://github.com/apps/{slug}/installations/new?state={state}"


async def resolve_github_credential(credentials: Mapping[str, str]) -> Optional[str]:
    """Best-effort per-cluster GitHub token.

    Prefers a freshly minted App installation token; falls back to the
    cluster's stored PAT on any failure (misconfiguration, GitHub outage) so
    a broken App integration never blocks an investigation that only needs
    the fallback credential — same non-fatal-integration convention as
    ``sre_agent/integrations/jira.py``.
    """
    installation_id = credentials.get("github_app_installation_id")
    if installation_id and github_app_configured():
        try:
            return await mint_installation_token(str(installation_id))
        except Exception as exc:
            logger.warning(
                f"github_app: installation token mint failed, falling back to PAT: {exc}"
            )
    return credentials.get("github_token")
