#!/usr/bin/env python3
"""
Executor MCP Server — the ACT phase's hands (Phase 1).

This is the WRITE counterpart to the read-only k8s_real server. It exposes a
narrow, allow-listed set of remediation tools that mutate the target cluster:
restart, scale, patch resource limits, and rollback. It is the boundary where
the agent's decisions finally become real `kubectl`-equivalent operations.

Safety model (defense in depth):
- Every tool defaults to ``dry_run=True``. Dry-run maps to the Kubernetes API's
  server-side dry-run (``dryRun=All``) — the apiserver validates the change but
  persists nothing — so even a "dry run" is a real, honest validation.
- Every tool passes through ``guardrails.guardrail_check`` first: action
  allow-list, namespace allow-list, and a scale-to-0 floor. These are operator-
  owned env vars, independent of the LLM.
- The server should be granted least-privilege RBAC (patch/scale on Deployments
  in the demo namespace only) — see README.

Returns JSON strings (matching the other edge servers), each including the
``kubectl`` equivalent for transparency and audit.
"""

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from typing import Optional

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from mcp.server.fastmcp import FastMCP

from guardrails import allowed_namespaces, guardrail_check

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

k8s_client = None


def _initialize_client() -> None:
    """Initialize the Kubernetes client (mirrors k8s_real's connection logic)."""
    global k8s_client

    api_server_host = os.getenv("KUBERNETES_API_SERVER_HOST")
    kubeconfig_path = os.getenv("KUBECONFIG") or os.path.expanduser("~/.kube/config")

    if api_server_host and os.path.exists(kubeconfig_path):
        import yaml

        logger.info(f"🔧 Patching kubeconfig to use host: {api_server_host}")
        with open(kubeconfig_path, "r") as f:
            config_data = yaml.safe_load(f)
        for cluster in config_data.get("clusters", []):
            server_url = cluster.get("cluster", {}).get("server", "")
            if "127.0.0.1" in server_url or "localhost" in server_url:
                cluster["cluster"]["server"] = server_url.replace(
                    "127.0.0.1", api_server_host
                ).replace("localhost", api_server_host)
        patched = "/tmp/kubeconfig_patched"
        with open(patched, "w") as f:
            yaml.dump(config_data, f)
        config.load_kube_config(config_file=patched)
        cfg = client.Configuration.get_default_copy()
        cfg.verify_ssl = False
        cfg.assert_hostname = False
        client.Configuration.set_default(cfg)
    else:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config(config_file=kubeconfig_path)

    k8s_client = client.ApiClient()
    logger.info("✅ Executor Kubernetes client initialized")


def _apps_api() -> Optional[client.AppsV1Api]:
    global k8s_client
    if k8s_client is None:
        try:
            _initialize_client()
        except Exception as e:
            logger.error(f"❌ Failed to initialize Kubernetes client: {e}")
            return None
    return client.AppsV1Api(k8s_client)


def _refused(action: str, namespace: str, reason: str) -> str:
    return json.dumps(
        {"tool": action, "namespace": namespace, "status": "REFUSED", "reason": reason},
        indent=2,
    )


def _dry_run_kwarg(dry_run: bool) -> dict:
    # Kubernetes server-side dry-run validates without persisting.
    return {"dry_run": "All"} if dry_run else {}


port = int(os.getenv("HTTP_PORT", "3000"))
host = os.getenv("HOST", "0.0.0.0")
mcp = FastMCP("executor-real-mcp-server", host=host, port=port)


@mcp.tool()
async def executor_health() -> str:
    """Report executor connectivity and the operator-configured safety envelope."""
    api = _apps_api()
    ok = False
    detail = "client not initialized"
    if api:
        try:
            await asyncio.to_thread(api.list_deployment_for_all_namespaces, limit=1)
            ok, detail = True, "connected"
        except Exception as e:
            detail = str(e)
    return json.dumps(
        {
            "status": "healthy" if ok else "unhealthy",
            "detail": detail,
            "allowed_namespaces": sorted(allowed_namespaces()),
            "min_replicas": int(os.getenv("EXECUTOR_MIN_REPLICAS", "1")),
        },
        indent=2,
    )


@mcp.tool()
async def restart_deployment(name: str, namespace: str = "demo-app", dry_run: bool = True) -> str:
    """Restart a deployment (rolling restart). Equivalent to `kubectl rollout restart`."""
    allowed, reason = guardrail_check("restart", namespace)
    if not allowed:
        return _refused("restart", namespace, reason)

    api = _apps_api()
    if not api:
        return _refused("restart", namespace, "Kubernetes client unavailable")

    now = datetime.now(timezone.utc).isoformat()
    body = {
        "spec": {"template": {"metadata": {"annotations": {
            "kubectl.kubernetes.io/restartedAt": now
        }}}}
    }
    kubectl = f"kubectl rollout restart deployment/{name} -n {namespace}"
    try:
        await asyncio.to_thread(
            api.patch_namespaced_deployment, name, namespace, body, **_dry_run_kwarg(dry_run)
        )
        return json.dumps({
            "tool": "restart", "name": name, "namespace": namespace,
            "dry_run": dry_run, "applied": not dry_run,
            "kubectl_equivalent": kubectl, "status": "OK", "restartedAt": now,
        }, indent=2)
    except ApiException as e:
        return json.dumps({"tool": "restart", "status": "ERROR", "kubectl_equivalent": kubectl,
                           "code": e.status, "reason": e.reason}, indent=2)


