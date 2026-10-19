"""The sole authorization boundary for live remediation mutations."""

from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Mapping, Optional

from .confidence_calibration import (
    ConfidenceCalibrationError,
    calibrate_confidence,
    load_calibration_artifact,
)
from .execution_context import ExecutionContext, require_execution_context
from .executor import (
    EXECUTOR_TOOL_MAP,
    GITHUB_EXEC_TOOL_MAP,
    ExecutionMode,
    ExecutionResult,
    Executor,
    _structured_payload,
    build_command,
    build_rollback_command,
    live_resource_name,
    live_tool_for_action,
    missing_capability_reason,
)
from .policy_gate import AutonomyDecision, decide
from .redis_state_store import get_state_store
from .rollback_binding import bound_rollback
from .severity_engine import Severity

# Rejections that leave nothing written but the plan's premise broken: the
# deployment is not in the state a rollback was approved against, or cannot be
# shown to be. Later actions were planned on that state too, so the caller
# stops the plan for a human rather than carrying on.
ESCALATING_REJECTIONS = frozenset({"state_drift", "rollout_state_unavailable"})


class MutationRejected(PermissionError):
    """A live mutation was rejected before any external write was attempted."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class MutationPreDispatchError(RuntimeError):
    """A transient boundary failure happened before any claim or write attempt.

    Temporal may retry this failure because the gateway can prove that neither
    the idempotency claim nor the external mutation was attempted.
    """


class MutationRetryUnsafe(RuntimeError):
    """A boundary failure happened after retry safety could no longer be proved."""


class MutationAuditError(MutationRetryUnsafe):
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
    raw_action_confidence: Optional[float] = None


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
        raise MutationRejected(
            "invalid_gate_context", "A valid incident severity is required"
        ) from exc


def _decision_value(value: Any) -> str:
    if isinstance(value, AutonomyDecision):
        return value.value
    return str(value or "").lower()


def _idempotency_ttl() -> int:
    try:
        return max(1, int(os.getenv("MUTATION_IDEMPOTENCY_TTL_SECONDS", "86400")))
    except (TypeError, ValueError):
        return 86400


def _runtime_remediation_calibration(
    raw_confidence: Any,
) -> tuple[Optional[float], Optional[float]]:
    """Recompute confidence at the mutation boundary from operator config."""
    if (
        isinstance(raw_confidence, bool)
        or not isinstance(raw_confidence, (int, float))
        or not 0 <= float(raw_confidence) <= 1
    ):
        return None, None
    configured = os.getenv(
        "REMEDIATION_CONFIDENCE_CALIBRATION_PATH", ""
    ).strip()
    config_fingerprint = os.getenv(
        "SENTINEL_CONFIG_FINGERPRINT", ""
    ).strip()
    if not configured or not config_fingerprint:
        return None, None
    try:
        calibrated = calibrate_confidence(
            float(raw_confidence),
            load_calibration_artifact(Path(configured)),
            task="remediation",
            config_fingerprint=config_fingerprint,
        )
    except (ConfidenceCalibrationError, OSError):
        return None, None
    return calibrated.calibrated_probability, calibrated.autonomy_threshold


def _verify_scope(action: Any, context: ExecutionContext) -> None:
    """Require the proposed target to remain inside its tenant and namespace."""
    from .namespace_scope import NamespaceScopeError, assert_action_namespace

    if not context.organization_id or not context.cluster_id:
        raise MutationRejected(
            "scope_mismatch", "Tenant and cluster scope are required"
        )

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
        # Capability before scope: a namespace complaint about a step nothing
        # can execute anyway sends the operator chasing the wrong problem.
        if live_tool_for_action(action) is None:
            raise MutationRejected(
                "unsupported_action",
                missing_capability_reason(action_type, getattr(action, "parameters", None)),
            )
        try:
            assert_action_namespace(action, context)
        except NamespaceScopeError as exc:
            raise MutationRejected("scope_mismatch", str(exc)) from exc
        # Re-read after possible injection.
        params = getattr(action, "parameters", None) or {}
        namespace = str((params or {}).get("namespace") or "").strip()
        allowed = set(context.allowlist)
        if context.namespace:
            allowed.add(context.namespace)
        if not allowed:
            raise MutationRejected(
                "scope_mismatch", "No mutation namespace is configured"
            )
        if not namespace or namespace not in allowed:
            raise MutationRejected(
                "scope_mismatch",
                f"Namespace '{namespace or '<missing>'}' is outside the execution context",
            )
    elif action_type not in GITHUB_EXEC_TOOL_MAP:
        raise MutationRejected(
            "unsupported_action", f"No live tool maps to '{action_type}'"
        )


def _as_uuid(value: str, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise MutationAuditError(f"Audit {label} must be a UUID") from exc


@asynccontextmanager
async def _incident_mutation_guard(
    incident_id: Optional[str], context: ExecutionContext
):
    """Order a live write atomically against an Alertmanager clear.

    The incident row remains locked through the external write. If recovery
    committed first, the mutation is refused; if execution acquired the lock
    first, the clear is serialized after that already-authorized write.
    """
    if not incident_id:
        yield
        return

    from sqlalchemy import select

    from backend import database, models

    try:
        incident_uuid = uuid.UUID(str(incident_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise MutationRejected(
            "invalid_gate_context", "A valid incident id is required"
        ) from exc

    async with database.AsyncSessionLocal() as db:
        result = await db.execute(
            select(models.Incident)
            .where(
                models.Incident.id == incident_uuid,
                models.Incident.cluster_id
                == uuid.UUID(str(context.cluster_id)),
            )
            .with_for_update()
        )
        incident = result.scalar_one_or_none()
        if incident is None:
            raise MutationRejected(
                "scope_mismatch", "Incident does not belong to this cluster"
            )
        if incident.status == models.IncidentStatus.RESOLVED:
            raise MutationRejected(
                "incident_resolved",
                "The source alert cleared before this write could start",
            )
        yield


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


async def _rollback_precondition(
    action: Any,
    tool_caller: Optional[Callable[[str, Dict[str, Any]], Any]],
    executor: Executor,
    decision: str,
) -> Optional[ExecutionResult]:
    """Re-read the deployment a bound rollback was approved against.

    Returns a no-write success when the deployment is already on the approved
    target — a second approval, or a retry after the first run succeeded.
    Returns None when it is still on the template the approval expected, so
    the rollback may proceed. Anything else raises: the approver saw a state
    that no longer exists. Runs before the idempotency claim, so a failed read
    is retry-safe and a refusal leaves no claim behind.
    """
    params = getattr(action, "parameters", None) or {}
    binding = bound_rollback(params)
    if binding is None:  # `_verify_scope` refuses this first; never undo blindly
        raise MutationRejected(
            "unsupported_action", missing_capability_reason("rollback", params)
        )
    name = live_resource_name(getattr(action, "target", ""))
    namespace = str(params.get("namespace") or "").strip()
    if binding["deployment"] != name or binding["namespace"] != namespace:
        raise MutationRejected(
            "scope_mismatch",
            f"The rollback was bound to deployment/{binding['deployment']} in "
            f"'{binding['namespace']}', not deployment/{name} in '{namespace}'",
        )
    to_hash = binding["to_template_hash"]
    expected = binding["expected_template_hash"]
    if tool_caller is None:
        raise MutationRejected(
            "rollout_state_unavailable",
            "No executor tool_caller is configured to re-read the deployment",
        )

    payload = _structured_payload(
        await tool_caller("get_rollout_state", {"name": name, "namespace": namespace})
    )
    if not payload or str(payload.get("status") or "").upper() != "OK":
        detail = json.dumps(payload, sort_keys=True, default=str) if payload else "no response"
        raise MutationRejected(
            "rollout_state_unavailable",
            f"deployment/{name}'s rollout history could not be re-read, so the "
            f"rollback's precondition is unverified; nothing was written: {detail[:300]}",
        )
    current = payload.get("current_template_hash")
    if current == to_hash:
        command = build_command(action)
        return ExecutionResult(
            action_type="rollback",
            target=str(getattr(action, "target", "")),
            command=command,
            mode=ExecutionMode.LIVE,
            status="EXECUTED",
            audit=executor._audit(action, command, decision, ExecutionMode.LIVE),
            rollback_command=build_rollback_command(action),
            detail=(
                f"no write: deployment/{name} is already on the approved target "
                f"template {to_hash}; nothing was changed"
            ),
        )
    if payload.get("paused"):
        raise MutationRejected(
            "state_drift",
            f"deployment/{name}'s rollout was paused after the rollback was "
            "approved; nothing was written",
        )
    if current != expected:
        seen = (
            f"template {current}"
            if current
            else "a template no ReplicaSet matches yet (a rollout is in progress)"
        )
        raise MutationRejected(
            "state_drift",
            f"deployment/{name} was approved for rollback from template {expected} "
            f"to {to_hash}, but it is now on {seen}; nothing was written",
        )
    hashes = {r.get("template_hash") for r in payload.get("revisions") or [] if isinstance(r, dict)}
    if to_hash not in hashes:
        raise MutationRejected(
            "state_drift",
            f"deployment/{name}'s revision history no longer holds the approved "
            f"target template {to_hash}; nothing was written",
        )
    return None


async def _authorize_and_execute_unclassified(
    action: Any,
    gate_decision: Any,
    context: Optional[ExecutionContext],
    tool_caller: Optional[Callable[[str, Dict[str, Any]], Any]],
    github_caller: Optional[Callable[[str, Dict[str, Any]], Any]],
    idempotency_key: str,
    failure_phase: Dict[str, str],
) -> ExecutionResult:
    """Gateway core that records which side-effect boundary it has reached."""
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
        raise MutationRejected(
            "invalid_gate_context", "Risk score must be numeric"
        ) from exc
    approved = bool(_field(gate_decision, "approved", False))
    calibrated_probability, autonomy_threshold = (
        _runtime_remediation_calibration(
            _field(gate_decision, "raw_action_confidence")
        )
    )

    fresh = decide(
        action,
        SimpleNamespace(severity=severity),
        environment=environment,
        risk_score=risk_score,
        calibrated_action_probability=calibrated_probability,
        minimum_autonomy_probability=autonomy_threshold,
    )
    if fresh.decision is AutonomyDecision.BLOCKED:
        raise MutationRejected("policy_blocked", fresh.reason)
    if fresh.decision is AutonomyDecision.REQUIRES_APPROVAL and not approved:
        raise MutationRejected("approval_required", fresh.reason)

    _verify_scope(action, execution_context)
    async with _incident_mutation_guard(
        _field(gate_decision, "incident_id"), execution_context
    ):
        if not idempotency_key or not str(idempotency_key).strip():
            raise MutationRejected(
                "invalid_idempotency_key", "A non-empty idempotency key is required"
            )
        executor = Executor(
            actor=str(_field(gate_decision, "actor", "sre-agent") or "sre-agent"),
            incident_id=_field(gate_decision, "incident_id"),
        )
        decision = "approved" if approved else fresh.decision.value
        if str(getattr(action, "action_type", "")).lower() == "rollback":
            settled = await _rollback_precondition(action, tool_caller, executor, decision)
            if settled is not None:
                # Nothing was claimed or written; an audit failure here is
                # still pre-claim, and a retry re-reads the same settled state.
                await _persist_audit_event(execution_context, settled, approved=approved)
                return settled
        # From this point onward an exception cannot prove whether the atomic
        # claim was installed. Retrying could therefore turn a missing action
        # into a misleading duplicate, even before the external call begins.
        failure_phase["value"] = "claim_started"
        if not store.set_idempotency(str(idempotency_key), _idempotency_ttl()):
            if hasattr(store, "is_available") and not store.is_available():
                raise MutationRejected(
                    "state_unavailable", "Redis failed during idempotency claim"
                )
            return ExecutionResult(
                action_type=str(getattr(action, "action_type", "")).lower(),
                target=str(getattr(action, "target", "")),
                command=build_command(action),
                mode=ExecutionMode.LIVE,
                status="SKIPPED",
                rollback_command=build_rollback_command(action),
                detail="Duplicate mutation short-circuited by its idempotency claim.",
            )

        failure_phase["value"] = "dispatch_started"
        result = await executor._aexecute_unchecked(
            action,
            decision,
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
        if result.remote_status == "STATE_DRIFT":
            # The executor re-read the deployment after the check above and
            # found it moved: it wrote nothing, and the refusal is the same
            # one the gateway would have made a moment later.
            raise MutationRejected("state_drift", result.detail)
        return result


async def authorize_and_execute(
    action: Any,
    gate_decision: Any,
    context: Optional[ExecutionContext],
    tool_caller: Optional[Callable[[str, Dict[str, Any]], Any]],
    github_caller: Optional[Callable[[str, Dict[str, Any]], Any]],
    idempotency_key: str,
) -> ExecutionResult:
    """Freshly authorize, atomically claim, execute, and audit one mutation.

    Unexpected failures are classified at the last boundary known to have
    completed. Only failures before the atomic claim begins are retryable.
    """
    failure_phase = {"value": "pre_claim"}
    try:
        return await _authorize_and_execute_unclassified(
            action,
            gate_decision,
            context,
            tool_caller,
            github_caller,
            idempotency_key,
            failure_phase,
        )
    except (MutationRejected, MutationRetryUnsafe):
        raise
    except Exception as exc:
        if failure_phase["value"] == "pre_claim":
            raise MutationPreDispatchError(
                "Live mutation setup failed before its idempotency claim; "
                "a bounded retry is safe"
            ) from exc
        raise MutationRetryUnsafe(
            "Live mutation failed after its idempotency claim began; "
            "automatic retry is unsafe"
        ) from exc
