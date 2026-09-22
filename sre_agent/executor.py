#!/usr/bin/env python3
"""
Executor — the ACT phase's hands.

Given a ``RemediationAction`` that the Policy Gate has cleared, the Executor
translates it into the concrete command to run, returning that command plus a
tamper-evident audit record. Under ``dry_run=True`` it stops there, having
touched nothing, which is what makes the whole ACT path demoable and
reviewable at zero risk.

Live apply *is* wired — just not through this class's synchronous entry point.
``execute(dry_run=False)`` raises ``NotImplementedError`` on purpose and names
the supported path in the message. Real mutations go through
``mutation_gateway.authorize_and_execute``, called from ``act_phase`` once the
policy gate and, above low severity, a human approval have cleared the exact
action; see ``act_phase.execute_autonomous_live``. The refusal below is a
guard rail against a second, unaudited apply path, not evidence that the
product cannot act.

Dependency-light: operates on any object exposing ``action_type``, ``target``,
``parameters`` (dict) and optional ``rollback_plan``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional

from .execution_context import (
    ExecutionContext,
    require_execution_context,
    require_operator_mcp_endpoint,
)

logger = logging.getLogger(__name__)

# Infra remediation → executor MCP server tools. (escalate = notify-only, absent.)
# The value is the action's *default* tool; `patch`/`config_change` resolve to
# one of two tools depending on what the action carries — see
# `live_tool_for_action`, which is the authority.
EXECUTOR_TOOL_MAP: Dict[str, str] = {
    "restart": "restart_deployment",
    "scale": "scale_deployment",
    "rollback": "rollback_deployment",
    "patch": "patch_resource_limits",
    "config_change": "patch_resource_limits",
    "recreate_pod": "recreate_pod",
    "inspect": "get_deployment_config",
}

# The third dispatch family, alongside the two tool maps below: action types
# that reach a *human* rather than an MCP server. They mutate nothing, so they
# deliberately have no tool — but that also means every "is this a known
# action?" check written as "in one of the tool maps" silently misclassifies
# them as unsupported. Keep them here so those checks can be explicit instead.
NOTIFY_ONLY_ACTIONS: frozenset = frozenset({"escalate"})

# The fourth family: actions that reach the cluster but only *read* it. They
# have a tool and a namespace worth scope-checking, so unlike notify-only they
# take the normal dispatch path — but nothing about severity, telemetry
# completeness or calibration can make looking unsafe, so the policy gate lets
# them run without approval (see `policy_gate.decide`).
READ_ONLY_ACTIONS: frozenset = frozenset({"inspect"})

# "Did this action actually change anything?" Verification and skill learning
# both need this: a page and a config dump are EXECUTED too, and grading an
# incident on either would call it resolved before anyone touched the system.
NON_MUTATING_ACTIONS: frozenset = NOTIFY_ONLY_ACTIONS | READ_ONLY_ACTIONS

# Code-change remediation → github-exec MCP server tools. This is what makes an
# LLM-suggested code fix (revert the bad deploy) actually execute, not just be
# proposed. Routed to a separate caller (the github-exec server).
GITHUB_EXEC_TOOL_MAP: Dict[str, str] = {
    "revert_commit": "create_revert_pr",
    "revert_pr": "create_revert_pr",
    "comment_pr": "comment_on_pr",
}

# Sandbox verification → sandbox MCP server tools (K8s Job lifecycle for the
# Temporal-orchestrated code-fix verification workflow). Not reachable through
# Executor._aexecute_unchecked — the workflow calls these via
# sandbox_gateway.authorize_and_provision_sandbox, never directly.
SANDBOX_TOOL_MAP: Dict[str, str] = {
    "provision": "sandbox_provision",
    "status": "sandbox_status",
    "logs": "sandbox_logs",
    "teardown": "sandbox_teardown",
}

# Action types that name an *intent* ("change some config") rather than a tool.
# Two tools serve them — `patch_resource_limits` for a cpu/memory change,
# `patch_deployment_env` for a runtime env var — and which one applies is
# decided by what the action carries, never by its name. Routing on the name
# was the original defect: it claimed a capability that did not exist, so a
# human approved a step nothing could run and the refusal then blamed the
# planner ("provide at least one of memory/cpu") for a missing tool.
_CONFIG_INTENT_ACTIONS: frozenset = frozenset({"patch", "config_change"})


def live_tool_for_action(action: Any) -> Optional[str]:
    """The executor-MCP tool that can really carry out this action, or None.

    The canonical answer to "can Sentinel actually execute this?" for infra
    actions. Membership in ``EXECUTOR_TOOL_MAP`` is necessary but not
    sufficient: ``patch``/``config_change`` resolve to the resource tool when
    the action carries a cpu/memory limit, to the env tool when it carries env
    vars, and to nothing at all otherwise — a ConfigMap rewrite or a change
    described only in prose still has no tool behind it, and naming that as a
    capability gap is the honest failure.
    """
    action_type = str(getattr(action, "action_type", "")).lower()
    tool_name = EXECUTOR_TOOL_MAP.get(action_type)
    if tool_name is None:
        return None
    if action_type in _CONFIG_INTENT_ACTIONS:
        params = getattr(action, "parameters", None) or {}
        if not isinstance(params, dict):
            return None
        if _find_resource_field(params, "memory") or _find_resource_field(params, "cpu"):
            return "patch_resource_limits"
        if _find_env_map(params):
            return "patch_deployment_env"
        return None
    return tool_name


# What approving an action actually causes. The four dispatch families answer
# "where does this go?"; these answer the question a human is really being
# asked in Slack — "what happens to my systems if I say yes?".
EFFECT_CLUSTER_CHANGE = "cluster_change"
EFFECT_REPO_CHANGE = "repo_change"
EFFECT_NOTIFICATION = "notification"
EFFECT_READ_ONLY = "read_only"
EFFECT_NO_CAPABILITY = "no_capability"

# Operator-facing wording, (singular, plural). Deliberately concrete: "a page"
# is not "a change to the cluster", and an approver who reads one as the other
# has been misinformed by the one message that gates the whole system. Both
# forms are spelled out rather than derived, because the head noun is not
# always the first word ("read-only check").
EFFECT_LABELS: Dict[str, tuple] = {
    EFFECT_CLUSTER_CHANGE: ("change to the cluster", "changes to the cluster"),
    EFFECT_REPO_CHANGE: ("change to the repository", "changes to the repository"),
    EFFECT_NOTIFICATION: (
        "notification to a human (no system change)",
        "notifications to humans (no system change)",
    ),
    EFFECT_READ_ONLY: (
        "read-only check (no system change)",
        "read-only checks (no system change)",
    ),
    EFFECT_NO_CAPABILITY: (
        "action Sentinel cannot execute (it will be skipped)",
        "actions Sentinel cannot execute (they will be skipped)",
    ),
}


def approval_effect(action_type: Any, parameters: Any = None) -> str:
    """What approving this one action really does, by capability not by name.

    `format_approval_request` used to tell the approver that approving runs N
    held actions "against the cluster", counted straight off the held list.
    Two kinds of action make that untrue, and both show up in real plans:

    - `escalate` is notify-only. It pages a human and mutates nothing. A plan
      whose only held actions are two escalations was described as two cluster
      writes (live, incident 2c49ac9d on 2026-09-14).
    - `code_fix` is in no dispatch map at all, so it can never execute; at run
      time it reports `SKIPPED: No MCP tool maps to action_type 'code_fix'`.
      It was still counted as something approving would run (live, incident
      8c925dbd: "Approving runs 3 held actions against the cluster", one of
      them a `code_fix` that could not run).

    This routes on the same capability logic as dispatch — a `patch` or
    `config_change` with neither a cpu/memory limit nor env vars has no tool
    behind it and is reported as such, not as a cluster change.
    """
    name = str(action_type or "").lower().strip()
    if name in NOTIFY_ONLY_ACTIONS:
        return EFFECT_NOTIFICATION
    if name in READ_ONLY_ACTIONS:
        return EFFECT_READ_ONLY
    if name in GITHUB_EXEC_TOOL_MAP:
        return EFFECT_REPO_CHANGE
    if name not in EXECUTOR_TOOL_MAP:
        return EFFECT_NO_CAPABILITY
    if name in _CONFIG_INTENT_ACTIONS:
        params = parameters if isinstance(parameters, dict) else {}
        if _find_resource_field(params, "memory") or _find_resource_field(params, "cpu"):
            return EFFECT_CLUSTER_CHANGE
        if _find_env_map(params):
            return EFFECT_CLUSTER_CHANGE
        return EFFECT_NO_CAPABILITY
    return EFFECT_CLUSTER_CHANGE


def describe_approval_effects(action_reports: Any) -> str:
    """One sentence saying what approving this plan actually does.

    Returns "" when nothing is held, so the caller can omit the line entirely
    rather than print "Approving runs 0 actions".
    """
    held = [
        rep
        for rep in (action_reports or [])
        if isinstance(rep, dict) and str(rep.get("decision")) == "requires_approval"
    ]
    if not held:
        return ""
    counts: Dict[str, int] = {}
    for rep in held:
        effect = approval_effect(rep.get("action_type"), rep.get("parameters"))
        counts[effect] = counts.get(effect, 0) + 1

    order = [
        EFFECT_CLUSTER_CHANGE,
        EFFECT_REPO_CHANGE,
        EFFECT_NOTIFICATION,
        EFFECT_READ_ONLY,
        EFFECT_NO_CAPABILITY,
    ]
    parts = []
    for effect in order:
        n = counts.get(effect, 0)
        if not n:
            continue
        singular, plural = EFFECT_LABELS[effect]
        parts.append(f"{n} {singular}" if n == 1 else f"{n} {plural}")

    total = len(held)
    plural = "" if total == 1 else "s"
    return f"Approving runs {total} held action{plural}: " + ", ".join(parts) + "."


def missing_capability_reason(action_type: str) -> str:
    """Why a known-but-unexecutable action cannot run, in the operator's terms."""
    return (
        f"no automation capability: '{action_type}' names neither a cpu/memory "
        "limit (parameters.memory / parameters.cpu) nor environment variables "
        "(parameters.env), which are the two configuration surfaces Sentinel "
        "can mutate. A ConfigMap, a Helm value or a change described only in "
        "prose needs a human."
    )


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