@mcp.tool()
async def scale_deployment(name: str, replicas: int, namespace: str = "demo-app", dry_run: bool = True) -> str:
    """Scale a deployment to `replicas`. Blocked below the min-replicas floor."""
    allowed, reason = guardrail_check("scale", namespace, {"replicas": replicas})
    if not allowed:
        return _refused("scale", namespace, reason)

    api = _apps_api()
    if not api:
        return _refused("scale", namespace, "Kubernetes client unavailable")

    body = {"spec": {"replicas": int(replicas)}}
    kubectl = f"kubectl scale deployment/{name} --replicas={int(replicas)} -n {namespace}"
    try:
        await asyncio.to_thread(
            api.patch_namespaced_deployment_scale, name, namespace, body, **_dry_run_kwarg(dry_run)
        )
        return json.dumps({
            "tool": "scale", "name": name, "namespace": namespace, "replicas": int(replicas),
            "dry_run": dry_run, "applied": not dry_run,
            "kubectl_equivalent": kubectl, "status": "OK",
        }, indent=2)
    except ApiException as e:
        return json.dumps({"tool": "scale", "status": "ERROR", "kubectl_equivalent": kubectl,
                           "code": e.status, "reason": e.reason}, indent=2)


@mcp.tool()
async def patch_resource_limits(
    name: str, container: str, memory: Optional[str] = None, cpu: Optional[str] = None,
    namespace: str = "demo-app", dry_run: bool = True,
) -> str:
    """Patch a container's resource limits (e.g. memory='512Mi', cpu='500m')."""
    allowed, reason = guardrail_check("patch_resource_limits", namespace)
    if not allowed:
        return _refused("patch_resource_limits", namespace, reason)
    if not memory and not cpu:
        return _refused("patch_resource_limits", namespace, "provide at least one of memory/cpu")

    api = _apps_api()
    if not api:
        return _refused("patch_resource_limits", namespace, "Kubernetes client unavailable")

    limits = {}
    if memory:
        limits["memory"] = memory
    if cpu:
        limits["cpu"] = cpu
    body = {"spec": {"template": {"spec": {"containers": [
        {"name": container, "resources": {"limits": limits}}
    ]}}}}
    kubectl = (
        f"kubectl set resources deployment/{name} -c {container} "
        f"--limits={','.join(f'{k}={v}' for k, v in limits.items())} -n {namespace}"
    )
    try:
        await asyncio.to_thread(
            api.patch_namespaced_deployment, name, namespace, body, **_dry_run_kwarg(dry_run)
        )
        return json.dumps({
            "tool": "patch_resource_limits", "name": name, "namespace": namespace,
            "container": container, "limits": limits,
            "dry_run": dry_run, "applied": not dry_run,
            "kubectl_equivalent": kubectl, "status": "OK",
        }, indent=2)
    except ApiException as e:
        return json.dumps({"tool": "patch_resource_limits", "status": "ERROR",
                           "kubectl_equivalent": kubectl, "code": e.status, "reason": e.reason}, indent=2)


@mcp.tool()
async def rollback_deployment(name: str, namespace: str = "demo-app", dry_run: bool = True) -> str:
    """Roll a deployment back to its previous revision (`kubectl rollout undo`).

    Dry-run returns the intended command without executing, since `rollout undo`
    server-side dry-run support is version-dependent.
    """
    allowed, reason = guardrail_check("rollback", namespace)
    if not allowed:
        return _refused("rollback", namespace, reason)

    kubectl = f"kubectl rollout undo deployment/{name} -n {namespace}"
    if dry_run:
        return json.dumps({
            "tool": "rollback", "name": name, "namespace": namespace,
            "dry_run": True, "applied": False,
            "kubectl_equivalent": kubectl,
            "status": "DRY_RUN", "detail": "Would run the command; not executed.",
        }, indent=2)

    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            ["kubectl", "rollout", "undo", f"deployment/{name}", "-n", namespace],
            capture_output=True, text=True, timeout=60,
        )
        return json.dumps({
            "tool": "rollback", "name": name, "namespace": namespace,
            "dry_run": False, "applied": proc.returncode == 0,
            "kubectl_equivalent": kubectl,
            "status": "OK" if proc.returncode == 0 else "ERROR",
            "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip(),
        }, indent=2)
    except Exception as e:
        return json.dumps({"tool": "rollback", "status": "ERROR",
                           "kubectl_equivalent": kubectl, "error": str(e)}, indent=2)


if __name__ == "__main__":
    logger.info("Starting Executor MCP server...")
    try:
        _initialize_client()
    except Exception as e:
        logger.warning(f"Deferred client init (will retry lazily): {e}")
    mcp.run(transport="sse")
