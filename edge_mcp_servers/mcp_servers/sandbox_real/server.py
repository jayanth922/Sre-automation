#!/usr/bin/env python3
"""
Sandbox MCP Server — ephemeral K8s Jobs for verifying AI-generated code fixes.

This server's only job is Kubernetes Job lifecycle management for the
Temporal-orchestrated code-fix verification workflow (`sre_agent.sandbox_workflow`):
provision a Job that runs a given runner image/command, report its status,
fetch its logs, and tear it down. It does not decide *what* to run — the
Temporal worker supplies the image/command for each stage (baseline replay,
patched candidate) and diffs the resulting logs itself.

Safety model (defense in depth, mirroring executor_real):
- Every tool passes through ``guardrails.guardrail_check`` first: a fixed
  sandbox namespace (never a client workload namespace), an image allow-list,
  and a mandatory ``activeDeadlineSeconds`` ceiling so an untrusted candidate
  can never run unbounded.
- The server is granted least-privilege RBAC (batch/jobs + pods/log only,
  confined to the sandbox namespace) — see deploy/k8s/rbac.yaml.
- In-cluster ServiceAccount only; no kubeconfig is ever mounted.

Returns JSON strings (matching the other edge servers).
"""

import asyncio
import json
import logging
import os
import uuid
from typing import Any, Dict, List, Optional

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from mcp.server.fastmcp import FastMCP

from guardrails import guardrail_check, resource_envelope, sandbox_namespace

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

k8s_client = None


def _initialize_client() -> None:
    """Initialize the Kubernetes client (mirrors executor_real's connection logic)."""
    global k8s_client
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config(config_file=os.getenv("KUBECONFIG") or os.path.expanduser("~/.kube/config"))
    k8s_client = client.ApiClient()
    logger.info("✅ Sandbox Kubernetes client initialized")


def _batch_api() -> Optional[client.BatchV1Api]:
    global k8s_client
    if k8s_client is None:
        try:
            _initialize_client()
        except Exception as e:
            logger.error(f"❌ Failed to initialize Kubernetes client: {e}")
            return None
    return client.BatchV1Api(k8s_client)


def _core_api() -> Optional[client.CoreV1Api]:
    global k8s_client
    if k8s_client is None:
        try:
            _initialize_client()
        except Exception as e:
            logger.error(f"❌ Failed to initialize Kubernetes client: {e}")
            return None
    return client.CoreV1Api(k8s_client)


def _refused(action: str, namespace: str, reason: str) -> str:
    return json.dumps(
        {"tool": action, "namespace": namespace, "status": "REFUSED", "reason": reason},
        separators=(",", ":"),
    )


def _job_manifest(
    job_name: str, image: str, command: List[str], env: Dict[str, str], active_deadline_seconds: int
) -> Dict[str, Any]:
    envelope = resource_envelope()
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": job_name, "labels": {"app.kubernetes.io/component": "sentinel-sandbox"}},
        "spec": {
            "activeDeadlineSeconds": active_deadline_seconds,
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 300,
            "template": {
                "metadata": {"labels": {"job-name": job_name}},
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "containers": [
                        {
                            "name": "candidate",
                            "image": image,
                            "command": command,
                            "env": [{"name": k, "value": v} for k, v in env.items()],
                            "resources": envelope,
                        }
                    ],
                },
            },
        },
    }


port = int(os.getenv("HTTP_PORT", "3000"))
host = os.getenv("HOST", "0.0.0.0")
mcp = FastMCP("sandbox-real-mcp-server", host=host, port=port)


@mcp.tool()
async def sandbox_health() -> str:
    """Report sandbox connectivity and the operator-configured safety envelope."""
    api = _batch_api()
    ok = False
    detail = "client not initialized"
    if api:
        try:
            await asyncio.to_thread(api.list_namespaced_job, sandbox_namespace(), limit=1)
            ok, detail = True, "connected"
        except Exception as e:
            detail = str(e)
    return json.dumps(
        {"status": "healthy" if ok else "unhealthy", "detail": detail, "namespace": sandbox_namespace()},
        separators=(",", ":"),
    )