# Mirrors the executor MCP server's guardrail denylist. The edge refuses to
# *write* a credential-named env var; this keeps its value out of the rendered
# command, which is persisted verbatim in the audit trail and shown in Slack.
# A refusal must not be the thing that logs the secret.
_CREDENTIAL_KEY_RE = re.compile(
    r"(SECRET|PASSWORD|PASSWD|TOKEN|CREDENTIAL|PRIVATE_KEY|API_?KEY|_KEY$|^KEY$|AUTH|SESSION|SALT|CERT)",
    re.IGNORECASE,
)


def _redact_env_value(key: str, value: Any) -> str:
    """The value as it should appear in a transcript: hidden if the name is a secret."""
    return "[REDACTED]" if _CREDENTIAL_KEY_RE.search(str(key)) else str(value)


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
        params = getattr(action, "parameters", None) or {}
        if not isinstance(params, dict):
            params = {}
        memory = _find_resource_field(params, "memory")
        cpu = _find_resource_field(params, "cpu")
        if memory or cpu:
            limits = ",".join(f"{k}={v}" for k, v in (("memory", memory), ("cpu", cpu)) if v)
            return f"kubectl set resources deployment/{target} -c {target} --limits={limits} -n {ns}"
        env = _find_env_map(params)
        if env:
            container = params.get("container", target)
            pairs = " ".join(f"{k}={_redact_env_value(k, v)}" for k, v in env.items())
            return f"kubectl set env deployment/{target} -c {container} {pairs} -n {ns}"
        # No tool can issue this; `live_tool_for_action` blocks it upstream.
        return f"# no executable config change for '{target}' (no resource limit, no env vars)"
    if action_type == "inspect":
        return f"kubectl get deployment/{target} -n {ns} -o yaml  # read-only"
    if action_type == "recreate_pod":
        return f"kubectl delete pod/{target} -n {ns}"
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
        # `kubectl set env` and `set resources` both cut a new ReplicaSet
        # revision, so rollout undo is a true inverse for them as well. The
        # executor MCP additionally returns the exact prior values it
        # overwrote (`prior_env`), which the audit trail keeps.
        return f"kubectl rollout undo deployment/{target} -n {ns}"
    if action_type == "scale":
        return f"kubectl scale deployment/{target} --replicas=<previous> -n {ns}"
    # A read has no inverse; returning a command here would imply it changed
    # something. (`recreate_pod` likewise — the controller recreates the pod.)
    return None


