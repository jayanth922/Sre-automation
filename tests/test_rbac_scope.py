#!/usr/bin/env python3
"""T10 source-contract checks for least-privilege Kubernetes RBAC."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _documents(source: str):
    return [document.strip() for document in source.split("---") if document.strip()]


def _document(source: str, kind: str, name: str) -> str:
    for document in _documents(source):
        lines = {line.strip() for line in document.splitlines()}
        if f"kind: {kind}" in lines and f"name: {name}" in lines:
            return document
    raise AssertionError(f"missing {kind}/{name}")


def _assert_delete_is_pods_only(source: str):
    lines = source.splitlines()
    for index, line in enumerate(lines):
        if "resources:" not in line:
            continue
        verbs = next(
            (candidate for candidate in lines[index + 1 : index + 4] if "verbs:" in candidate),
            "",
        )
        if '"delete"' in verbs:
            assert line.strip() == 'resources: ["pods"]'


def test_plain_manifest_actuator_is_namespaced_and_delete_is_pods_only():
    source = (ROOT / "deploy" / "k8s" / "rbac.yaml").read_text()
    role = _document(source, "Role", "sentinel-actuator")
    binding = _document(source, "RoleBinding", "sentinel-actuator")

    assert "metadata:\n  name: sentinel-actuator\n  namespace: default" in role
    assert "metadata:\n  name: sentinel-actuator\n  namespace: default" in binding
    assert "name: sentinel-actuator\n    namespace: sentinel" in binding
    assert 'resources: ["nodes"' not in role
    assert '"namespaces"' not in role
    assert "kind: Role\n  name: sentinel-actuator" in binding
    _assert_delete_is_pods_only(role)


def test_plain_manifest_observer_remains_read_only():
    source = (ROOT / "deploy" / "k8s" / "rbac.yaml").read_text()
    observer = _document(source, "ClusterRole", "sentinel-observer")
    assert 'verbs: ["get", "list", "watch"]' in observer
    assert '"delete"' not in observer


def test_helm_defaults_namespaced_and_cluster_wide_is_opt_in():
    values = (ROOT / "deploy" / "helm" / "sentinel" / "values.yaml").read_text()
    template = (
        ROOT / "deploy" / "helm" / "sentinel" / "templates" / "rbac.yaml"
    ).read_text()

    assert "namespaced:\n    enabled: true" in values
    assert 'namespace: "default"' in values
    assert "clusterWide:\n    enabled: false" in values
    assert "if .Values.rbac.namespaced.enabled" in template
    assert "if .Values.rbac.clusterWide.enabled" in template
    assert "namespace: {{ .Values.rbac.namespaced.namespace }}" in template
    assert "namespace: {{ .Release.Namespace }}" in template
    # The always-rendered sandbox Role (namespace-isolated, see the comment
    # above it in rbac.yaml) legitimately grants delete on batch/jobs to tear
    # down ephemeral code-fix-verification Jobs -- a distinct concern from the
    # actuator's pods-only delete invariant, so scope the check past that block.
    actuator_scoped = template.split("{{ if .Values.rbac.namespaced.enabled }}", 1)[1]
    _assert_delete_is_pods_only(actuator_scoped)


def test_ci_renders_default_and_opt_in_rbac_modes():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    check = (ROOT / "scripts" / "check_helm_rbac.sh").read_text()
    live = (ROOT / "scripts" / "check_live_rbac.sh").read_text()
    deploy_templates = (ROOT / "scripts" / "check_deploy_templates.sh").read_text()

    # CI may invoke RBAC checks directly or via the deploy-templates wrapper (P08).
    assert (
        "check_helm_rbac.sh" in workflow
        or (
            "check_deploy_templates.sh" in workflow
            and "check_helm_rbac.sh" in deploy_templates
        )
    )
    assert "check_helm_ws.sh" in workflow or "check_helm_ws.sh" in deploy_templates
    assert "default chart rendered cluster-wide RBAC" in check
    assert "--set rbac.clusterWide.enabled=true" in check
    assert "assert_denied delete services" in live
    assert "assert_denied delete pods" in live
    assert "assert_denied delete nodes" in live