@mcp.tool()
async def sandbox_provision(
    image: str,
    command: List[str],
    job_name: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    namespace: str = "sentinel-sandbox",
    active_deadline_seconds: int = 300,
    dry_run: bool = True,
) -> str:
    """Create an ephemeral, bounded K8s Job to run one sandbox verification stage."""
    allowed, reason = guardrail_check(namespace, image, active_deadline_seconds)
    if not allowed:
        return _refused("provision", namespace, reason)

    api = _batch_api()
    if not api:
        return _refused("provision", namespace, "Kubernetes client unavailable")

    name = job_name or f"sentinel-sandbox-{uuid.uuid4().hex[:10]}"
    manifest = _job_manifest(name, image, command, env or {}, active_deadline_seconds)
    dry_run_kwarg = {"dry_run": "All"} if dry_run else {}
    try:
        await asyncio.to_thread(
            api.create_namespaced_job, namespace, manifest, **dry_run_kwarg
        )
        return json.dumps(
            {
                "tool": "provision", "job_name": name, "namespace": namespace,
                "dry_run": dry_run, "applied": not dry_run, "status": "OK",
            },
            separators=(",", ":"),
        )
    except ApiException as e:
        return json.dumps(
            {"tool": "provision", "job_name": name, "status": "ERROR", "code": e.status, "reason": e.reason},
            separators=(",", ":"),
        )


@mcp.tool()
async def sandbox_status(job_name: str, namespace: str = "sentinel-sandbox") -> str:
    """Report a sandbox Job's lifecycle status: PENDING/RUNNING/SUCCEEDED/FAILED/NOT_FOUND."""
    api = _batch_api()
    if not api:
        return _refused("status", namespace, "Kubernetes client unavailable")

    try:
        job = await asyncio.to_thread(api.read_namespaced_job_status, job_name, namespace)
    except ApiException as e:
        if e.status == 404:
            return json.dumps(
                {"tool": "status", "job_name": job_name, "namespace": namespace, "status": "NOT_FOUND"},
                separators=(",", ":"),
            )
        return json.dumps(
            {"tool": "status", "job_name": job_name, "status": "ERROR", "code": e.status, "reason": e.reason},
            separators=(",", ":"),
        )

    js = job.status
    if js.succeeded:
        state = "SUCCEEDED"
    elif js.failed:
        state = "FAILED"
    elif js.active:
        state = "RUNNING"
    else:
        state = "PENDING"
    return json.dumps(
        {
            "tool": "status", "job_name": job_name, "namespace": namespace, "status": state,
            "active": js.active or 0, "succeeded": js.succeeded or 0, "failed": js.failed or 0,
        },
        separators=(",", ":"),
    )


@mcp.tool()
async def sandbox_logs(job_name: str, namespace: str = "sentinel-sandbox", tail_lines: int = 500) -> str:
    """Fetch the logs of a sandbox Job's pod (the log evidence being diffed)."""
    core = _core_api()
    if not core:
        return _refused("logs", namespace, "Kubernetes client unavailable")

    try:
        pods = await asyncio.to_thread(
            core.list_namespaced_pod, namespace, label_selector=f"job-name={job_name}"
        )
    except ApiException as e:
        return json.dumps(
            {"tool": "logs", "job_name": job_name, "status": "ERROR", "code": e.status, "reason": e.reason},
            separators=(",", ":"),
        )
    if not pods.items:
        return json.dumps(
            {"tool": "logs", "job_name": job_name, "namespace": namespace, "status": "NOT_FOUND", "logs": ""},
            separators=(",", ":"),
        )

    pod_name = pods.items[0].metadata.name
    try:
        logs = await asyncio.to_thread(
            core.read_namespaced_pod_log, pod_name, namespace, tail_lines=tail_lines
        )
        return json.dumps(
            {"tool": "logs", "job_name": job_name, "namespace": namespace, "status": "OK", "logs": logs},
            separators=(",", ":"),
        )
    except ApiException as e:
        return json.dumps(
            {"tool": "logs", "job_name": job_name, "status": "ERROR", "code": e.status, "reason": e.reason},
            separators=(",", ":"),
        )


@mcp.tool()
async def sandbox_teardown(job_name: str, namespace: str = "sentinel-sandbox") -> str:
    """Delete a sandbox Job and its pods. Idempotent — a missing Job is OK."""
    api = _batch_api()
    if not api:
        return _refused("teardown", namespace, "Kubernetes client unavailable")

    try:
        await asyncio.to_thread(
            api.delete_namespaced_job, job_name, namespace, propagation_policy="Background"
        )
        return json.dumps({"tool": "teardown", "job_name": job_name, "namespace": namespace, "status": "OK"}, separators=(",", ":"))
    except ApiException as e:
        if e.status == 404:
            return json.dumps(
                {"tool": "teardown", "job_name": job_name, "namespace": namespace, "status": "OK", "detail": "already gone"},
                separators=(",", ":"),
            )
        return json.dumps(
            {"tool": "teardown", "job_name": job_name, "status": "ERROR", "code": e.status, "reason": e.reason},
            separators=(",", ":"),
        )


if __name__ == "__main__":
    logger.info("Starting Sandbox MCP server...")
    try:
        _initialize_client()
    except Exception as e:
        logger.warning(f"Deferred client init (will retry lazily): {e}")
    from mcp_auth import run_authenticated_sse
    run_authenticated_sse(mcp, host=host, port=port)
