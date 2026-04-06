#!/usr/bin/env python3
"""
Real Kubernetes MCP Server

This MCP server directly uses the Kubernetes Python client library
instead of calling mock APIs. It provides production-ready Kubernetes
operations through the Model Context Protocol.
"""

import asyncio
import logging
import os
import time
import json
from typing import Any, Dict, List, Optional

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Initialize Kubernetes client
k8s_client = None
v1 = None
last_connection_attempt = 0
CONNECTION_RETRY_INTERVAL = 10 

def get_k8s_api() -> Optional[client.CoreV1Api]:
    """
    Get Kubernetes CoreV1Api, attempting to initialize if necessary.
    Implements lazy loading and backoff.
    """
    global k8s_client, v1, last_connection_attempt
    
    if v1:
        return v1

    # Check retry interval
    now = time.time()
    if now - last_connection_attempt < CONNECTION_RETRY_INTERVAL:
        return None
        
    last_connection_attempt = now

    try:
        initialize_kubernetes_client_logic()
        return v1
    except Exception as e:
        logger.error(f"❌ Failed to initialize Kubernetes client: {e}")
        return None

def get_apps_v1_api() -> Optional[client.AppsV1Api]:
    """Get AppsV1Api, ensuring connection exists."""
    global k8s_client
    get_k8s_api() # Trigger initialization if needed
    
    if k8s_client:
        return client.AppsV1Api(k8s_client)
    return None


def _format_owner_references(owner_references):
    return [
        {
            "kind": owner.kind,
            "name": owner.name,
            "uid": owner.uid,
            "controller": owner.controller,
            "block_owner_deletion": owner.block_owner_deletion,
        }
        for owner in (owner_references or [])
    ]


def _format_namespace_entry(namespace) -> Dict[str, Any]:
    return {
        "name": namespace.metadata.name,
        "phase": getattr(namespace.status, "phase", None),
        "labels": dict(namespace.metadata.labels or {}),
        "annotations": dict(namespace.metadata.annotations or {}),
        "creation_timestamp": namespace.metadata.creation_timestamp.isoformat()
        if namespace.metadata.creation_timestamp
        else None,
    }


def _format_pod_entry(pod) -> Dict[str, Any]:
    return {
        "name": pod.metadata.name,
        "namespace": pod.metadata.namespace,
        "phase": pod.status.phase,
        "node_name": pod.spec.node_name,
        "pod_ip": pod.status.pod_ip,
        "host_ip": pod.status.host_ip,
        "labels": dict(pod.metadata.labels or {}),
        "annotations": dict(pod.metadata.annotations or {}),
        "owner_references": _format_owner_references(pod.metadata.owner_references),
        "containers": [
            {
                "name": container.name,
                "image": container.image,
                "ports": [
                    {"container_port": port.container_port, "protocol": port.protocol}
                    for port in (container.ports or [])
                ],
            }
            for container in (pod.spec.containers or [])
        ],
        "container_statuses": [
            {
                "name": status.name,
                "ready": status.ready,
                "restart_count": status.restart_count,
                "image": getattr(status, "image", None),
                "image_id": getattr(status, "image_id", None),
            }
            for status in (pod.status.container_statuses or [])
        ],
        "start_time": pod.status.start_time.isoformat() if pod.status.start_time else None,
    }


def _format_service_entry(service) -> Dict[str, Any]:
    def _normalize_list(value):
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return list(value)

    cluster_ips = _normalize_list(getattr(service.spec, "cluster_ips", None))
    external_ips = _normalize_list(getattr(service.spec, "external_ips", None))
    if not external_ips:
        external_ips = _normalize_list(getattr(service.spec, "external_i_ps", None))

    return {
        "name": service.metadata.name,
        "namespace": service.metadata.namespace,
        "type": service.spec.type,
        "cluster_ip": service.spec.cluster_ip,
        "cluster_ips": cluster_ips,
        "external_ips": external_ips,
        "selector": dict(service.spec.selector or {}),
        "session_affinity": service.spec.session_affinity,
        "ports": [
            {
                "name": port.name,
                "port": port.port,
                "target_port": getattr(port, "target_port", None),
                "node_port": getattr(port, "node_port", None),
                "protocol": port.protocol,
            }
            for port in (service.spec.ports or [])
        ],
        "labels": dict(service.metadata.labels or {}),
        "annotations": dict(service.metadata.annotations or {}),
    }


