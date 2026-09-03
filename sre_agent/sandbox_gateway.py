"""The sole authorization boundary for provisioning code-fix verification sandboxes.

Mirrors mutation_gateway.py's shape: every K8s Job the sandbox workflow needs
passes through here first, re-deriving tenant/namespace scope, an idempotency
claim, and an audit record from scratch rather than trusting the caller. The
Temporal worker process calls this directly (it is not itself the mutation
path — it never touches a live customer deployment, only an ephemeral sandbox
namespace carved out for verification).
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from .execution_context import ExecutionContext, require_execution_context
from .executor import _structured_payload
from .redis_state_store import get_state_store

SANDBOX_NAMESPACE_ENV = "SANDBOX_NAMESPACE"
DEFAULT_SANDBOX_NAMESPACE = "sentinel-sandbox"


class SandboxRejected(PermissionError):
    """A sandbox provisioning request was rejected before any K8s call fired."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class SandboxAuditError(RuntimeError):
    """The sandbox action could not be durably recorded after execution."""


@dataclass(frozen=True)
class SandboxGateContext:
    """Inputs identifying the incident and tenant this sandbox run belongs to."""

    incident_id: str
    organization_id: str
    cluster_id: str
    actor: str = "sre-agent"


def sandbox_namespace() -> str:
    """The single, operator-controlled namespace all sandbox Jobs run in.

    Deliberately never the client's own workload namespace — a code-fix
    candidate is untrusted and must never run alongside real traffic.
    """
    return os.getenv(SANDBOX_NAMESPACE_ENV, DEFAULT_SANDBOX_NAMESPACE).strip() or (
        DEFAULT_SANDBOX_NAMESPACE
    )


def _idempotency_ttl() -> int:
    try:
        return max(1, int(os.getenv("SANDBOX_IDEMPOTENCY_TTL_SECONDS", "3600")))
    except (TypeError, ValueError):
        return 3600


def _as_uuid(value: str, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise SandboxAuditError(f"Audit {label} must be a UUID") from exc


async def _persist_audit_event(
    gate_context: SandboxGateContext,
    tool_name: str,
    job_name: str,
    status: str,
    detail: str,
) -> None:
    """Persist the sandbox action to AuditEvent, mirroring mutation_gateway."""
    from backend import database, models

    event = models.AuditEvent(
        cluster_id=_as_uuid(gate_context.cluster_id, "cluster_id"),
        organization_id=_as_uuid(gate_context.organization_id, "organization_id"),
        actor_type="AGENT",
        actor_id=gate_context.actor,
        action_type=f"SANDBOX_{tool_name.upper()}",
        resource_target=job_name,
        outcome=status,
        details=json.dumps(
            {
                "incident_id": gate_context.incident_id,
                "namespace": sandbox_namespace(),
                "detail": detail,
            },
            sort_keys=True,
            default=str,
        ),
    )
    async with database.AsyncSessionLocal() as db:
        db.add(event)
        await db.commit()


async def authorize_and_provision_sandbox(
    gate_context: SandboxGateContext,
    execution_context: Optional[ExecutionContext],
    tool_caller: Callable[[str, Dict[str, Any]], Any],
    tool_name: str,
    arguments: Dict[str, Any],
    idempotency_key: str,
) -> Dict[str, Any]:
    """Freshly authorize, atomically claim, execute, and audit one sandbox call.

    Every sandbox tool call (provision/status/logs/teardown) goes through this
    single choke point — never directly through a raw MCP tool_caller — so a
    bug anywhere upstream in the workflow can never provision or leave running
    an untracked Job outside the dedicated sandbox namespace.
    """
    ctx = require_execution_context(execution_context)
    if str(ctx.organization_id) != str(gate_context.organization_id) or str(
        ctx.cluster_id
    ) != str(gate_context.cluster_id):
        raise SandboxRejected(
            "scope_mismatch", "Execution context does not match the sandbox gate context"
        )

    namespace = sandbox_namespace()
    args = dict(arguments)
    args["namespace"] = namespace

    store = get_state_store()
    if hasattr(store, "is_available") and not store.is_available():
        raise SandboxRejected(
            "state_unavailable", "Redis is unavailable for a fresh idempotency check"
        )
    if not idempotency_key or not str(idempotency_key).strip():
        raise SandboxRejected(
            "invalid_idempotency_key", "A non-empty idempotency key is required"
        )
    if not store.set_idempotency(str(idempotency_key), _idempotency_ttl()):
        if hasattr(store, "is_available") and not store.is_available():
            raise SandboxRejected(
                "state_unavailable", "Redis failed during idempotency claim"
            )
        return {"status": "SKIPPED", "detail": "Duplicate sandbox call short-circuited."}

    try:
        response = await tool_caller(tool_name, args)
    except Exception as exc:
        try:
            await _persist_audit_event(
                gate_context, tool_name, args.get("job_name", ""), "ERROR", str(exc)
            )
        except Exception:
            pass
        raise

    payload = _structured_payload(response)
    if payload is None:
        payload = {"raw": str(response)}
    status = str((payload or {}).get("status", "UNKNOWN")).upper()

    try:
        await _persist_audit_event(
            gate_context,
            tool_name,
            (payload or {}).get("job_name", args.get("job_name", "")),
            status,
            (payload or {}).get("detail", ""),
        )
    except Exception as exc:
        raise SandboxAuditError(
            "Sandbox call completed but its AuditEvent could not be persisted; "
            "the idempotency claim remains active"
        ) from exc

    return payload if isinstance(payload, dict) else {"status": status, "raw": payload}
