"""Per-request relayed tenant credentials, captured off the MCP transport.

Deliberately dependency-free — this module is copied flat into each edge MCP
server's container image alongside ``mcp_auth.py`` and must never import
``sre_agent`` (a separate, customer-deployed process). Header names here
must match ``sre_agent/multitenant/relay_auth.py`` exactly; kept as
duplicated string constants rather than a shared import for that reason.

One Sentinel control plane can manage many ``Cluster`` rows, so a single
edge deployment may be asked to act on behalf of different tenants across
different MCP connections. The bearer-auth ASGI middleware in
``mcp_auth.py`` captures each connection's relayed credential headers into a
``contextvar`` here before handing off to the FastMCP app; tool handlers
running within that same connection's async call chain can then read them
back via ``get_relay_credential`` and prefer them over a static,
single-tenant environment-variable fallback.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Dict, Iterable, Optional, Tuple

GITHUB_TOKEN_HEADER = b"x-sentinel-relay-github-token"
GITHUB_REPO_HEADER = b"x-sentinel-relay-github-repo"
K8S_API_SERVER_HEADER = b"x-sentinel-relay-k8s-api-server"
K8S_TOKEN_HEADER = b"x-sentinel-relay-k8s-token"
NOTION_API_KEY_HEADER = b"x-sentinel-relay-notion-key"
NOTION_DATABASE_ID_HEADER = b"x-sentinel-relay-notion-database"

_RELAY_HEADER_NAMES = {
    GITHUB_TOKEN_HEADER: "github_token",
    GITHUB_REPO_HEADER: "github_repo",
    K8S_API_SERVER_HEADER: "k8s_api_server",
    K8S_TOKEN_HEADER: "k8s_token",
    NOTION_API_KEY_HEADER: "notion_api_key",
    NOTION_DATABASE_ID_HEADER: "notion_database_id",
}

_relay_credentials: "ContextVar[Dict[str, str]]" = ContextVar(
    "sentinel_relay_credentials", default={}
)


def capture_relay_credentials(headers: Iterable[Tuple[bytes, bytes]]) -> None:
    """Parse one ASGI connection's headers into the relay-credential contextvar."""
    parsed: Dict[str, str] = {}
    for name, value in headers:
        key = _RELAY_HEADER_NAMES.get(name.lower())
        if key is not None:
            parsed[key] = value.decode("utf-8", errors="strict")
    _relay_credentials.set(parsed)


def get_relay_credential(name: str) -> Optional[str]:
    """A credential relayed for the in-flight connection, if any."""
    return _relay_credentials.get().get(name)