def _github_args(action: Any) -> Dict[str, Any]:
    """Build github-exec tool arguments for a code-change action."""
    params = getattr(action, "parameters", None) or {}
    if not isinstance(params, dict):
        params = {}
    atype = str(getattr(action, "action_type", "")).lower()
    if atype in ("revert_commit", "revert_pr"):
        identifier = params.get("commit_sha") or params.get("sha") or params.get("pr_number") or ""
        return {"identifier": str(identifier), "dry_run": False}
    if atype == "comment_pr":
        return {"pr_number": params.get("pr_number"), "body": str(params.get("body", "")), "dry_run": False}
    return {"dry_run": False}


# Planner targets are frequently not a bare k8s object name: sometimes
# "<deployment>:<sub-resource descriptor>" (e.g.
# "checkout-service:process-handler-pods"), sometimes a full descriptive
# phrase (e.g. "checkout-service pods (targeted canary subset only, ...)").
# A k8s object name is a DNS-1123 label (lowercase alphanumeric + '-'), so
# the leading run of valid label characters is the real resource name in
# either case — this subsumes the old colon-only split.
_K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?")


_RESOURCE_FIELD_ALIASES = {
    "memory": ("memory", "proposed_memory_limit", "memory_limit", "new_memory_limit"),
    "cpu": ("cpu", "proposed_cpu_limit", "cpu_limit", "new_cpu_limit"),
}


