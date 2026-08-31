"""Tenant-bound execution configuration for agent and MCP construction."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple, cast

_MCP_ENDPOINT_ENV = {
    "k8s": "MCP_K8S_URI",
    "executor": "MCP_EXECUTOR_URI",
    "metrics": "MCP_METRICS_URI",
    "logs": "MCP_LOGS_URI",
    "runbooks": "MCP_RUNBOOKS_URI",
    "github": "MCP_GITHUB_URI",
    "github_exec": "MCP_GITHUB_EXEC_URI",
    "sandbox": "MCP_SANDBOX_URI",
}


def operator_mcp_endpoints() -> dict[str, str]:
    """Return only deployment-operator-configured MCP transport endpoints."""
    return {
        name: value.strip()
        for name, env_name in _MCP_ENDPOINT_ENV.items()
        if (value := os.getenv(env_name, "").strip())
    }


def require_operator_mcp_endpoint(name: str, endpoint: str) -> str:
    """Reject context/argument endpoints that differ from trusted deployment config."""
    expected = operator_mcp_endpoints().get(name)
    if not expected:
        raise RuntimeError(f"Operator MCP endpoint for {name} is not configured")
    if endpoint != expected:
        raise RuntimeError(
            f"MCP endpoint for {name} is not operator-controlled; refusing credentials"
        )
    return expected


def _normalized_environment(value: Any) -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "prod": "production",
        "production": "production",
        "stage": "staging",
        "staging": "staging",
        "dev": "development",
        "development": "development",
        "test": "testing",
        "testing": "testing",
    }
    return aliases.get(raw, "production")


def operator_cluster_environment() -> str:
    """Trusted policy environment; unknown or absent values fail to production."""
    return _normalized_environment(os.getenv("SENTINEL_CLUSTER_ENVIRONMENT"))


def _value(source: Any, name: str) -> Optional[str]:
    value = getattr(source, name, None) if source is not None else None
    if value is None or not str(value).strip():
        return None
    return str(value).strip()


def is_production_runtime() -> bool:
    environment = (
        os.getenv("SENTINEL_ENV")
        or os.getenv("APP_ENV")
        or os.getenv("ENVIRONMENT")
        or ""
    ).lower()
    return os.getenv("AGENT_MODE", "").lower() == "api" or environment in {
        "prod",
        "production",
    }


@dataclass(frozen=True)
class ExecutionContext:
    organization_id: str
    cluster_id: str
    mcp_endpoints: Mapping[str, str] = field(default_factory=dict)
    credentials: Mapping[str, str] = field(default_factory=dict, repr=False)
    namespace: Optional[str] = None
    allowlist: Tuple[str, ...] = ()
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    llm_base_url: Optional[str] = None
    environment: str = "production"
    key_version: int = 1
    context_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "mcp_endpoints", MappingProxyType(dict(self.mcp_endpoints))
        )
        object.__setattr__(
            self, "credentials", MappingProxyType(dict(self.credentials))
        )
        object.__setattr__(self, "allowlist", tuple(sorted(set(self.allowlist))))
        object.__setattr__(
            self, "environment", _normalized_environment(self.environment)
        )

    @classmethod
    def from_cluster(cls, cluster: Any) -> "ExecutionContext":
        if cluster is None or getattr(cluster, "id", None) is None:
            raise ValueError("A persisted cluster is required for execution context")

        # Cluster connectivity fields are tenant-controlled destinations. MCP
        # clients carry an operator secret, so they may only use deployment-
        # controlled service routes and pass cluster identity separately.
        endpoints = operator_mcp_endpoints()
        from .cluster_context import resolve_authorized_llm

        llm = resolve_authorized_llm(cluster)
        credentials = {
            name: value
            for name, value in {
                "k8s_token": _value(cluster, "k8s_token"),
                "k8s_api_server": _value(cluster, "k8s_api_server"),
                "github_token": _value(cluster, "github_token"),
                "github_repo": _value(cluster, "github_repo"),
                "github_app_installation_id": _value(
                    cluster, "github_app_installation_id"
                ),
                "notion_api_key": _value(cluster, "notion_api_key"),
                "notion_database_id": _value(cluster, "notion_database_id"),
                "llm_api_key": llm.get("api_key") or _value(cluster, "llm_api_key"),
            }.items()
            if value
        }
        namespace = _value(cluster, "namespace")
        from .namespace_scope import NamespaceScopeError, namespace_required

        if not namespace and namespace_required():
            raise NamespaceScopeError(
                f"Cluster {cluster.id} has no configured namespace; refusing scoped execution"
            )
        return cls(
            organization_id=str(cluster.org_id),
            cluster_id=str(cluster.id),
            mcp_endpoints=endpoints,
            credentials=credentials,
            namespace=namespace,
            allowlist=(namespace,) if namespace else (),
            llm_provider=llm["provider"],
            llm_model=llm["model"],
            llm_base_url=llm["base_url"],
            environment=operator_cluster_environment(),
            key_version=int(getattr(cluster, "key_version", 1) or 1),
            context_version=int(getattr(cluster, "execution_context_version", 1) or 1),
        )

    @classmethod
    def from_environment(cls) -> "ExecutionContext":
        """Build a local-development context from process environment."""
        from .cluster_context import resolve_authorized_llm

        endpoints = operator_mcp_endpoints()
        namespace = os.getenv("EXECUTOR_ALLOWED_NAMESPACES", "").strip()
        allowlist = tuple(item.strip() for item in namespace.split(",") if item.strip())
        llm = resolve_authorized_llm(None)
        return cls(
            organization_id="local",
            cluster_id=os.getenv("CLUSTER_ID", "local"),
            mcp_endpoints=endpoints,
            credentials={
                name: value
                for name, value in {
                    "llm_api_key": llm.get("api_key") or os.getenv("LLM_API_KEY"),
                    "github_token": os.getenv("GITHUB_TOKEN"),
                    "notion_api_key": os.getenv("NOTION_API_KEY"),
                    "notion_database_id": os.getenv("NOTION_DATABASE_ID"),
                }.items()
                if value
            },
            namespace=allowlist[0] if len(allowlist) == 1 else None,
            allowlist=allowlist,
            llm_provider=llm["provider"],
            llm_model=llm["model"],
            llm_base_url=llm["base_url"],
            environment=operator_cluster_environment(),
            key_version=int(os.getenv("CREDENTIAL_ENCRYPTION_KEY_VERSION", "1")),
        )

    def endpoint(self, name: str) -> str:
        endpoint = self.mcp_endpoints.get(name)
        if not endpoint:
            raise RuntimeError(
                f"Execution context for cluster {self.cluster_id} has no {name} endpoint"
            )
        return endpoint

    def transport_headers(self, service_token: Optional[str] = None) -> dict[str, str]:
        token = service_token or os.getenv("MCP_SERVICE_TOKEN", "").strip()
        if not token:
            raise RuntimeError("MCP_SERVICE_TOKEN is required")
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Sentinel-Organization-ID": self.organization_id,
            "X-Sentinel-Cluster-ID": self.cluster_id,
        }
        if self.namespace:
            headers["X-Sentinel-Namespace"] = self.namespace
        return headers

    def fingerprint(self) -> str:
        """Cache key containing no credential values."""
        payload = {
            "cluster_id": self.cluster_id,
            "mcp_endpoints": sorted(self.mcp_endpoints.items()),
            "namespace": self.namespace,
            "allowlist": self.allowlist,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "llm_base_url": self.llm_base_url,
            "environment": self.environment,
            "key_version": self.key_version,
            "context_version": self.context_version,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def llm_kwargs(self) -> dict[str, str]:
        values = {
            "model_id": self.llm_model,
            "base_url": self.llm_base_url,
            "api_key": self.credentials.get("llm_api_key"),
        }
        return {name: value for name, value in values.items() if value}

    def llm_manifest(self) -> dict[str, Optional[str]]:
        """Exact authorized LLM settings for traces/UI (no secrets)."""
        from .cluster_context import llm_manifest

        return cast(
            dict[str, Optional[str]],
            llm_manifest(
                {
                    "provider": self.llm_provider,
                    "model": self.llm_model,
                    "base_url": self.llm_base_url,
                }
            ),
        )


def require_execution_context(
    context: Optional[ExecutionContext],
) -> ExecutionContext:
    if context is not None:
        return context
    if is_production_runtime():
        raise RuntimeError(
            "Production execution requires a tenant-bound ExecutionContext"
        )
    return ExecutionContext.from_environment()
