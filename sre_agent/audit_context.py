"""Task-local audit scope for the agent flight recorder."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Optional, Tuple

# Context Variables to hold state during Agent execution
# These are thread-local (or task-local in asyncio), ensuring safety in concurrent execution

_incident_id_ctx: ContextVar[Optional[str]] = ContextVar("incident_id", default=None)
_agent_name_ctx: ContextVar[str] = ContextVar("agent_name", default="UnknownAgent")
_organization_id_ctx: ContextVar[Optional[str]] = ContextVar(
    "organization_id", default=None
)
_cluster_id_ctx: ContextVar[Optional[str]] = ContextVar("cluster_id", default=None)
_run_id_ctx: ContextVar[Optional[str]] = ContextVar("run_id", default=None)
_audit_write_failure_ctx: ContextVar[Optional[str]] = ContextVar(
    "audit_write_failure", default=None
)


def set_audit_context(
    incident_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    *,
    organization_id: Optional[str] = None,
    cluster_id: Optional[str] = None,
    run_id: Optional[str] = None,
):
    """Set the current audit context for the running task.

    Scope fields (organization/cluster/run) are only overwritten when explicitly
    provided so specialist nodes can update agent/incident without clearing the
    investigation-wide identifiers set at run start.
    """
    if incident_id is not None:
        _incident_id_ctx.set(incident_id)
    if agent_name is not None:
        _agent_name_ctx.set(agent_name)
    if organization_id is not None:
        _organization_id_ctx.set(organization_id)
    if cluster_id is not None:
        _cluster_id_ctx.set(cluster_id)
    if run_id is not None:
        _run_id_ctx.set(run_id)


def get_audit_context() -> (
    Tuple[Optional[str], str, Optional[str], Optional[str], Optional[str]]
):
    """Return (incident_id, agent_name, organization_id, cluster_id, run_id)."""
    return (
        _incident_id_ctx.get(),
        _agent_name_ctx.get(),
        _organization_id_ctx.get(),
        _cluster_id_ctx.get(),
        _run_id_ctx.get(),
    )


def note_audit_write_failure(error: str) -> None:
    """Record a flight-recorder persistence failure for job-health checks."""
    previous = _audit_write_failure_ctx.get()
    if previous:
        _audit_write_failure_ctx.set(f"{previous}; {error}")
    else:
        _audit_write_failure_ctx.set(error)


def pop_audit_write_failure() -> Optional[str]:
    """Return and clear any recorded audit write failure for this task."""
    error = _audit_write_failure_ctx.get()
    _audit_write_failure_ctx.set(None)
    return error


def clear_audit_context():
    """Reset context to defaults."""
    _incident_id_ctx.set(None)
    _agent_name_ctx.set("UnknownAgent")
    _organization_id_ctx.set(None)
    _cluster_id_ctx.set(None)
    _run_id_ctx.set(None)
    _audit_write_failure_ctx.set(None)
