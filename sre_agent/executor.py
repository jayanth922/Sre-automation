#!/usr/bin/env python3
"""
Executor — the ACT phase's hands (Phase 0: dry-run only).

Given a ``RemediationAction`` that the Policy Gate has cleared, the Executor
translates it into the concrete command it *would* run and — in Phase 0 — stops
there, returning the command plus a tamper-evident audit record instead of
touching any cluster. This makes the entire ACT path demoable and reviewable
with zero production risk.

Live execution is intentionally **not** implemented yet: calling with
``dry_run=False`` raises ``NotImplementedError``. That is a deliberate honesty
guarantee — nothing in this file can mutate real infrastructure until Phase 1
wires it to the sandboxed Executor MCP server with least-privilege RBAC.

Dependency-light: operates on any object exposing ``action_type``, ``target``,
``parameters`` (dict) and optional ``rollback_plan``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Maps a RemediationAction.action_type to the executor MCP server tool that
# carries it out. Types with no infra tool (escalate = notify-only,
# revert_commit = handled by the GitHub agent) are intentionally absent.
EXECUTOR_TOOL_MAP: Dict[str, str] = {
    "restart": "restart_deployment",
    "scale": "scale_deployment",
    "rollback": "rollback_deployment",
    "patch": "patch_resource_limits",
    "config_change": "patch_resource_limits",
}


class ExecutionMode(str):
    DRY_RUN = "dry_run"
    LIVE = "live"


@dataclass
class ExecutionResult:
    action_type: str
    target: str
    command: str
    mode: str
    status: str                       # DRY_RUN | EXECUTED | REFUSED | ERROR
    audit: Dict[str, Any] = field(default_factory=dict)
    rollback_command: Optional[str] = None
    detail: str = ""


def _namespace(action: Any) -> str:
    params = getattr(action, "parameters", None) or {}
    if isinstance(params, dict):
        return str(params.get("namespace", "default"))
    return "default"


def _replicas(action: Any, default: int = 1) -> int:
    params = getattr(action, "parameters", None) or {}
    if isinstance(params, dict):
        val = params.get("replicas", params.get("replica_count"))
        try:
            return int(val)
        except (TypeError, ValueError):
            return default
    return default


def build_command(action: Any) -> str:
    """Translate a remediation action into the concrete command it maps to.

    Returns a single shell/kubectl/gh command string. Pure and deterministic so
    it is easy to test and easy to show in a dry-run transcript.
    """
    action_type = str(getattr(action, "action_type", "")).lower()
    target = str(getattr(action, "target", "")) or "<unknown-target>"
    ns = _namespace(action)

    if action_type == "restart":
        return f"kubectl rollout restart deployment/{target} -n {ns}"
    if action_type == "scale":
        return f"kubectl scale deployment/{target} --replicas={_replicas(action)} -n {ns}"
    if action_type == "rollback":
        return f"kubectl rollout undo deployment/{target} -n {ns}"
    if action_type == "patch":
        params = getattr(action, "parameters", None) or {}
        patch = json.dumps(params.get("patch", params)) if isinstance(params, dict) else "{}"
        return f"kubectl patch deployment/{target} -n {ns} --type merge -p '{patch}'"
    if action_type == "config_change":
        return f"kubectl apply -f <rendered-config for {target}> -n {ns}"
    if action_type == "revert_commit":
        params = getattr(action, "parameters", None) or {}
        sha = params.get("commit_sha", params.get("sha", "<sha>")) if isinstance(params, dict) else "<sha>"
        return f"gh pr create --title 'Revert {sha}' --body 'Automated revert of {sha}' (revert {sha})"
    if action_type == "escalate":
        return f"notify on-call: escalate '{target}' (no infrastructure mutation)"
    return f"# no command mapping for action_type='{action_type}' on '{target}'"


def build_rollback_command(action: Any) -> Optional[str]:
    """Best-effort inverse command, for the audit trail and Phase-1 rollback."""
    action_type = str(getattr(action, "action_type", "")).lower()
    target = str(getattr(action, "target", "")) or "<unknown-target>"
    ns = _namespace(action)
    if action_type in ("rollback", "restart", "config_change", "patch"):
        return f"kubectl rollout undo deployment/{target} -n {ns}"
    if action_type == "scale":
        return f"kubectl scale deployment/{target} --replicas=<previous> -n {ns}"
    return None


def _live_args(action: Any) -> Dict[str, Any]:
    """Build the executor-MCP tool arguments for a live (real) execution."""
    params = getattr(action, "parameters", None) or {}
    if not isinstance(params, dict):
        params = {}
    action_type = str(getattr(action, "action_type", "")).lower()
    args: Dict[str, Any] = {
        "name": str(getattr(action, "target", "")),
        "namespace": params.get("namespace", "default"),
        "dry_run": False,  # live apply (the MCP server still validates server-side)
    }
    if action_type == "scale":
        raw = params.get("replicas", params.get("replica_count", 1))
        try:
            args["replicas"] = int(raw)
        except (TypeError, ValueError):
            args["replicas"] = 1
    if action_type in ("patch", "config_change"):
        args["container"] = params.get("container", str(getattr(action, "target", "")))
        if params.get("memory"):
            args["memory"] = params["memory"]
        if params.get("cpu"):
            args["cpu"] = params["cpu"]
    return args


class Executor:
    """Executes cleared remediation actions.

    - ``execute(...)`` is synchronous and dry-run only (local preview + audit).
    - ``aexecute(..., dry_run=False, tool_caller=...)`` performs live remediation
      by calling the executor MCP server through an injected async ``tool_caller``.
    """

    def __init__(self, actor: str = "sre-agent", incident_id: Optional[str] = None):
        self.actor = actor
        self.incident_id = incident_id

    def _audit(self, action: Any, command: str, decision: str, mode: str) -> Dict[str, Any]:
        """Build a tamper-evident audit record.

        A content hash chains the record's own fields so any later edit is
        detectable — mirroring the signed-event audit trail pattern the platform
        should ultimately persist to ``AuditLog``.
        """
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "actor": self.actor,
            "incident_id": self.incident_id,
            "action_type": str(getattr(action, "action_type", "")),
            "target": str(getattr(action, "target", "")),
            "parameters": getattr(action, "parameters", {}) or {},
            "gate_decision": decision,
            "mode": mode,
            "command": command,
        }
        record["content_hash"] = hashlib.sha256(
            json.dumps(record, sort_keys=True, default=str).encode()
        ).hexdigest()
        return record

    def execute(self, action: Any, gate_decision: str, dry_run: bool = True) -> ExecutionResult:
        """Execute (or dry-run) a single cleared action.

        Args:
            action: a Policy-Gate-cleared remediation action.
            gate_decision: the AutonomyDecision value that cleared this action
                (``"autonomous"`` or ``"requires_approval"`` post-approval).
            dry_run: Phase 0 must be True; live execution is not implemented yet.
        """
        command = build_command(action)
        rollback = build_rollback_command(action)
        mode = ExecutionMode.DRY_RUN if dry_run else ExecutionMode.LIVE

        if not dry_run:
            # Synchronous live execution is unsupported by design — live apply
            # goes through the executor MCP server, which is async.
            raise NotImplementedError(
                "Synchronous live execution is not supported. Use "
                "'await Executor.aexecute(..., dry_run=False, tool_caller=...)'."
            )

        audit = self._audit(action, command, gate_decision, mode)
        logger.info(f"🧪 Executor[dry-run]: would run → {command}")
        return ExecutionResult(
            action_type=str(getattr(action, "action_type", "")),
            target=str(getattr(action, "target", "")),
            command=command,
            mode=mode,
            status="DRY_RUN",
            audit=audit,
            rollback_command=rollback,
            detail="Dry-run only; no cluster mutation performed.",
        )

    async def aexecute(
        self,
        action: Any,
        gate_decision: str,
        dry_run: bool = True,
        tool_caller: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    ) -> "ExecutionResult":
        """Async execution. Dry-run mirrors ``execute``; live calls the MCP server.

        Args:
            tool_caller: async ``(tool_name, args) -> result`` that invokes the
                executor MCP server tool. Injected so this is testable without a
                live server. Build a real one with ``build_executor_tool_caller``.
        """
        if dry_run:
            return self.execute(action, gate_decision, dry_run=True)

        command = build_command(action)
        rollback = build_rollback_command(action)
        atype = str(getattr(action, "action_type", "")).lower()
        target = str(getattr(action, "target", ""))
        audit = self._audit(action, command, gate_decision, ExecutionMode.LIVE)

        if tool_caller is None:
            return ExecutionResult(
                action_type=atype, target=target, command=command,
                mode=ExecutionMode.LIVE, status="ERROR", audit=audit,
                rollback_command=rollback,
                detail="Live execution requested but no executor tool_caller configured.",
            )

        tool_name = EXECUTOR_TOOL_MAP.get(atype)
        if not tool_name:
            return ExecutionResult(
                action_type=atype, target=target, command=command,
                mode=ExecutionMode.LIVE, status="SKIPPED", audit=audit,
                rollback_command=rollback,
                detail=f"No executor MCP tool maps to action_type '{atype}'.",
            )

        try:
            resp = await tool_caller(tool_name, _live_args(action))
            logger.info(f"⚙️  Executor[live]: {tool_name} → applied ({command})")
            return ExecutionResult(
                action_type=atype, target=target, command=command,
                mode=ExecutionMode.LIVE, status="EXECUTED", audit=audit,
                rollback_command=rollback, detail=str(resp),
            )
        except Exception as e:
            logger.error(f"❌ Executor[live]: {tool_name} failed: {e}")
            return ExecutionResult(
                action_type=atype, target=target, command=command,
                mode=ExecutionMode.LIVE, status="ERROR", audit=audit,
                rollback_command=rollback, detail=f"executor MCP call failed: {e}",
            )


async def build_executor_tool_caller(uri: Optional[str] = None):
    """Build a live async tool_caller bound to the executor MCP server.

    Lazily imports the MCP adapter so importing this module stays dependency-light.
    """
    uri = uri or os.getenv("MCP_EXECUTOR_URI")
    if not uri:
        raise RuntimeError("MCP_EXECUTOR_URI is not set; cannot reach the executor server")

    from langchain_mcp_adapters.client import MultiServerMCPClient  # lazy

    client = MultiServerMCPClient({"executor": {"url": uri, "transport": "sse"}})
    tools = await client.get_tools()
    by_name = {getattr(t, "name", ""): t for t in tools}

    async def _caller(tool_name: str, args: Dict[str, Any]) -> Any:
        tool = by_name.get(tool_name)
        if tool is None:
            raise RuntimeError(
                f"executor tool '{tool_name}' not found (available: {sorted(by_name)})"
            )
        if hasattr(tool, "ainvoke"):
            return await tool.ainvoke(args)
        return tool.invoke(args)

    return _caller