def _format_deployment_entry(deployment) -> Dict[str, Any]:
    return {
        "name": deployment.metadata.name,
        "namespace": deployment.metadata.namespace,
        "replicas": deployment.spec.replicas,
        "ready_replicas": deployment.status.ready_replicas or 0,
        "available_replicas": deployment.status.available_replicas or 0,
        "updated_replicas": deployment.status.updated_replicas or 0,
        "unavailable_replicas": deployment.status.unavailable_replicas or 0,
        "labels": dict(deployment.metadata.labels or {}),
        "selector": deployment.spec.selector.match_labels if deployment.spec.selector else {},
        "strategy": getattr(deployment.spec.strategy, "type", None),
        "creation_timestamp": deployment.metadata.creation_timestamp.isoformat()
        if deployment.metadata.creation_timestamp
        else None,
    }


def _format_event_entry(event) -> Dict[str, Any]:
    involved_object = getattr(event, "involved_object", None) or getattr(event, "regarding", None)
    source = getattr(event, "source", None)
    return {
        "name": getattr(event.metadata, "name", None),
        "namespace": getattr(event.metadata, "namespace", None),
        "type": getattr(event, "type", None),
        "reason": getattr(event, "reason", None),
        "message": getattr(event, "message", None),
        "count": getattr(event, "count", None),
        "first_timestamp": getattr(event, "first_timestamp", None).isoformat()
        if getattr(event, "first_timestamp", None)
        else None,
        "last_timestamp": getattr(event, "last_timestamp", None).isoformat()
        if getattr(event, "last_timestamp", None)
        else None,
        "involved_object": {
            "kind": getattr(involved_object, "kind", None),
            "name": getattr(involved_object, "name", None),
            "namespace": getattr(involved_object, "namespace", None),
            "uid": getattr(involved_object, "uid", None),
        },
        "source": {
            "component": getattr(source, "component", None),
            "host": getattr(source, "host", None),
        },
    }


def _format_endpoints_entry(endpoints) -> Dict[str, Any]:
    subsets = []
    for subset in endpoints.subsets or []:
        subsets.append(
            {
                "addresses": [
                    {
                        "ip": address.ip,
                        "node_name": getattr(address, "node_name", None),
                        "target_ref": {
                            "kind": getattr(address.target_ref, "kind", None) if address.target_ref else None,
                            "name": getattr(address.target_ref, "name", None) if address.target_ref else None,
                            "namespace": getattr(address.target_ref, "namespace", None) if address.target_ref else None,
                        },
                    }
                    for address in (subset.addresses or [])
                ],
                "not_ready_addresses": [
                    {
                        "ip": address.ip,
                        "node_name": getattr(address, "node_name", None),
                    }
                    for address in (subset.not_ready_addresses or [])
                ],
                "ports": [
                    {
                        "name": port.name,
                        "port": port.port,
                        "protocol": port.protocol,
                    }
                    for port in (subset.ports or [])
                ],
            }
        )

    return {
        "name": endpoints.metadata.name,
        "namespace": endpoints.metadata.namespace,
        "subsets": subsets,
    }