def _find_resource_field(params: Dict[str, Any], field: str) -> Optional[str]:
    """Find a memory/cpu limit value anywhere in planner-produced parameters.

    The planner's parameter shape for a resource-limit change isn't stable
    across runs — it may emit a flat {"memory": "512Mi"}, an alias like
    {"proposed_memory_limit": "512Mi"}, or a nested
    {"resources": {"limits": {"memory": "512Mi"}}}. Search recursively,
    preferring a hit found under a "limit(s)" key over one under
    "request(s)" when both are present.
    """
    aliases = _RESOURCE_FIELD_ALIASES[field]
    limit_hits: list = []
    other_hits: list = []

    def walk(node: Any, under_limit: bool) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                key_lower = str(key).lower()
                if key_lower in aliases and isinstance(value, (str, int, float)):
                    (limit_hits if under_limit else other_hits).append(str(value))
                else:
                    walk(value, under_limit or "limit" in key_lower)
        elif isinstance(node, list):
            for item in node:
                walk(item, under_limit)

    walk(params, False)
    if limit_hits:
        return limit_hits[0]
    if other_hits:
        return other_hits[0]
    return None


_ENV_FIELD_ALIASES = ("env", "env_vars", "environment", "environment_variables")
# A k8s env var name (C_IDENTIFIER). Used to tell a genuine env map apart from
# some other nested dict that happens to sit under a key called "environment"
# — "production" is an environment; {"SLOW_QUERY_RATE": "0"} is env vars.
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _find_env_map(params: Dict[str, Any]) -> Dict[str, str]:
    """Find environment variables to set anywhere in planner-produced parameters.

    Mirrors ``_find_resource_field``'s tolerance for shape drift: the planner
    may emit ``{"env": {...}}``, ``{"env_vars": {...}}``, or bury either under
    a nested object. A match must look like env vars — every key a valid
    variable name, every value a scalar — so ``{"environment": "production"}``
    is not mistaken for one.
    """
    found: Dict[str, str] = {}

    def looks_like_env(value: Any) -> bool:
        return (
            isinstance(value, dict)
            and bool(value)
            and all(
                isinstance(k, str)
                and _ENV_KEY_RE.match(k)
                and not isinstance(v, (dict, list))
                for k, v in value.items()
            )
        )

    def walk(node: Any) -> None:
        if found or not isinstance(node, (dict, list)):
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        for key, value in node.items():
            if str(key).lower() in _ENV_FIELD_ALIASES and looks_like_env(value):
                found.update({str(k): "" if v is None else str(v) for k, v in value.items()})
                return
        for value in node.values():
            walk(value)

    walk(params)
    return found


def _live_args(action: Any) -> Dict[str, Any]:
    """Build the executor-MCP tool arguments for a live (real) execution."""
    params = getattr(action, "parameters", None) or {}
    if not isinstance(params, dict):
        params = {}
    action_type = str(getattr(action, "action_type", "")).lower()
    target = str(getattr(action, "target", "")).strip()
    match = _K8S_NAME_RE.match(target.lower())
    resource_name = match.group(0) if match else target
    args: Dict[str, Any] = {
        "name": resource_name,
        "namespace": params.get("namespace", "default"),
        "dry_run": False,  # live apply (the MCP server still validates server-side)
    }
    if action_type == "scale":
        raw = params.get("replicas", params.get("replica_count", 1))
        try:
            args["replicas"] = int(raw)
        except (TypeError, ValueError):
            args["replicas"] = 1
    if action_type in _CONFIG_INTENT_ACTIONS:
        args["container"] = params.get("container", resource_name)
        memory = _find_resource_field(params, "memory")
        cpu = _find_resource_field(params, "cpu")
        if memory or cpu:
            if memory:
                args["memory"] = memory
            if cpu:
                args["cpu"] = cpu
        else:
            # Only when there is no resource change: `live_tool_for_action`
            # prefers the resource tool, and sending env to it would be ignored.
            args["env"] = _find_env_map(params)
    if action_type in READ_ONLY_ACTIONS:
        # A read takes no dry_run: there is nothing to not-do.
        args.pop("dry_run", None)
        container = params.get("container")
        if container:
            args["container"] = str(container)
    return args


