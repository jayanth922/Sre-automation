"""The sole authorization boundary for live remediation mutations."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Dict, Mapping, Optional

from .execution_context import ExecutionContext, require_execution_context
from .executor import (
    EXECUTOR_TOOL_MAP,
    GITHUB_EXEC_TOOL_MAP,
    ExecutionMode,
    ExecutionResult,
    Executor,
    build_command,
    build_rollback_command,
)
from .policy_gate import AutonomyDecision, decide
from .redis_state_store import get_state_store
from .severity_engine import Severity


class MutationRejected(PermissionError):
    """A live mutation was rejected before any external write was attempted."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class MutationAuditError(RuntimeError):
    """The mutation result could not be durably recorded after execution."""


@dataclass(frozen=True)
class MutationGateContext:
    """Inputs needed to reproduce, rather than trust, a prior gate decision."""

    decision: str
    severity: Severity | str | int
    environment: str = "production"
    risk_score: float = 0.0
    approved: bool = False
    actor: str = "sre-agent"
    incident_id: Optional[str] = None


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _severity(value: Any) -> Severity:
    if isinstance(value, Severity):
        return value
    try:
        if isinstance(value, str):
            token = value.upper()
            if token == "UNKNOWN":
                return Severity.UNKNOWN
            if token.startswith("SEV"):
                return Severity[token]
        return Severity(int(value))
    except (KeyError, TypeError, ValueError) as exc:
        raise MutationRejected("invalid_gate_context", "A valid incident severity is required") from exc


def _decision_value(value: Any) -> str:
    if isinstance(value, AutonomyDecision):
        return value.value
    return str(value or "").lower()


def _idempotency_ttl() -> int:
    try:
        return max(1, int(os.getenv("MUTATION_IDEMPOTENCY_TTL_SECONDS", "86400")))
    except (TypeError, ValueError):
        return 86400


def _verify_scope(action: Any, context: ExecutionContext) -> None:
    """Require the proposed target to remain inside its tenant and namespace."""
    if not context.organization_id or not context.cluster_id:
        raise MutationRejected("scope_mismatch", "Tenant and cluster scope are required")

    params = getattr(action, "parameters", None) or {}
    if not isinstance(params, dict):
        raise MutationRejected("scope_mismatch", "Action parameters must be a mapping")

    for field, expected in (
        ("organization_id", context.organization_id),
        ("cluster_id", context.cluster_id),
    ):
        supplied = params.get(field)
        if supplied is not None and str(supplied) != str(expected):
            raise MutationRejected(
                "scope_mismatch",
                f"Action {field} does not match the execution context",
            )

    action_type = str(getattr(action, "action_type", "")).lower()
    target = str(getattr(action, "target", "")).strip()
    if not target:
        raise MutationRejected("scope_mismatch", "Action target is required")

    if action_type in EXECUTOR_TOOL_MAP:
        allowed = set(context.allowlist)
        if context.namespace:
            allowed.add(context.namespace)
        namespace = str(params.get("namespace") or "").strip()
        if not allowed:
            raise MutationRejected("scope_mismatch", "No mutation namespace is configured")
        if not namespace or namespace not in allowed:
            raise MutationRejected(
                "scope_mismatch",
                f"Namespace '{namespace or '<missing>'}' is outside the execution context",
            )
    elif action_type not in GITHUB_EXEC_TOOL_MAP:
        raise MutationRejected("unsupported_action", f"No live tool maps to '{action_type}'")


