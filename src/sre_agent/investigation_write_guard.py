#!/usr/bin/env python3
"""Investigating specialists may look at the world. They may not change it.

Sentinel has exactly one sanctioned way to change anything: the planner
proposes an action, `policy_gate.decide` classifies it, a human replies
`approve fix` in Slack, and only then does `executor.py` dispatch it — through
a client it builds itself (`build_executor_tool_caller`,
`build_github_exec_tool_caller`). That whole path exists to put a person
between a model and production.

The investigation graph is a different path. Its specialists get their tools
from `wrap_all_tools_with_retry`, and which tools they get is decided by a
YAML list. `github_agent` carried `create_revert_pr`, `comment_on_pr` and
`revert_pr`, and `github_exec/server.py` signs them `(identifier, dry_run =
True)` — a default, not a constraint, so the *model* chose whether the call
was real. Nothing downstream would have stopped it: `guardrail_check`
validates the repo and the argument shape, never whether anyone approved.
A diagnosing agent could have opened a revert PR against the live repository
mid-investigation, and the first anyone would know is the PR.

The YAML is fixed. This module is why the YAML is not the only thing standing
there — a tool list is one line away from growing a write back, and a config
file cannot enforce anything. The two together are the fix.

The forbidden set is derived from the executor's own dispatch maps rather
than hand-listed, so a new remediation tool is covered the day it is added.
The rule is literally "if the approved path can call it to change something,
the unapproved path cannot call it at all."
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Set

logger = logging.getLogger(__name__)


class ToolNotAuthorizedError(PermissionError):
    """A specialist tried to call a tool only approved remediation may call.

    Not a tool failure — the tool worked fine and was never reached. Audited
    as REFUSED rather than FAILURE so the distinction survives to the log an
    operator reads.
    """

    def __init__(self, tool_name: str):
        super().__init__(
            f"{tool_name} changes systems outside this platform and is not "
            f"available during investigation. Nothing was called. Diagnose "
            f"with read-only tools and propose the change as a remediation "
            f"action — a human approves it in Slack before it runs."
        )
        self.tool_name = tool_name


@functools.lru_cache(maxsize=1)
def remediation_only_tools() -> frozenset:
    """Tools that may only be reached through the approved remediation path.

    Derived from `executor.py`'s dispatch maps. Read-only members are excluded
    deliberately: nothing about looking at a deployment config or a sandbox's
    logs is unsafe, and `policy_gate.decide` already lets those run without
    approval, so forbidding them here would contradict the gate.
    """
    from .executor import (
        EXECUTOR_TOOL_MAP,
        GITHUB_EXEC_TOOL_MAP,
        NON_MUTATING_ACTIONS,
        SANDBOX_TOOL_MAP,
    )

    forbidden: Set[str] = {
        tool
        for action, tool in EXECUTOR_TOOL_MAP.items()
        if action not in NON_MUTATING_ACTIONS
    }
    # `patch`/`config_change` resolve to one of two tools at dispatch time;
    # the map only names the first. See `live_tool_for_action`.
    forbidden.add("patch_deployment_env")
    # Every github-exec tool writes to the repository, comment_on_pr included:
    # a comment posted under Sentinel's identity is a public claim about an
    # incident, and it is not the investigator's to make.
    forbidden.update(GITHUB_EXEC_TOOL_MAP.values())
    forbidden.add("create_fix_pr")
    # Provision and teardown cost real cluster resources; status and logs read.
    forbidden.update(
        tool for action, tool in SANDBOX_TOOL_MAP.items()
        if action in {"provision", "teardown"}
    )
    return frozenset(forbidden)


def is_remediation_only(tool_name: str) -> bool:
    return tool_name in remediation_only_tools()


def wrap_tool_with_write_guard(tool: Any) -> Any:
    """Refuse, before calling anything, if this tool may only run once approved.

    Sits inside the audit wrapper and outside the circuit breaker: the refusal
    must be recorded, and there is nothing to retry or to trip a breaker over
    — the tool was never contacted.
    """
    tool_name = getattr(tool, "name", "unknown_tool")
    if not is_remediation_only(tool_name):
        return tool

    def _refuse():
        logger.warning(
            "Refused %s during investigation: remediation-only tool reached a "
            "specialist. Check agent_config.yaml.",
            tool_name,
        )
        raise ToolNotAuthorizedError(tool_name)

    original_invoke = getattr(tool, "invoke", None)
    original_ainvoke = getattr(tool, "ainvoke", None)

    if original_invoke:
        @functools.wraps(original_invoke)
        def guarded_invoke(*args, **kwargs) -> Any:
            _refuse()
        object.__setattr__(tool, "invoke", guarded_invoke)

    if original_ainvoke:
        @functools.wraps(original_ainvoke)
        async def guarded_ainvoke(*args, **kwargs) -> Any:
            _refuse()
        object.__setattr__(tool, "ainvoke", guarded_ainvoke)

    logger.warning(
        "Tool %s is remediation-only but was handed to an investigating "
        "specialist; every call will be refused.",
        tool_name,
    )
    return tool
