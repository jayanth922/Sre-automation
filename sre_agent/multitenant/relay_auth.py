"""Per-tenant credential relay over the MCP transport.

Historically every edge MCP server (``edge_mcp_servers/mcp_servers/*``)
resolved exactly one tenant's credentials from its own process environment
(``GITHUB_TOKEN``/``GITHUB_REPO``, ``KUBECONFIG``, ...) — correct only when
one Sentinel deployment serves exactly one ``Cluster``. A single control
plane can manage many ``Cluster`` rows per ``Organization``, so the same
edge deployment can be asked to act against several distinct tenant
destinations in turn.

This module lets the control plane relay one cluster's own resolved
credentials alongside its MCP connection, as additional headers next to the
existing tenant-identity headers (``X-Sentinel-Organization-ID`` /
``X-Sentinel-Cluster-ID``) already set by
``ExecutionContext.transport_headers()``. An edge server then prefers the
relayed, request-scoped credential over its static single-tenant
environment fallback.

See ``edge_mcp_servers/relay_credentials.py`` for the receiving side — kept
deliberately dependency-free (it must not import ``sre_agent``, since
``edge_mcp_servers`` is a separately deployed container; the same constraint
already applies to ``edge_mcp_servers/mcp_servers/runbooks_local/server.py``,
which keeps its own embedding code rather than importing
``sre_agent.embedding``). Header names are duplicated as string constants on
both sides rather than shared via import for that reason — keep them in
sync by hand.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, Optional

from .github_app import resolve_github_credential

if TYPE_CHECKING:
    from ..execution_context import ExecutionContext

logger = logging.getLogger(__name__)

# Must match the constants in edge_mcp_servers/relay_credentials.py exactly.
GITHUB_TOKEN_HEADER = "X-Sentinel-Relay-Github-Token"
GITHUB_REPO_HEADER = "X-Sentinel-Relay-Github-Repo"
K8S_API_SERVER_HEADER = "X-Sentinel-Relay-K8s-Api-Server"
K8S_TOKEN_HEADER = "X-Sentinel-Relay-K8s-Token"


async def build_relay_headers(
    context: "ExecutionContext", *, service_token: Optional[str] = None
) -> Dict[str, str]:
    """The full header set for one cluster's MCP connection: the existing
    tenant-identity/bearer headers plus this cluster's own relayed
    credentials, resolved from ``context.credentials``."""
    headers = dict(context.transport_headers(service_token))

    github_token = await resolve_github_credential(context.credentials)
    github_repo = context.credentials.get("github_repo")
    if github_token and github_repo:
        headers[GITHUB_TOKEN_HEADER] = github_token
        headers[GITHUB_REPO_HEADER] = github_repo

    k8s_api_server = context.credentials.get("k8s_api_server")
    k8s_token = context.credentials.get("k8s_token")
    if k8s_api_server and k8s_token:
        headers[K8S_API_SERVER_HEADER] = k8s_api_server
        headers[K8S_TOKEN_HEADER] = k8s_token

    return headers