def initialize_kubernetes_client_logic():
    """Internal logic to initialize the client."""
    global k8s_client, v1
    
    # Check for API Server Host override (e.g. for Kind on Docker Host)
    api_server_host = os.getenv("KUBERNETES_API_SERVER_HOST")
    kubeconfig_path = os.getenv("KUBECONFIG") or os.path.expanduser("~/.kube/config")

    if api_server_host and kubeconfig_path and os.path.exists(kubeconfig_path):
        logger.info(f"🔧 Patching kubeconfig to use host: {api_server_host}")
        import yaml
        
        with open(kubeconfig_path, 'r') as f:
            config_data = yaml.safe_load(f)
        
        # Patch the server URL in all clusters
        for cluster in config_data.get('clusters', []):
            server_url = cluster.get('cluster', {}).get('server', '')
            if '127.0.0.1' in server_url or 'localhost' in server_url:
                # Replace 127.0.0.1 or localhost with the docker host alias
                new_url = server_url.replace('127.0.0.1', api_server_host).replace('localhost', api_server_host)
                cluster['cluster']['server'] = new_url
                logger.info(f"   - Patched cluster '{cluster.get('name')}' server to: {new_url}")
        
        # Save patched config to temp file
        patched_config_path = "/tmp/kubeconfig_patched"
        with open(patched_config_path, 'w') as f:
            yaml.dump(config_data, f)
        
        # Load the patched config
        config.load_kube_config(config_file=patched_config_path)
        configuration = client.Configuration.get_default_copy()
        configuration.verify_ssl = False
        configuration.assert_hostname = False
        client.Configuration.set_default(configuration)
        logger.info(f"✅ Loaded PATCHED Kubernetes config from {patched_config_path}")

    # Try in-cluster config first (if running in a pod and no override active)
    elif not os.getenv("KUBERNETES_API_SERVER_HOST"):
        try:
            config.load_incluster_config()
            logger.info("✅ Loaded in-cluster Kubernetes config")
        except config.ConfigException:
            # Fall back to local kubeconfig
            if os.path.exists(kubeconfig_path):
                config.load_kube_config(config_file=kubeconfig_path)
                logger.info(f"✅ Loaded Kubernetes config from {kubeconfig_path}")
            else:
                logger.warning(f"⚠️ Kubeconfig not found at {kubeconfig_path}")
                raise Exception(f"Kubeconfig missing at {kubeconfig_path}")
    else:
            # Fallback if patch logic didn't trigger but env var was set (e.g. invalid path)
            if os.path.exists(kubeconfig_path):
                config.load_kube_config(config_file=kubeconfig_path)
            else:
                config.load_kube_config()

    # Create API client
    k8s_client = client.ApiClient()
    v1 = client.CoreV1Api(k8s_client)
    logger.info("✅ Kubernetes client initialized successfully")


# Create FastMCP server
port = int(os.getenv("HTTP_PORT", "3000"))
host = os.getenv("HOST", "0.0.0.0")

mcp = FastMCP("k8s-real-mcp-server", host=host, port=port)


# Tool parameter models
class GetPodStatusParams(BaseModel):
    """Parameters for get_pod_status tool."""
    pod_name: str = Field(..., description="Name of the pod")
    namespace: str = Field(default="default", description="Kubernetes namespace")


class GetDeploymentStatusParams(BaseModel):
    """Parameters for get_deployment_status tool."""
    deployment_name: str = Field(..., description="Name of the deployment")
    namespace: str = Field(default="default", description="Kubernetes namespace")


class GetNodeStatusParams(BaseModel):
    """Parameters for get_node_status tool."""
    node_name: Optional[str] = Field(None, description="Specific node name (optional)")


class ListNamespacesParams(BaseModel):
    """Parameters for list_namespaces tool."""

    limit: int = Field(default=100, ge=1, le=1000, description="Maximum number of namespaces to return")


class ListPodsParams(BaseModel):
    """Parameters for list_pods tool."""

    namespace: Optional[str] = Field(
        None, description="Namespace to filter by, or omit for all namespaces"
    )
    label_selector: Optional[str] = Field(None, description="Label selector to filter pods")
    limit: int = Field(default=200, ge=1, le=2000, description="Maximum number of pods to return")


class ListServicesParams(BaseModel):
    """Parameters for list_services tool."""

    namespace: Optional[str] = Field(
        None, description="Namespace to filter by, or omit for all namespaces"
    )
    label_selector: Optional[str] = Field(None, description="Label selector to filter services")
    limit: int = Field(default=200, ge=1, le=2000, description="Maximum number of services to return")


