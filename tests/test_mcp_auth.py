#!/usr/bin/env python3
"""MCP bearer-authentication and network-exposure regression tests."""

import asyncio
import importlib.util
import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_EDGE_DIR = _ROOT / "edge_mcp_servers"


def _module():
    # mcp_auth.py does `from relay_credentials import ...` — a flat, same-directory
    # import that resolves naturally in the container (both files copied to the same
    # WORKDIR); add the directory to sys.path here so it resolves the same way in-test.
    if str(_EDGE_DIR) not in sys.path:
        sys.path.insert(0, str(_EDGE_DIR))
    path = _EDGE_DIR / "mcp_auth.py"
    spec = importlib.util.spec_from_file_location("_edge_mcp_auth", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_bearer_auth_accepts_only_exact_shared_token(monkeypatch):
    module = _module()
    monkeypatch.setenv("MCP_SERVICE_TOKEN", "shared-secret")
    assert module.bearer_authorized(
        [(b"authorization", b"Bearer shared-secret")]
    )
    assert not module.bearer_authorized([])
    assert not module.bearer_authorized(
        [(b"authorization", b"Bearer wrong-secret")]
    )
    assert not module.bearer_authorized(
        [(b"authorization", b"Basic shared-secret")]
    )


def test_unset_service_token_fails_closed(monkeypatch):
    module = _module()
    monkeypatch.delenv("MCP_SERVICE_TOKEN", raising=False)
    assert not module.bearer_authorized(
        [(b"authorization", b"Bearer anything")]
    )


def test_middleware_rejects_before_reaching_mcp_app(monkeypatch):
    module = _module()
    monkeypatch.setenv("MCP_SERVICE_TOKEN", "shared-secret")
    reached = []
    sent = []

    async def app(scope, receive, send):
        reached.append(True)

    async def receive():
        return {"type": "http.request"}

    async def send(message):
        sent.append(message)

    asyncio.run(
        module.MCPBearerAuthMiddleware(app)(
            {"type": "http", "headers": []}, receive, send
        )
    )
    assert not reached
    assert sent[0]["status"] == 401
    assert (b"www-authenticate", b"Bearer") in sent[0]["headers"]


def test_every_edge_server_runs_authenticated_transport():
    servers = list(
        (_ROOT / "edge_mcp_servers" / "mcp_servers").glob("*/server.py")
    )
    assert servers
    for server in servers:
        source = server.read_text()
        assert "run_authenticated_sse" in source, server
        assert 'mcp.run(transport="sse")' not in source, server


def test_compose_ports_are_loopback_only_and_require_token():
    source = (_ROOT / "edge_mcp_servers" / "docker-compose.yaml").read_text()
    for port in range(4000, 4008):
        assert f'"127.0.0.1:{port}:3000"' in source
    assert source.count("MCP_SERVICE_TOKEN=${MCP_SERVICE_TOKEN:?required}") == 8


def test_mcp_client_threads_bearer_header_through_server_config():
    source = (_ROOT / "sre_agent" / "multi_agent_langgraph.py").read_text()
    assert '"headers": dict(relay_headers)' in source
    assert "build_relay_headers(context, service_token=service_token)" in source
    executor_source = (_ROOT / "sre_agent" / "executor.py").read_text()
    assert '"headers": execution_context.transport_headers()' in executor_source
    assert "require_operator_mcp_endpoint(server_name, endpoint)" in executor_source
    assert "require_operator_mcp_endpoint(name, uri)" in source
    relay_auth_source = (_ROOT / "sre_agent" / "multitenant" / "relay_auth.py").read_text()
    assert "context.transport_headers(service_token)" in relay_auth_source
