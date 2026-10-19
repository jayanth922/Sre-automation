#!/usr/bin/env python3
"""
Executor MCP Server — the ACT phase's hands (Phase 1).

This is the WRITE counterpart to the read-only k8s_real server. It exposes a
narrow, allow-listed set of remediation tools that mutate the target cluster:
restart, scale, patch resource limits, patch deployment env, recreate pod, and
rollback. It is the boundary where the agent's decisions finally become real
`kubectl`-equivalent operations.

One tool here reads rather than writes — ``get_deployment_config``. It lives on
this server, not on k8s_real, because it is the diagnostic half of
``patch_deployment_env``: a plan inspects what a config value currently is
through the same connection that would change it, and inspection stays modelled
as the read it is instead of borrowing a mutation's approval requirements.

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
from datetime import datetime, timezone
from typing import Optional

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from mcp.server.fastmcp import FastMCP

import rollout
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
        separators=(",", ":"),
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
        }, separators=(",", ":"))
    except ApiException as e:
        return json.dumps({"tool": "restart", "status": "ERROR", "kubectl_equivalent": kubectl,
                           "code": e.status, "reason": e.reason}, separators=(",", ":"))


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
        }, separators=(",", ":"))
    except ApiException as e:
        return json.dumps({"tool": "scale", "status": "ERROR", "kubectl_equivalent": kubectl,
                           "code": e.status, "reason": e.reason}, separators=(",", ":"))


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
        }, separators=(",", ":"))
    except ApiException as e:
        return json.dumps({"tool": "patch_resource_limits", "status": "ERROR",
                           "kubectl_equivalent": kubectl, "code": e.status, "reason": e.reason}, separators=(",", ":"))


def _container_spec(deployment, container: str):
    """Find a named container in a deployment, or None."""
    for spec in (deployment.spec.template.spec.containers or []):
        if spec.name == container:
            return spec
    return None


def _env_as_dict(container_spec) -> dict:
    """The container's literal env vars, name → value.

    Entries sourced from a ConfigMap or Secret (``valueFrom``) have no literal
    value; they are reported as null rather than omitted, so a caller can tell
    "not set" apart from "set, but indirected through a reference we are not
    reading".
    """
    out = {}
    for var in (getattr(container_spec, "env", None) or []):
        out[var.name] = var.value if var.value is not None else None
    return out


@mcp.tool()
async def get_deployment_config(
    name: str, container: Optional[str] = None, namespace: str = "demo-app",
) -> str:
    """Read a deployment's runtime configuration: env vars, resources, image.

    Read-only — the diagnostic counterpart to patch_deployment_env. Exists so a
    plan can inspect what a config value *currently is* before proposing to
    change it, without that inspection being modelled as a mutation and gated
    behind a human approval it does not need.
    """
    allowed, reason = guardrail_check("get_deployment_config", namespace)
    if not allowed:
        return _refused("get_deployment_config", namespace, reason)

    api = _apps_api()
    if not api:
        return _refused("get_deployment_config", namespace, "Kubernetes client unavailable")

    kubectl = f"kubectl get deployment/{name} -n {namespace} -o yaml"
    try:
        dep = await asyncio.to_thread(api.read_namespaced_deployment, name, namespace)
    except ApiException as e:
        return json.dumps({"tool": "get_deployment_config", "status": "ERROR",
                           "kubectl_equivalent": kubectl, "code": e.status,
                           "reason": e.reason}, separators=(",", ":"))

    containers = []
    for spec in (dep.spec.template.spec.containers or []):
        if container and spec.name != container:
            continue
        limits, requests = {}, {}
        if spec.resources:
            limits = dict(spec.resources.limits or {})
            requests = dict(spec.resources.requests or {})
        containers.append({
            "container": spec.name,
            "image": spec.image,
            "env": _env_as_dict(spec),
            "limits": limits,
            "requests": requests,
        })
    if container and not containers:
        return _refused("get_deployment_config", namespace,
                        f"container '{container}' not found in deployment '{name}'")

    return json.dumps({
        "tool": "get_deployment_config", "name": name, "namespace": namespace,
        "replicas": dep.spec.replicas, "containers": containers,
        "mutated": False, "kubectl_equivalent": kubectl, "status": "OK",
    }, separators=(",", ":"))


@mcp.tool()
async def patch_deployment_env(
    name: str, container: str, env: dict, namespace: str = "demo-app", dry_run: bool = True,
) -> str:
    """Set environment variables on a deployment's container (runtime config).

    The write counterpart to get_deployment_config, and the tool behind a
    `config_change` that is not a resource-limit change: feature flags,
    fault-injection toggles, log levels, timeouts.

    Two things this deliberately does not do. It does not send a bare env patch
    — a strategic-merge patch on `env` *replaces the whole list*, so patching
    one variable would silently delete every other one; it reads the current
    list and upserts into it instead. And it does not touch Secret- or
    ConfigMap-sourced entries (`valueFrom`): those are owned elsewhere, and
    overwriting one with a literal would quietly detach it from its source.
    """
    allowed, reason = guardrail_check("patch_deployment_env", namespace, {"env": env})
    if not allowed:
        return _refused("patch_deployment_env", namespace, reason)

    api = _apps_api()
    if not api:
        return _refused("patch_deployment_env", namespace, "Kubernetes client unavailable")

    desired = {str(k): (None if v is None else str(v)) for k, v in env.items()}
    kubectl = (
        f"kubectl set env deployment/{name} -c {container} "
        + " ".join(f"{k}={v}" for k, v in desired.items())
        + f" -n {namespace}"
    )

    try:
        dep = await asyncio.to_thread(api.read_namespaced_deployment, name, namespace)
    except ApiException as e:
        return json.dumps({"tool": "patch_deployment_env", "status": "ERROR",
                           "kubectl_equivalent": kubectl, "code": e.status,
                           "reason": e.reason}, separators=(",", ":"))

    spec = _container_spec(dep, container)
    if spec is None:
        return _refused("patch_deployment_env", namespace,
                        f"container '{container}' not found in deployment '{name}'")

    current = list(getattr(spec, "env", None) or [])
    by_name = {var.name: var for var in current}

    # Refuse rather than silently clobber an indirected value.
    indirected = [k for k in desired if k in by_name and by_name[k].value is None
                  and getattr(by_name[k], "value_from", None) is not None]
    if indirected:
        return _refused("patch_deployment_env", namespace, (
            f"env var(s) {sorted(indirected)} are sourced from a ConfigMap/Secret "
            "(valueFrom); the executor will not overwrite them with a literal"
        ))

    # Exact prior state for the keys being touched, so the audit trail carries a
    # real restore and not just "roll the whole deployment back". A key that is
    # absent today is reported null — restoring means removing it, not setting "".
    prior_env = {k: (by_name[k].value if k in by_name else None) for k in desired}

    merged = [{"name": v.name, "value": v.value} if getattr(v, "value_from", None) is None
              else {"name": v.name, "valueFrom": api.api_client.sanitize_for_serialization(v.value_from)}
              for v in current]
    index = {entry["name"]: i for i, entry in enumerate(merged)}
    for key, value in desired.items():
        if key in index:
            merged[index[key]] = {"name": key, "value": value}
        else:
            merged.append({"name": key, "value": value})

    body = {"spec": {"template": {"spec": {"containers": [
        {"name": container, "env": merged}
    ]}}}}
    try:
        await asyncio.to_thread(
            api.patch_namespaced_deployment, name, namespace, body, **_dry_run_kwarg(dry_run)
        )
        return json.dumps({
            "tool": "patch_deployment_env", "name": name, "namespace": namespace,
            "container": container, "env": desired, "prior_env": prior_env,
            "dry_run": dry_run, "applied": not dry_run,
            "kubectl_equivalent": kubectl, "status": "OK",
        }, separators=(",", ":"))
    except ApiException as e:
        return json.dumps({"tool": "patch_deployment_env", "status": "ERROR",
                           "kubectl_equivalent": kubectl, "code": e.status,
                           "reason": e.reason}, separators=(",", ":"))


async def _read_rollout(api: client.AppsV1Api, name: str, namespace: str):
    """The Deployment and its ReplicaSets as the apiserver's raw JSON.

    Raw, not the client's models: the rollback writes a ReplicaSet's template
    back verbatim, and a model round-trip silently drops any field this client
    version does not know.
    """
    dep_resp = await asyncio.to_thread(
        api.read_namespaced_deployment, name, namespace, _preload_content=False
    )
    deployment = json.loads(dep_resp.data)
    rs_resp = await asyncio.to_thread(
        api.list_namespaced_replica_set,
        namespace,
        label_selector=rollout.selector_string(deployment),
        _preload_content=False,
    )
    return deployment, json.loads(rs_resp.data).get("items") or []


@mcp.tool()
async def get_rollout_state(name: str, namespace: str = "demo-app") -> str:
    """Read a deployment's revision history: each ReplicaSet's template hash,
    revision, images, and which one the deployment currently runs.

    Read-only — the evidence a rollback is bound to. A rollback names its
    target by pod-template-hash (revision numbers renumber on every rollback)
    and the template it expects to replace; both come from this read.
    """
    allowed, reason = guardrail_check("get_rollout_state", namespace)
    if not allowed:
        return _refused("get_rollout_state", namespace, reason)

    api = _apps_api()
    if not api:
        return _refused("get_rollout_state", namespace, "Kubernetes client unavailable")

    kubectl = f"kubectl rollout history deployment/{name} -n {namespace}"
    try:
        deployment, replica_sets = await _read_rollout(api, name, namespace)
    except ApiException as e:
        return json.dumps({"tool": "get_rollout_state", "status": "ERROR",
                           "kubectl_equivalent": kubectl, "code": e.status,
                           "reason": e.reason}, separators=(",", ":"))

    return json.dumps({
        "tool": "get_rollout_state", "name": name, "namespace": namespace,
        **rollout.rollout_state(deployment, replica_sets),
        "mutated": False, "kubectl_equivalent": kubectl, "status": "OK",
    }, separators=(",", ":"))


# A write that lost a race re-reads and re-decides; after this many losses the
# deployment is changing faster than a precondition can be checked.
_ROLLBACK_ATTEMPTS = 3


@mcp.tool()
async def rollback_deployment(
    name: str,
    to_template_hash: str,
    expected_template_hash: str,
    namespace: str = "demo-app",
    dry_run: bool = True,
) -> str:
    """Roll a deployment back to the ReplicaSet with ``to_template_hash``,
    only if it is still on ``expected_template_hash``.

    Re-reads the deployment first. Already on the target: nothing is written
    and the result says so. On the expected template: the target's template is
    written back with a compare-and-swap on resourceVersion. Anything else is
    refused as state drift — the deployment is not in the state the rollback
    was approved against. Dry-run validates the write server-side
    (``dryRun=All``) without persisting it.
    """
    allowed, reason = guardrail_check(
        "rollback",
        namespace,
        {"to_template_hash": to_template_hash, "expected_template_hash": expected_template_hash},
    )
    if not allowed:
        return _refused("rollback", namespace, reason)

    api = _apps_api()
    if not api:
        return _refused("rollback", namespace, "Kubernetes client unavailable")

    kubectl = rollout.rollback_command(name, namespace, to_template_hash, expected_template_hash)
    base = {
        "tool": "rollback", "name": name, "namespace": namespace, "dry_run": dry_run,
        "to_template_hash": to_template_hash,
        "expected_template_hash": expected_template_hash,
        "kubectl_equivalent": kubectl,
    }
    try:
        for _ in range(_ROLLBACK_ATTEMPTS):
            deployment, replica_sets = await _read_rollout(api, name, namespace)
            verdict, why = rollout.classify_rollback(
                deployment, replica_sets, to_template_hash, expected_template_hash
            )
            if verdict == rollout.ALREADY_AT_TARGET:
                return json.dumps({**base, "status": "ALREADY_AT_TARGET", "applied": False,
                                   "mutated": False, "detail": why}, separators=(",", ":"))
            if verdict != rollout.APPLY:
                state = rollout.rollout_state(deployment, replica_sets)
                return json.dumps({**base, "status": "STATE_DRIFT", "applied": False,
                                   "refusal_code": "state_drift", "drift": verdict,
                                   "reason": why,
                                   "current_template_hash": state["current_template_hash"]},
                                  separators=(",", ":"))
            target = rollout.find_replica_set(deployment, replica_sets, to_template_hash)
            try:
                await asyncio.to_thread(
                    api.patch_namespaced_deployment, name, namespace,
                    rollout.rollback_patch(deployment, target), **_dry_run_kwarg(dry_run),
                )
            except ApiException as e:
                # 422 is the resourceVersion `test` failing (and 409 a plain
                # conflict): the object moved after the read. Decide again on
                # the new object rather than writing over what changed.
                if e.status in (409, 422):
                    continue
                raise
            return json.dumps({**base, "status": "DRY_RUN" if dry_run else "OK",
                               "applied": not dry_run,
                               "from_template_hash": expected_template_hash,
                               "detail": why}, separators=(",", ":"))
        return json.dumps({**base, "status": "ERROR",
                           "reason": f"the deployment kept changing; gave up after "
                                     f"{_ROLLBACK_ATTEMPTS} attempts without writing"},
                          separators=(",", ":"))
    except ApiException as e:
        return json.dumps({**base, "status": "ERROR", "code": e.status, "reason": e.reason},
                          separators=(",", ":"))


@mcp.tool()
async def recreate_pod(name: str, namespace: str = "demo-app", dry_run: bool = True) -> str:
    """Delete a single pod so its controller (ReplicaSet/StatefulSet) recreates it.

    Narrower than ``restart`` (which rolling-restarts an entire deployment): use
    this to clear one stuck/crash-looping pod — e.g. wedged in Terminating, or
    the lone bad replica in an otherwise healthy set — without disturbing its
    siblings. Equivalent to `kubectl delete pod`. Reversible in the same sense
    as a restart: the controller recreates the pod immediately, it does not
    reduce capacity.
    """
    allowed, reason = guardrail_check("recreate_pod", namespace)
    if not allowed:
        return _refused("recreate_pod", namespace, reason)

    api = _core_api()
    if not api:
        return _refused("recreate_pod", namespace, "Kubernetes client unavailable")

    kubectl = f"kubectl delete pod/{name} -n {namespace}"
    try:
        await asyncio.to_thread(
            api.delete_namespaced_pod, name, namespace, **_dry_run_kwarg(dry_run)
        )
        return json.dumps({
            "tool": "recreate_pod", "name": name, "namespace": namespace,
            "dry_run": dry_run, "applied": not dry_run,
            "kubectl_equivalent": kubectl, "status": "OK",
        }, separators=(",", ":"))
    except ApiException as e:
        return json.dumps({"tool": "recreate_pod", "status": "ERROR", "kubectl_equivalent": kubectl,
                           "code": e.status, "reason": e.reason}, separators=(",", ":"))


if __name__ == "__main__":
    logger.info("Starting Executor MCP server...")
    try:
        _initialize_client()
    except Exception as e:
        logger.warning(f"Deferred client init (will retry lazily): {e}")
    from mcp_auth import run_authenticated_sse
    run_authenticated_sse(mcp, host=host, port=port)