class ListDeploymentsParams(BaseModel):
    """Parameters for list_deployments tool."""

    namespace: Optional[str] = Field(
        None, description="Namespace to filter by, or omit for all namespaces"
    )
    limit: int = Field(default=200, ge=1, le=2000, description="Maximum number of deployments to return")


class ListEventsParams(BaseModel):
    """Parameters for list_events tool."""

    namespace: Optional[str] = Field(
        None, description="Namespace to filter by, or omit for all namespaces"
    )
    involved_object_name: Optional[str] = Field(
        None, description="Only return events tied to a specific resource name"
    )
    limit: int = Field(default=200, ge=1, le=2000, description="Maximum number of events to return")


class GetServiceEndpointsParams(BaseModel):
    """Parameters for get_service_endpoints tool."""

    service_name: str = Field(..., description="Name of the Kubernetes service")
    namespace: str = Field(default="default", description="Kubernetes namespace")


class GetPodLogsParams(BaseModel):
    """Parameters for get_pod_logs tool."""

    pod_name: str = Field(..., description="Name of the pod")
    namespace: str = Field(default="default", description="Kubernetes namespace")
    container: Optional[str] = Field(None, description="Optional container name")
    tail_lines: int = Field(default=200, ge=1, le=2000, description="Maximum log lines to return")
    timestamps: bool = Field(default=False, description="Include timestamps in logs")


# Implementation Helpers

async def handle_get_pod_status(params: GetPodStatusParams) -> str:
    """Get pod status using Kubernetes API."""
    logger.info(f"Getting pod status: {params.pod_name} in namespace {params.namespace}")

    api = get_k8s_api()
    if not api:
        return "Error: Kubernetes client not initialized. Cluster might be unreachable."

    # Run in thread pool to avoid blocking
    loop = asyncio.get_event_loop()
    try:
        pod = await loop.run_in_executor(
            None, api.read_namespaced_pod_status, params.pod_name, params.namespace
        )

        # Format response
        result = {
            "name": pod.metadata.name,
            "namespace": pod.metadata.namespace,
            "phase": pod.status.phase,
            "conditions": [
                {
                    "type": c.type,
                    "status": c.status,
                    "reason": c.reason,
                    "message": c.message,
                }
                for c in (pod.status.conditions or [])
            ],
            "container_statuses": [
                {
                    "name": cs.name,
                    "ready": cs.ready,
                    "restart_count": cs.restart_count,
                    "state": {
                        "running": cs.state.running is not None,
                        "waiting": (
                            {"reason": cs.state.waiting.reason, "message": cs.state.waiting.message}
                            if cs.state.waiting
                            else None
                        ),
                        "terminated": (
                            {
                                "exit_code": cs.state.terminated.exit_code,
                                "reason": cs.state.terminated.reason,
                            }
                            if cs.state.terminated
                            else None
                        ),
                    },
                }
                for cs in (pod.status.container_statuses or [])
            ],
            "pod_ip": pod.status.pod_ip,
            "host_ip": pod.status.host_ip,
            "start_time": pod.status.start_time.isoformat() if pod.status.start_time else None,
        }

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error getting pod status: {e}")
        return f"Error getting pod status: {e}"