def _as_uuid(value: str, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise MutationAuditError(f"Audit {label} must be a UUID") from exc


async def _persist_audit_event(
    context: ExecutionContext,
    result: ExecutionResult,
    *,
    approved: bool,
) -> None:
    """Persist the Executor's tamper-evident audit payload to AuditEvent."""
    from backend import database, models

    details = dict(result.audit)
    details.update(
        execution_detail=result.detail,
        rollback_command=result.rollback_command,
        human_approved=approved,
    )
    event = models.AuditEvent(
        cluster_id=_as_uuid(context.cluster_id, "cluster_id"),
        organization_id=_as_uuid(context.organization_id, "organization_id"),
        actor_type="AGENT",
        actor_id=str(result.audit.get("actor") or "sre-agent"),
        action_type=result.action_type.upper(),
        resource_target=result.target,
        outcome=result.status,
        details=json.dumps(details, sort_keys=True, default=str),
    )
    async with database.AsyncSessionLocal() as db:
        db.add(event)
        await db.commit()


async def authorize_and_execute(
    action: Any,
    gate_decision: Any,
    context: Optional[ExecutionContext],
    tool_caller: Optional[Callable[[str, Dict[str, Any]], Any]],
    github_caller: Optional[Callable[[str, Dict[str, Any]], Any]],
    idempotency_key: str,
) -> ExecutionResult:
    """Freshly authorize, atomically claim, execute, and audit one mutation."""
    execution_context = require_execution_context(context)
    initial_decision = _decision_value(_field(gate_decision, "decision"))
    if initial_decision not in {
        AutonomyDecision.AUTONOMOUS.value,
        AutonomyDecision.REQUIRES_APPROVAL.value,
    }:
        raise MutationRejected(
            "stale_gate_blocked", "The prior gate did not authorize this action"
        )

    store = get_state_store()
    if hasattr(store, "is_available") and not store.is_available():
        raise MutationRejected(
            "state_unavailable", "Redis is unavailable for a fresh lock check"
        )
    if store.is_cluster_locked(str(execution_context.cluster_id)):
        raise MutationRejected("cluster_locked", "The cluster emergency lock is active")

    severity = _severity(_field(gate_decision, "severity"))
    # The prior decision and alert state are untrusted/stale here. Environment
    # comes only from the operator-built execution context and defaults closed.
    environment = str(getattr(execution_context, "environment", "production"))
    try:
        risk_score = float(_field(gate_decision, "risk_score", 0.0) or 0.0)
    except (TypeError, ValueError) as exc:
        raise MutationRejected("invalid_gate_context", "Risk score must be numeric") from exc
    approved = bool(_field(gate_decision, "approved", False))

    fresh = decide(
        action,
        SimpleNamespace(severity=severity),
        environment=environment,
        risk_score=risk_score,
    )
    if fresh.decision is AutonomyDecision.BLOCKED:
        raise MutationRejected("policy_blocked", fresh.reason)
    if fresh.decision is AutonomyDecision.REQUIRES_APPROVAL and not approved:
        raise MutationRejected("approval_required", fresh.reason)

    _verify_scope(action, execution_context)
    if not idempotency_key or not str(idempotency_key).strip():
        raise MutationRejected(
            "invalid_idempotency_key", "A non-empty idempotency key is required"
        )
    if not store.set_idempotency(str(idempotency_key), _idempotency_ttl()):
        if hasattr(store, "is_available") and not store.is_available():
            raise MutationRejected("state_unavailable", "Redis failed during idempotency claim")
        return ExecutionResult(
            action_type=str(getattr(action, "action_type", "")).lower(),
            target=str(getattr(action, "target", "")),
            command=build_command(action),
            mode=ExecutionMode.LIVE,
            status="SKIPPED",
            rollback_command=build_rollback_command(action),
            detail="Duplicate mutation short-circuited by its idempotency claim.",
        )

    executor = Executor(
        actor=str(_field(gate_decision, "actor", "sre-agent") or "sre-agent"),
        incident_id=_field(gate_decision, "incident_id"),
    )
    result = await executor._aexecute_unchecked(
        action,
        "approved" if approved else fresh.decision.value,
        dry_run=False,
        tool_caller=tool_caller,
        github_caller=github_caller,
    )
    try:
        await _persist_audit_event(execution_context, result, approved=approved)
    except Exception as exc:
        raise MutationAuditError(
            "Live execution completed but its AuditEvent could not be persisted; "
            "the idempotency claim remains active"
        ) from exc
    return result
