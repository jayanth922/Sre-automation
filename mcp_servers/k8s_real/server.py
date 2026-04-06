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

def initialize_kubernetes_client_logic():
    """Internal logic to initialize the client."""
    global k8s_client, v1
    
    # Check for API Server Host override (e.g. for Kind on Docker Host)
    api_server_host = os.getenv("KUBERNETES_API_SERVER_HOST")
    kubeconfig_path = os.getenv("KUBECONFIG")

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
        logger.info(f"✅ Loaded PATCHED Kubernetes config from {patched_config_path}")

    # Try in-cluster config first (if running in a pod and no override active)
    elif not os.getenv("KUBERNETES_API_SERVER_HOST"):
        try:
            config.load_incluster_config()
            logger.info("✅ Loaded in-cluster Kubernetes config")
        except config.ConfigException:
            # Fall back to local kubeconfig
            if kubeconfig_path:
                if os.path.exists(kubeconfig_path):
                    config.load_kube_config(config_file=kubeconfig_path)
                    logger.info(f"✅ Loaded Kubernetes config from {kubeconfig_path}")
                else:
                    logger.warning(f"⚠️ Kubeconfig not found at {kubeconfig_path}")
                    raise Exception(f"Kubeconfig missing at {kubeconfig_path}")
            else:
                config.load_kube_config()
                logger.info("✅ Loaded Kubernetes config from default location")
    else:
            # Fallback if patch logic didn't trigger but env var was set (e.g. invalid path)
            if kubeconfig_path and os.path.exists(kubeconfig_path):
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


class DeletePodParams(BaseModel):
    """Parameters for delete_pod tool."""
    pod_name: str = Field(..., description="Name of the pod")
    namespace: str = Field(default="default", description="Kubernetes namespace")
    grace_period_seconds: int = Field(
        default=30, description="Grace period for pod termination"
    )


class GetDeploymentStatusParams(BaseModel):
    """Parameters for get_deployment_status tool."""
    deployment_name: str = Field(..., description="Name of the deployment")
    namespace: str = Field(default="default", description="Kubernetes namespace")


class ScaleDeploymentParams(BaseModel):
    """Parameters for scale_deployment tool."""
    deployment_name: str = Field(..., description="Name of the deployment")
    namespace: str = Field(default="default", description="Kubernetes namespace")
    replicas: int = Field(..., ge=0, description="Number of replicas")


class GetNodeStatusParams(BaseModel):
    """Parameters for get_node_status tool."""
    node_name: Optional[str] = Field(None, description="Specific node name (optional)")


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


async def handle_delete_pod(params: DeletePodParams) -> str:
    """Delete pod using Kubernetes API."""
    logger.info(
        f"Deleting pod: {params.pod_name} in namespace {params.namespace} "
        f"(grace_period: {params.grace_period_seconds}s)"
    )

    api = get_k8s_api()
    if not api:
        return "Error: Kubernetes client not initialized."

    # Run in thread pool to avoid blocking
    loop = asyncio.get_event_loop()
    try:
        body = client.V1DeleteOptions(grace_period_seconds=params.grace_period_seconds)
        await loop.run_in_executor(
            None,
            api.delete_namespaced_pod,
            params.pod_name,
            params.namespace,
            body,
        )

        result = {
            "status": "deleted",
            "pod_name": params.pod_name,
            "namespace": params.namespace,
            "grace_period_seconds": params.grace_period_seconds,
            "message": f"Pod {params.pod_name} deletion initiated",
        }
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error deleting pod: {e}")
        return f"Error deleting pod: {e}"


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


async def handle_scale_deployment(params: ScaleDeploymentParams) -> str:
    """Scale deployment using Kubernetes API."""
    logger.info(
        f"Scaling deployment: {params.deployment_name} in namespace {params.namespace} "
        f"to {params.replicas} replicas"
    )

    apps_v1 = get_apps_v1_api()
    if not apps_v1:
        return "Error: Kubernetes client not initialized."

    loop = asyncio.get_event_loop()
    try:
        # Get current deployment
        deployment = await loop.run_in_executor(
            None,
            apps_v1.read_namespaced_deployment,
            params.deployment_name,
            params.namespace,
        )

        # Update replicas
        deployment.spec.replicas = params.replicas

        # Apply update
        await loop.run_in_executor(
            None,
            apps_v1.replace_namespaced_deployment,
            params.deployment_name,
            params.namespace,
            deployment,
        )

        result = {
            "status": "scaled",
            "deployment_name": params.deployment_name,
            "namespace": params.namespace,
            "replicas": params.replicas,
            "message": f"Deployment {params.deployment_name} scaled to {params.replicas} replicas",
        }
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error scaling deployment: {e}")
        return f"Error scaling deployment: {e}"


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
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, api.list_node, limit=1)
            
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
async def delete_pod(pod_name: str, namespace: str = "default", grace_period_seconds: int = 30) -> str:
    """Delete a Kubernetes pod."""
    return await handle_delete_pod(
        DeletePodParams(pod_name=pod_name, namespace=namespace, grace_period_seconds=grace_period_seconds)
    )


@mcp.tool()
async def get_deployment_status(deployment_name: str, namespace: str = "default") -> str:
    """Get the status of a Kubernetes deployment."""
    return await handle_get_deployment_status(
        GetDeploymentStatusParams(deployment_name=deployment_name, namespace=namespace)
    )


@mcp.tool()
async def scale_deployment(deployment_name: str, replicas: int, namespace: str = "default") -> str:
    """Scale a Kubernetes deployment to a specific number of replicas."""
    return await handle_scale_deployment(
        ScaleDeploymentParams(deployment_name=deployment_name, replicas=replicas, namespace=namespace)
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
