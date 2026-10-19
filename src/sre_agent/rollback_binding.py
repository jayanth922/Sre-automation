#!/usr/bin/env python3
"""Bind a planned rollback to the revision it restores and the one it replaces.

A rollback used to mean ``kubectl rollout undo``: "the previous revision", as
of whenever it ran. Kubernetes renumbers revisions on every rollback (the
restored ReplicaSet takes the next number), so "previous" is relative to the
last write: a retry after success, a second approval or a restart made earlier
each move it, and undo toggles straight back onto the faulty version.

So a rollback is bound before anyone approves it. After planning, this reads
the deployment's history from the executor (``get_rollout_state``) and records
in the action's parameters which ReplicaSet to restore — by pod-template-hash,
the one identity that survives renumbering — and which template it expects to
replace. The approval request shows both and the approval hash covers them. At
execution the mutation gateway and the executor each re-read the deployment
and act only in those two states: already on the target (nothing to write) or
still on the expected template (roll back). Anything else is drift, refused
for a human.

A rollback this cannot bind is left unbound, with the reason, and the act
report blocks it: an approver must never be asked to authorize a rollback
whose target nobody has read.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

ROLLBACK_BINDING_KEY = "rollback_binding"
UNBOUND_REASON_KEY = "unbound_reason"

# The executor MCP server's guardrail, mirrored: a pod-template-hash value.
TEMPLATE_HASH_RE = re.compile(r"^[a-z0-9]{1,63}$")

# Actions that rewrite a deployment's pod template. One of these earlier in the
# same plan moves the deployment off the template a rollback is bound to, so
# the rollback's precondition could never hold by the time it ran.
_TEMPLATE_WRITES = frozenset({"restart", "rollback", "patch", "config_change"})


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _replace(obj: Any, **fields: Any) -> Any:
    """A copy of ``obj`` with ``fields`` set: pydantic model, dict or object."""
    if isinstance(obj, dict):
        return {**obj, **fields}
    if hasattr(obj, "model_copy"):
        return obj.model_copy(update=fields)
    clone = copy.copy(obj)
    for key, value in fields.items():
        setattr(clone, key, value)
    return clone


def bound_rollback(parameters: Any) -> Optional[Dict[str, Any]]:
    """The action's rollback binding, or None when it has none it can run on.

    Well-formed means: a deployment and namespace, and two distinct template
    hashes. Anything less is the relative rollback this exists to remove.
    """
    if not isinstance(parameters, dict):
        return None
    binding = parameters.get(ROLLBACK_BINDING_KEY)
    if not isinstance(binding, dict):
        return None
    for key in ("deployment", "namespace"):
        if not isinstance(binding.get(key), str) or not binding[key].strip():
            return None
    hashes = (binding.get("to_template_hash"), binding.get("expected_template_hash"))
    if not all(isinstance(h, str) and TEMPLATE_HASH_RE.match(h) for h in hashes):
        return None
    if hashes[0] == hashes[1]:
        return None
    return binding


def unbound_reason(parameters: Any) -> str:
    """Why this rollback has no binding, as recorded when binding was tried."""
    binding = parameters.get(ROLLBACK_BINDING_KEY) if isinstance(parameters, dict) else None
    reason = binding.get(UNBOUND_REASON_KEY) if isinstance(binding, dict) else None
    return str(reason or "it was never bound to the deployment's revision history")


def rollback_command(binding: Dict[str, Any]) -> str:
    """The bound rollback as one line: what the approver reads, what runs.

    The executor renders the identical string as its ``kubectl_equivalent``
    (``executor_real/rollout.py:rollback_command``); a test holds the two
    together. Not a kubectl command — kubectl has no hash-addressed,
    preconditioned rollback.
    """
    return (
        f"sentinel rollback deployment/{binding['deployment']} -n {binding['namespace']} "
        f"--to-template={binding['to_template_hash']} "
        f"--expect-template={binding['expected_template_hash']}"
    )


def _created(revision: Dict[str, Any]) -> Optional[datetime]:
    text = str(revision.get("created_at") or "").strip()
    if not text:
        return None
    try:
        created = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return created if created.tzinfo else created.replace(tzinfo=timezone.utc)


def select_rollback_target(
    rollout: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], str]:
    """Pick ``(target, current, reason)`` from a ``get_rollout_state`` read.

    The target is the most recent revision (by revision number) that runs
    different software from the current one *and predates it*:

    - "Different" is by template fingerprint, which ignores a rolling
      restart's timestamp. A restart made after a bad deploy is a new revision
      running the bad deploy, and choosing it — what ``rollout undo`` did —
      rolls back onto the fault.
    - "Predates" is by when the current software first appeared: the oldest
      ReplicaSet with the current fingerprint. Revision numbers cannot say
      this, because a rollback renumbers the template it restores to the top.
      After a rollback from v2 to v1, v2 is the highest-numbered other
      revision, and binding to it would roll *forward* onto the fault the
      first rollback removed. Creation times are never renumbered.

    ``reason`` explains a ``None`` target.
    """
    if rollout.get("paused"):
        return None, None, "the deployment's rollout is paused"
    current_hash = rollout.get("current_template_hash")
    if not current_hash:
        return None, None, (
            "the deployment's template matches no ReplicaSet yet (a rollout is "
            "still being created), so there is no settled revision to roll back from"
        )
    revisions = [r for r in rollout.get("revisions") or [] if isinstance(r, dict)]
    current = next((r for r in revisions if r.get("template_hash") == current_hash), None)
    if current is None:
        return None, None, f"the current template {current_hash} is missing from the history read"
    fingerprint = current.get("fingerprint")
    first_seen = [_created(r) for r in revisions if r.get("fingerprint") == fingerprint]
    if not first_seen or any(when is None for when in first_seen):
        return None, current, (
            "the history read does not date every ReplicaSet running the current "
            "software, so which revisions predate it cannot be told"
        )
    introduced = min(first_seen)
    for revision in revisions:
        template_hash = str(revision.get("template_hash") or "")
        created = _created(revision)
        if (
            template_hash != current_hash
            and TEMPLATE_HASH_RE.match(template_hash)
            and revision.get("fingerprint") != fingerprint
            and created is not None
            and created < introduced
        ):
            return revision, current, ""
    return None, current, (
        "no revision that predates the current software runs anything different "
        "(revisions that differ only by a restart run the same software, and a "
        "newer revision would be a roll-forward, not a rollback)"
    )


def _payload(response: Any) -> Optional[Dict[str, Any]]:
    from .executor import _structured_payload

    return _structured_payload(response)


async def _read_rollout(
    tool_caller: Callable[[str, Dict[str, Any]], Any], name: str, namespace: str
) -> Tuple[Optional[Dict[str, Any]], str]:
    try:
        response = await tool_caller("get_rollout_state", {"name": name, "namespace": namespace})
    except Exception as exc:
        return None, f"the executor's rollout history read failed: {exc}"
    payload = _payload(response)
    if not payload or str(payload.get("status") or "").upper() != "OK":
        detail = json.dumps(payload, sort_keys=True, default=str) if payload else str(response)
        return None, f"the executor's rollout history read did not succeed: {detail[:300]}"
    return payload, ""


async def bind_rollbacks(
    state: Any,
    context: Any = None,
    *,
    tool_caller: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
) -> Dict[str, Any]:
    """Bind every rollback in the plan, or record why it could not be bound.

    Returns the graph update: a new ``remediation_plan`` whose rollbacks carry
    ``parameters.rollback_binding``, and ``metadata.rollback_bindings`` with the
    history each one was read from. Any binding the planner wrote itself is
    replaced — the target comes from the cluster, never from the model.
    """
    from .executor import build_executor_tool_caller, live_resource_name

    plan = _get(state, "remediation_plan")
    actions = list(_get(plan, "actions", []) or [])
    if not any(str(_get(a, "action_type", "")).lower() == "rollback" for a in actions):
        return {}

    metadata = dict(_get(state, "metadata", {}) or {})
    labels = _get(_get(state, "alert_context"), "labels", {}) or {}
    canonical = str(_get(labels, "service") or _get(labels, "app") or "").strip()

    def resource(action: Any) -> str:
        return live_resource_name(canonical or _get(action, "target", ""))

    caller_error = ""
    if tool_caller is None:
        try:
            tool_caller = await build_executor_tool_caller(context)
        except Exception as exc:
            caller_error = f"the executor could not be reached to read rollout history: {exc}"

    evidence: List[Dict[str, Any]] = []
    new_actions: List[Any] = []
    for index, action in enumerate(actions):
        if str(_get(action, "action_type", "")).lower() != "rollback":
            new_actions.append(action)
            continue
        params = dict(_get(action, "parameters", {}) or {})
        params.pop(ROLLBACK_BINDING_KEY, None)
        name = resource(action)
        namespace = str(
            params.get("namespace")
            or metadata.get("cluster_namespace")
            or getattr(context, "namespace", None)
            or ""
        ).strip()
        record: Dict[str, Any] = {"action_index": index, "deployment": name, "namespace": namespace}

        earlier = next(
            (
                str(_get(a, "action_type", "")).lower()
                for a in actions[:index]
                if str(_get(a, "action_type", "")).lower() in _TEMPLATE_WRITES
                and resource(a) == name
            ),
            None,
        )
        rollout, target, current, why = None, None, None, ""
        if not name:
            why = "the rollback names no deployment"
        elif not namespace:
            why = "no namespace is known for the deployment"
        elif earlier:
            why = (
                f"an earlier action in this plan ({earlier}) rewrites "
                f"deployment/{name}'s template first, so the template this "
                "rollback would be bound to will be gone when it runs; roll back "
                "first, or plan the rollback on its own"
            )
        elif caller_error:
            why = caller_error
        else:
            rollout, why = await _read_rollout(tool_caller, name, namespace)
            if rollout is not None:
                target, current, why = select_rollback_target(rollout)
                record["rollout_state"] = rollout

        if target is not None and current is not None:
            params[ROLLBACK_BINDING_KEY] = {
                "deployment": name,
                "namespace": namespace,
                "to_template_hash": target["template_hash"],
                "to_revision": target.get("revision"),
                "to_images": dict(target.get("images") or {}),
                "expected_template_hash": current["template_hash"],
                "expected_revision": current.get("revision"),
                "expected_images": dict(current.get("images") or {}),
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }
            # The binding names a namespace, and the gateway holds the action
            # to it; pin it here so the two cannot disagree later.
            params["namespace"] = namespace
            record["bound"] = True
            logger.info(
                "Rollback %d bound: deployment/%s -n %s, %s -> %s",
                index, name, namespace, current["template_hash"], target["template_hash"],
            )
        else:
            params[ROLLBACK_BINDING_KEY] = {UNBOUND_REASON_KEY: why}
            record.update(bound=False, reason=why)
            logger.warning("Rollback %d left unbound: %s", index, why)
        evidence.append(record)
        new_actions.append(_replace(action, parameters=params))

    metadata["rollback_bindings"] = evidence
    return {"remediation_plan": _replace(plan, actions=new_actions), "metadata": metadata}
