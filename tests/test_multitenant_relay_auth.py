"""Unit tests for the credential relay: sre_agent/multitenant/relay_auth.py
(control-plane side) and edge_mcp_servers/relay_credentials.py (edge side).
"""
import importlib.util
from pathlib import Path

import pytest

from sre_agent.execution_context import ExecutionContext
from sre_agent.multitenant import relay_auth

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _service_token(monkeypatch):
    monkeypatch.setenv("MCP_SERVICE_TOKEN", "svc-token")


def _edge_module():
    path = _ROOT / "edge_mcp_servers" / "relay_credentials.py"
    spec = importlib.util.spec_from_file_location("_edge_relay_credentials", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _context(**credentials):
    return ExecutionContext(
        organization_id="org-1",
        cluster_id="cluster-1",
        mcp_endpoints={"github": "http://edge/github/sse"},
        credentials=credentials,
    )


@pytest.mark.asyncio
async def test_build_relay_headers_includes_base_transport_headers(monkeypatch):
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    context = _context()
    headers = await relay_auth.build_relay_headers(context, service_token="svc-token")
    assert headers["Authorization"] == "Bearer svc-token"
    assert headers["X-Sentinel-Organization-ID"] == "org-1"
    assert headers["X-Sentinel-Cluster-ID"] == "cluster-1"


@pytest.mark.asyncio
async def test_build_relay_headers_adds_github_relay_when_pat_present(monkeypatch):
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    context = _context(github_token="pat-123", github_repo="acme/widgets")
    headers = await relay_auth.build_relay_headers(context)
    assert headers[relay_auth.GITHUB_TOKEN_HEADER] == "pat-123"
    assert headers[relay_auth.GITHUB_REPO_HEADER] == "acme/widgets"


@pytest.mark.asyncio
async def test_build_relay_headers_omits_github_relay_without_repo(monkeypatch):
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    context = _context(github_token="pat-123")
    headers = await relay_auth.build_relay_headers(context)
    assert relay_auth.GITHUB_TOKEN_HEADER not in headers


@pytest.mark.asyncio
async def test_build_relay_headers_adds_k8s_relay_when_both_present():
    context = _context(k8s_api_server="https://k8s.acme.internal", k8s_token="k8s-tok")
    headers = await relay_auth.build_relay_headers(context)
    assert headers[relay_auth.K8S_API_SERVER_HEADER] == "https://k8s.acme.internal"
    assert headers[relay_auth.K8S_TOKEN_HEADER] == "k8s-tok"


@pytest.mark.asyncio
async def test_build_relay_headers_omits_k8s_relay_when_partial():
    context = _context(k8s_api_server="https://k8s.acme.internal")
    headers = await relay_auth.build_relay_headers(context)
    assert relay_auth.K8S_API_SERVER_HEADER not in headers
    assert relay_auth.K8S_TOKEN_HEADER not in headers


# ── edge side ─────────────────────────────────────────────────────────────

def test_edge_headers_match_control_plane_header_names():
    edge = _edge_module()
    assert relay_auth.GITHUB_TOKEN_HEADER.lower().encode() == edge.GITHUB_TOKEN_HEADER
    assert relay_auth.GITHUB_REPO_HEADER.lower().encode() == edge.GITHUB_REPO_HEADER
    assert relay_auth.K8S_API_SERVER_HEADER.lower().encode() == edge.K8S_API_SERVER_HEADER
    assert relay_auth.K8S_TOKEN_HEADER.lower().encode() == edge.K8S_TOKEN_HEADER


def test_capture_and_get_relay_credential_roundtrip():
    edge = _edge_module()
    edge.capture_relay_credentials(
        [
            (b"x-sentinel-relay-github-token", b"pat-123"),
            (b"X-Sentinel-Relay-Github-Repo", b"acme/widgets"),
            (b"content-type", b"application/json"),
        ]
    )
    assert edge.get_relay_credential("github_token") == "pat-123"
    assert edge.get_relay_credential("github_repo") == "acme/widgets"
    assert edge.get_relay_credential("k8s_token") is None


def test_capture_relay_credentials_resets_between_connections():
    edge = _edge_module()
    edge.capture_relay_credentials([(b"x-sentinel-relay-github-token", b"pat-123")])
    assert edge.get_relay_credential("github_token") == "pat-123"
    edge.capture_relay_credentials([])
    assert edge.get_relay_credential("github_token") is None


# ── edge server wiring (source-level, mirrors test_mcp_auth.py's style) ────

def test_mcp_auth_middleware_captures_relay_credentials():
    source = (_ROOT / "edge_mcp_servers" / "mcp_auth.py").read_text()
    assert "capture_relay_credentials(" in source
    assert "import capture_relay_credentials" in source


def test_github_real_server_prefers_relayed_repo():
    source = (_ROOT / "edge_mcp_servers" / "mcp_servers" / "github_real" / "server.py").read_text()
    assert "_active_repo(" in source
    assert "get_relay_credential" in source


def test_k8s_real_server_prefers_relayed_api_client():
    source = (_ROOT / "edge_mcp_servers" / "mcp_servers" / "k8s_real" / "server.py").read_text()
    assert "_relay_api_client(" in source
    assert "get_relay_credential" in source


def test_all_relay_dockerfiles_copy_relay_credentials_module():
    dockerfiles = list(
        (_ROOT / "edge_mcp_servers" / "mcp_servers").glob("*/Dockerfile")
    )
    assert dockerfiles
    for dockerfile in dockerfiles:
        source = dockerfile.read_text()
        assert "COPY relay_credentials.py ." in source, dockerfile