async def handle_get_deployment_status(params: GetDeploymentStatusParams) -> str:
    """Get deployment status using Kubernetes API."""
    logger.info(
        f"Getting deployment status: {params.deployment_name} in namespace {params.namespace}"
    )

    apps_v1 = get_apps_v1_api()
    if not apps_v1:
        return "Error: Kubernetes client not initialized."

    # Run in thread pool to avoid blocking
    loop = asyncio.get_event_loop()
    try:
        deployment = await loop.run_in_executor(
            None,
            apps_v1.read_namespaced_deployment_status,
            params.deployment_name,
            params.namespace,
        )

        result = {
            "name": deployment.metadata.name,
            "namespace": deployment.metadata.namespace,
            "replicas": deployment.spec.replicas,
            "ready_replicas": deployment.status.ready_replicas or 0,
            "available_replicas": deployment.status.available_replicas or 0,
            "unavailable_replicas": deployment.status.unavailable_replicas or 0,
            "updated_replicas": deployment.status.updated_replicas or 0,
            "conditions": [
                {
                    "type": c.type,
                    "status": c.status,
                    "reason": c.reason,
                    "message": c.message,
                }
                for c in (deployment.status.conditions or [])
            ],
        }

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error getting deployment status: {e}")
        return f"Error getting deployment status: {e}"


async def handle_get_node_status(params: GetNodeStatusParams) -> str:
    """Get node status using Kubernetes API."""
    
    api = get_k8s_api()
    if not api:
        return "Error: Kubernetes client not initialized."

    loop = asyncio.get_event_loop()
    try:
        if params.node_name:
            logger.info(f"Getting node status: {params.node_name}")
            node = await loop.run_in_executor(None, api.read_node_status, params.node_name)
            nodes = [node]
        else:
            logger.info("Getting status of all nodes")
            node_list = await loop.run_in_executor(None, api.list_node)
            nodes = node_list.items

        result = []
        for node in nodes:
            node_info = {
                "name": node.metadata.name,
                "conditions": [
                    {
                        "type": c.type,
                        "status": c.status,
                        "reason": c.reason,
                        "message": c.message,
                    }
                    for c in (node.status.conditions or [])
                ],
                "addresses": [
                    {"type": addr.type, "address": addr.address}
                    for addr in (node.status.addresses or [])
                ],
                "allocatable": dict(node.status.allocatable) if node.status.allocatable else {},
                "capacity": dict(node.status.capacity) if node.status.capacity else {},
            }
            result.append(node_info)

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error getting node status: {e}")
        return f"Error getting node status: {e}"


