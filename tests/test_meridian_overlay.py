#!/usr/bin/env python3
"""P06: base platform config is Meridian-free; overlay restores the contract."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_base_k8s_config_has_no_meridian():
    text = (ROOT / "deploy" / "k8s" / "config.yaml").read_text()
    assert "meridian" not in text.lower()
    assert 'MCP_SERVERS_JSON: ""' in text or "MCP_SERVERS_JSON: ''" in text
    assert 'EXECUTOR_ALLOWED_NAMESPACES: ""' in text


def test_base_helm_values_have_no_meridian():
    text = (ROOT / "deploy" / "helm" / "sentinel" / "values.yaml").read_text()
    assert "meridian" not in text.lower()
    assert 'executorAllowedNamespaces: ""' in text
    assert 'namespace: "default"' in text


def test_edge_compose_does_not_hardcode_meridian_allowlist():
    text = (ROOT / "edge_mcp_servers" / "docker-compose.yaml").read_text()
    assert "EXECUTOR_ALLOWED_NAMESPACES=meridian" not in text
    assert "EXECUTOR_ALLOWED_NAMESPACES=${EXECUTOR_ALLOWED_NAMESPACES:-}" in text


def test_meridian_overlay_contract():
    overlay = ROOT / "deploy" / "examples" / "meridian"
    helm_values = (overlay / "helm-values.yaml").read_text()
    patch = (overlay / "patch-config.yaml").read_text()
    readme = (overlay / "README.md").read_text()

    assert "meridian-signals.meridian.svc.cluster.local" in helm_values
    assert 'executorAllowedNamespaces: "meridian"' in helm_values
    assert 'namespace: "meridian"' in helm_values
    assert "meridian-signals" in patch
    assert 'EXECUTOR_ALLOWED_NAMESPACES: "meridian"' in patch
    assert "Optional example client" in readme or "optional example client" in readme.lower()