_REFUSAL_STATUSES = {"REFUSED", "DENIED", "MANUAL_REQUIRED", "DRY_RUN"}
_ERROR_STATUSES = {"ERROR", "FAILED", "FAILURE", "UNHEALTHY"}
_SUCCESS_STATUSES = {"OK", "SUCCESS", "EXECUTED", "REVERT_REQUESTED"}


def _structured_payload(value: Any) -> Optional[Dict[str, Any]]:
    """Extract a structured MCP tool payload from common adapter wrappers."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return None
        return _structured_payload(decoded)
    if isinstance(value, Mapping):
        payload = dict(value)
        if "status" in payload or "applied" in payload:
            return payload
        # MCP SDK content blocks are plain dicts like {"type": "text", "text":
        # "<json>", "id": ...} — the JSON-encoded tool payload lives under the
        # "text" *key*, not a ".text" attribute (that's only handled below for
        # SDK objects). Recurse into it like any other wrapper key.
        for key in ("result", "data", "content", "text"):
            if key in payload:
                nested = _structured_payload(payload[key])
                if nested is not None:
                    return nested
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            nested = _structured_payload(item)
            if nested is not None:
                return nested
        return None
    text = getattr(value, "text", None)
    return _structured_payload(text) if text is not None else None


def _mark_trace(level: str, message: str) -> None:
    """Surface a non-EXECUTED live action on the enclosing Langfuse observation.

    Same honesty rule as ``classify_live_response`` below, applied to the
    trace: a remediation that was refused must not read as a clean run. Lazy
    import keeps this module importable without the tracing stack, and
    tracing is never load-bearing for execution.
    """
    try:
        from sre_agent.tracing import mark_current_observation

        mark_current_observation(level, message)
    except Exception:  # pragma: no cover - never let tracing break execution
        pass


def classify_live_response(response: Any) -> tuple[str, str]:
    """Map an MCP response to an honest execution outcome, failing closed."""
    payload = _structured_payload(response)
    detail = (
        json.dumps(payload, sort_keys=True, default=str)
        if payload is not None
        else str(response)
    )
    if payload is None:
        return "ERROR", f"Unstructured MCP response; execution not confirmed: {detail}"

    remote_status = str(payload.get("status", "")).strip().upper()
    applied = payload.get("applied")
    if remote_status in _ERROR_STATUSES:
        return "ERROR", detail
    if remote_status in _REFUSAL_STATUSES or applied is False:
        return "REFUSED", detail
    if applied is True or remote_status in _SUCCESS_STATUSES:
        return "EXECUTED", detail
    return "ERROR", f"MCP response did not confirm execution: {detail}"


class Executor:
    """Executes cleared remediation actions.

    - ``execute(...)`` is synchronous and dry-run only (local preview + audit).
    - Live remediation is private and may only be reached through
      ``mutation_gateway.authorize_and_execute``.
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
                "'await mutation_gateway.authorize_and_execute(...)'."
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

    async def _aexecute_unchecked(
        self,
        action: Any,
        gate_decision: str,
        dry_run: bool = True,
        tool_caller: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
        github_caller: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    ) -> "ExecutionResult":
        """Unchecked execution core; live callers must use the mutation gateway.

        Infra actions (restart/scale/…) go to the executor MCP via ``tool_caller``;
        code-change actions (revert_commit/…) go to the github-exec MCP via
        ``github_caller``. Both injected so this is testable without live servers.
        """
        if dry_run:
            return self.execute(action, gate_decision, dry_run=True)

        command = build_command(action)
        rollback = build_rollback_command(action)
        atype = str(getattr(action, "action_type", "")).lower()
        target = str(getattr(action, "target", ""))
        audit = self._audit(action, command, gate_decision, ExecutionMode.LIVE)

        def _result(status: str, detail: str) -> "ExecutionResult":
            return ExecutionResult(
                action_type=atype, target=target, command=command,
                mode=ExecutionMode.LIVE, status=status, audit=audit,
                rollback_command=rollback, detail=detail,
            )

        # Route to the right backend by what the action can actually do, not by
        # its name: a `config_change` with no cpu/memory limit has no tool.
        executor_tool = live_tool_for_action(action)
        if atype in GITHUB_EXEC_TOOL_MAP:
            caller, tool_name, args, backend = github_caller, GITHUB_EXEC_TOOL_MAP[atype], _github_args(action), "github-exec"
        elif executor_tool is not None:
            caller, tool_name, args, backend = tool_caller, executor_tool, _live_args(action), "executor"
        elif atype in EXECUTOR_TOOL_MAP:
            detail = missing_capability_reason(atype)
            _mark_trace("WARNING", f"{atype} → REFUSED: {detail}")
            return _result("REFUSED", detail)
        else:
            return _result("SKIPPED", f"No MCP tool maps to action_type '{atype}'.")

        if caller is None:
            return _result("ERROR", f"Live execution requested but no {backend} tool_caller configured.")

        try:
            resp = await caller(tool_name, args)
            status, detail = classify_live_response(resp)
            if status == "EXECUTED":
                logger.info(
                    f"⚙️  Executor[live/{backend}]: {tool_name} → applied ({command})"
                )
            else:
                logger.warning(
                    f"⛔ Executor[live/{backend}]: {tool_name} → {status.lower()}: {detail}"
                )
                _mark_trace(
                    "ERROR" if status == "ERROR" else "WARNING",
                    f"{tool_name} → {status}: {detail}",
                )
            return _result(status, detail)
        except Exception as e:
            logger.error(f"❌ Executor[live/{backend}]: {tool_name} failed: {e}")
            _mark_trace("ERROR", f"{tool_name} raised: {e}")
            return _result("ERROR", f"{backend} MCP call failed: {e}")