async def handle_list_namespaces(params: ListNamespacesParams) -> str:
    """List Kubernetes namespaces."""
    logger.info(f"Listing namespaces (limit: {params.limit})")

    api = get_k8s_api()
    if not api:
        return "Error: Kubernetes client not initialized."

    try:
        namespace_list = await asyncio.to_thread(api.list_namespace)
        namespaces = [_format_namespace_entry(namespace) for namespace in namespace_list.items[: params.limit]]
        return json.dumps({"count": len(namespaces), "namespaces": namespaces}, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error listing namespaces: {e}")
        return f"Error listing namespaces: {e}"


async def handle_list_pods(params: ListPodsParams) -> str:
    """List pods in the cluster."""
    logger.info(
        f"Listing pods (namespace: {params.namespace!r}, label_selector: {params.label_selector!r}, limit: {params.limit})"
    )

    api = get_k8s_api()
    if not api:
        return "Error: Kubernetes client not initialized."

    try:
        if params.namespace:
            pod_list = await asyncio.to_thread(
                api.list_namespaced_pod,
                params.namespace,
                label_selector=params.label_selector,
            )
        else:
            pod_list = await asyncio.to_thread(
                api.list_pod_for_all_namespaces,
                label_selector=params.label_selector,
            )

        pods = [_format_pod_entry(pod) for pod in pod_list.items[: params.limit]]
        return json.dumps(
            {"count": len(pods), "namespace": params.namespace, "pods": pods},
            indent=2,
            default=str,
        )
    except Exception as e:
        logger.error(f"Error listing pods: {e}")
        return f"Error listing pods: {e}"


async def handle_list_services(params: ListServicesParams) -> str:
    """List services in the cluster."""
    logger.info(
        f"Listing services (namespace: {params.namespace!r}, label_selector: {params.label_selector!r}, limit: {params.limit})"
    )

    api = get_k8s_api()
    if not api:
        return "Error: Kubernetes client not initialized."

    try:
        if params.namespace:
            service_list = await asyncio.to_thread(
                api.list_namespaced_service,
                params.namespace,
                label_selector=params.label_selector,
            )
        else:
            service_list = await asyncio.to_thread(
                api.list_service_for_all_namespaces,
                label_selector=params.label_selector,
            )

        services = [_format_service_entry(service) for service in service_list.items[: params.limit]]
        return json.dumps(
            {"count": len(services), "namespace": params.namespace, "services": services},
            indent=2,
            default=str,
        )
    except Exception as e:
        logger.error(f"Error listing services: {e}")
        return f"Error listing services: {e}"


async def handle_list_deployments(params: ListDeploymentsParams) -> str:
    """List deployments in the cluster."""
    logger.info(f"Listing deployments (namespace: {params.namespace!r}, limit: {params.limit})")

    apps_v1 = get_apps_v1_api()
    if not apps_v1:
        return "Error: Kubernetes client not initialized."

    try:
        if params.namespace:
            deployment_list = await asyncio.to_thread(apps_v1.list_namespaced_deployment, params.namespace)
        else:
            deployment_list = await asyncio.to_thread(apps_v1.list_deployment_for_all_namespaces)

        deployments = [_format_deployment_entry(deployment) for deployment in deployment_list.items[: params.limit]]
        return json.dumps(
            {"count": len(deployments), "namespace": params.namespace, "deployments": deployments},
            indent=2,
            default=str,
        )
    except Exception as e:
        logger.error(f"Error listing deployments: {e}")
        return f"Error listing deployments: {e}"


async def handle_list_events(params: ListEventsParams) -> str:
    """List recent Kubernetes events."""
    logger.info(
        f"Listing events (namespace: {params.namespace!r}, involved_object_name: {params.involved_object_name!r}, limit: {params.limit})"
    )

    api = get_k8s_api()
    if not api:
        return "Error: Kubernetes client not initialized."

    try:
        if params.namespace:
            event_list = await asyncio.to_thread(api.list_namespaced_event, params.namespace)
        else:
            event_list = await asyncio.to_thread(api.list_event_for_all_namespaces)

        events = [_format_event_entry(event) for event in event_list.items]
        if params.involved_object_name:
            events = [
                event
                for event in events
                if event["involved_object"]["name"] == params.involved_object_name
            ]

        events = events[: params.limit]
        return json.dumps(
            {"count": len(events), "namespace": params.namespace, "events": events},
            indent=2,
            default=str,
        )
    except Exception as e:
        logger.error(f"Error listing events: {e}")
        return f"Error listing events: {e}"


async def handle_get_service_endpoints(params: GetServiceEndpointsParams) -> str:
    """Get endpoint information for a service."""
    logger.info(f"Getting endpoints for service: {params.service_name} in namespace {params.namespace}")

    api = get_k8s_api()
    if not api:
        return "Error: Kubernetes client not initialized."

    try:
        endpoints = await asyncio.to_thread(api.read_namespaced_endpoints, params.service_name, params.namespace)
        return json.dumps(_format_endpoints_entry(endpoints), indent=2, default=str)
    except Exception as e:
        logger.error(f"Error getting service endpoints: {e}")
        return f"Error getting service endpoints: {e}"


async def handle_get_pod_logs(params: GetPodLogsParams) -> str:
    """Read recent logs from a pod."""
    logger.info(
        f"Reading pod logs: {params.pod_name} in namespace {params.namespace} (container: {params.container!r}, tail_lines: {params.tail_lines})"
    )

    api = get_k8s_api()
    if not api:
        return "Error: Kubernetes client not initialized."

    try:
        log_text = await asyncio.to_thread(
            api.read_namespaced_pod_log,
            params.pod_name,
            params.namespace,
            container=params.container,
            tail_lines=params.tail_lines,
            timestamps=params.timestamps,
        )
        return json.dumps(
            {
                "pod_name": params.pod_name,
                "namespace": params.namespace,
                "container": params.container,
                "tail_lines": params.tail_lines,
                "timestamps": params.timestamps,
                "content": log_text,
            },
            indent=2,
        )
    except Exception as e:
        logger.error(f"Error reading pod logs: {e}")
        return f"Error reading pod logs: {e}"


# fastmcp Tools

@mcp.tool()
async def check_k8s_health() -> str:
    """
    Check the health of the Kubernetes connection.
    Returns status and connectivity details.
    """
    api = get_k8s_api()
    if api:
        try:
            # Quick check
            await asyncio.to_thread(api.list_node, limit=1)
            
            return json.dumps({
                "status": "healthy",
                "message": "Connected to Kubernetes Cluster",
                "mode": "in-cluster" if not os.getenv("KUBECONFIG") else "kubeconfig"
            }, indent=2)
        except Exception as e:
             return json.dumps({
                "status": "unhealthy",
                "error": str(e),
                "message": "Client initialized but API unreachable"
            }, indent=2)
    else:
        return json.dumps({
            "status": "unhealthy",
            "message": "Failed to initialize Kubernetes client. Retrying automatically..."
        }, indent=2)


@mcp.tool()
async def get_pod_status(pod_name: str, namespace: str = "default") -> str:
    """Get the status of a Kubernetes pod. Returns pod phase, conditions, and container statuses."""
    return await handle_get_pod_status(GetPodStatusParams(pod_name=pod_name, namespace=namespace))


@mcp.tool()
async def list_namespaces(limit: int = 100) -> str:
    """List namespaces in the cluster."""
    return await handle_list_namespaces(ListNamespacesParams(limit=limit))


@mcp.tool()
async def list_pods(namespace: str = None, label_selector: str = None, limit: int = 200) -> str:
    """List pods in the cluster."""
    return await handle_list_pods(ListPodsParams(namespace=namespace, label_selector=label_selector, limit=limit))


@mcp.tool()
async def list_services(namespace: str = None, label_selector: str = None, limit: int = 200) -> str:
    """List services in the cluster."""
    return await handle_list_services(ListServicesParams(namespace=namespace, label_selector=label_selector, limit=limit))


@mcp.tool()
async def list_deployments(namespace: str = None, limit: int = 200) -> str:
    """List deployments in the cluster."""
    return await handle_list_deployments(ListDeploymentsParams(namespace=namespace, limit=limit))


@mcp.tool()
async def list_events(namespace: str = None, involved_object_name: str = None, limit: int = 200) -> str:
    """List Kubernetes events in the cluster."""
    return await handle_list_events(
        ListEventsParams(namespace=namespace, involved_object_name=involved_object_name, limit=limit)
    )


@mcp.tool()
async def get_service_endpoints(service_name: str, namespace: str = "default") -> str:
    """Read the endpoints behind a Kubernetes service."""
    return await handle_get_service_endpoints(
        GetServiceEndpointsParams(service_name=service_name, namespace=namespace)
    )


@mcp.tool()
async def get_pod_logs(
    pod_name: str,
    namespace: str = "default",
    container: str = None,
    tail_lines: int = 200,
    timestamps: bool = False,
) -> str:
    """Read recent logs from a Kubernetes pod."""
    return await handle_get_pod_logs(
        GetPodLogsParams(
            pod_name=pod_name,
            namespace=namespace,
            container=container,
            tail_lines=tail_lines,
            timestamps=timestamps,
        )
    )


@mcp.tool()
async def get_deployment_status(deployment_name: str, namespace: str = "default") -> str:
    """Get the status of a Kubernetes deployment."""
    return await handle_get_deployment_status(
        GetDeploymentStatusParams(deployment_name=deployment_name, namespace=namespace)
    )


@mcp.tool()
async def get_node_status(node_name: str = None) -> str:
    """Get the status of Kubernetes nodes."""
    return await handle_get_node_status(GetNodeStatusParams(node_name=node_name))


if __name__ == "__main__":
    logger.info("Starting FastMCP server execution...")
    
    # Try initial connection to warm up
    get_k8s_api()
    mcp.run(transport="sse")
