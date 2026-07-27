#!/usr/bin/env python3
"""
GitHub-exec edge guardrails — defense in depth for code-change remediation.

The read-only GitHub MCP server exposes evidence; this WRITE server executes
code-change fixes. Because writes to a code repo are high-blast-radius, the
allow-list here is deliberately tiny: the agent may only **open a revert PR** or
**comment** — never force-push, delete branches, close arbitrary PRs, or push to
a protected branch directly. Operator-owned env vars, independent of the LLM.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Tuple

# The only code-change actions the agent may perform. A revert PR is safe by
# construction: it proposes an undo that a human/CI still merges.
ALLOWED_ACTIONS = {"create_revert_pr", "comment_on_pr"}


def allowed_repos() -> set[str]:
    raw = os.getenv("GITHUB_EXEC_ALLOWED_REPOS", os.getenv("GITHUB_REPO", ""))
    return {r.strip() for r in raw.split(",") if r.strip()}


def guardrail_check(action: str, repo: str, params: Dict[str, Any] | None = None) -> Tuple[bool, str]:
    action = (action or "").lower()
    if action not in ALLOWED_ACTIONS:
        return False, f"action '{action}' not in the github-exec allow-list {sorted(ALLOWED_ACTIONS)}"

    allow = allowed_repos()
    if allow and repo not in allow:
        return False, f"repo '{repo}' not in the allow-list {sorted(allow)}"

    if action == "create_revert_pr":
        params = params or {}
        if not (params.get("identifier") or params.get("commit_sha") or params.get("pr_number")):
            return False, "create_revert_pr requires a commit_sha or pr_number to revert"

    return True, "ok"