async def build_mcp_tool_caller(
    context: Optional[ExecutionContext],
    server_name: str = "server",
    *,
    uri: Optional[str] = None,
):
    """Build a generic async tool_caller bound to any MCP (SSE) server.

    Lazily imports the MCP adapter so importing this module stays dependency-light.
    Returns an async ``(tool_name, args) -> result`` callable.
    """
    execution_context = require_execution_context(context)
    endpoint = uri or execution_context.endpoint(server_name)
    endpoint = require_operator_mcp_endpoint(server_name, endpoint)

    from langchain_mcp_adapters.client import MultiServerMCPClient  # lazy

    client = MultiServerMCPClient(
        {
            server_name: {
                "url": endpoint,
                "transport": "sse",
                "headers": execution_context.transport_headers(),
            }
        }
    )
    tools = await client.get_tools()
    by_name = {getattr(t, "name", ""): t for t in tools}

    async def _caller(tool_name: str, args: Dict[str, Any]) -> Any:
        tool = by_name.get(tool_name)
        if tool is None:
            raise RuntimeError(f"tool '{tool_name}' not found (available: {sorted(by_name)})")
        if hasattr(tool, "ainvoke"):
            return await tool.ainvoke(args)
        return tool.invoke(args)

    _caller.mcp_client = client
    return _caller


async def build_executor_tool_caller(
    context: Optional[ExecutionContext] = None,
    *,
    uri: Optional[str] = None,
):
    """Tool caller bound to this tenant's executor MCP server."""
    return await build_mcp_tool_caller(context, "executor", uri=uri)


async def build_metrics_tool_caller(
    context: Optional[ExecutionContext] = None,
    *,
    uri: Optional[str] = None,
):
    """Tool caller bound to this tenant's Prometheus MCP server."""
    return await build_mcp_tool_caller(context, "metrics", uri=uri)


async def build_github_exec_tool_caller(
    context: Optional[ExecutionContext] = None,
    *,
    uri: Optional[str] = None,
):
    """Tool caller bound to this tenant's GitHub executor MCP server."""
    return await build_mcp_tool_caller(context, "github_exec", uri=uri)


async def build_sandbox_tool_caller(
    context: Optional[ExecutionContext] = None,
    *,
    uri: Optional[str] = None,
):
    """Tool caller bound to this tenant's sandbox MCP server."""
    return await build_mcp_tool_caller(context, "sandbox", uri=uri)


async def build_k8s_tool_caller(
    context: Optional[ExecutionContext] = None,
    *,
    uri: Optional[str] = None,
):
    """Tool caller bound to this tenant's Kubernetes evidence MCP server."""
    return await build_mcp_tool_caller(context, "k8s", uri=uri)
