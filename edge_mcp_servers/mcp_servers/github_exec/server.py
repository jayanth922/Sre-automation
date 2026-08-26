#!/usr/bin/env python3
"""
GitHub-exec MCP server — code-change remediation (the "hands" for code fixes).

The write counterpart to the read-only github MCP server. It executes the one
code-change fix an SRE agent legitimately needs: **open a revert PR** for a bad
deploy (plus PR comments). This is what makes "LLM-suggested code change,
executed, then verified" real — the executor routes a `revert_commit` action
here.

Safety, same model as the k8s executor:
- **Dry-run by default** — returns the exact operation it *would* perform,
  performs nothing.
- **Guardrails** (`guardrails.py`) — allow-list of actions + repos; a revert PR
  is inherently safe (it proposes an undo a human/CI still merges).
- Uses the `gh` CLI for the live path (auth via `GITHUB_TOKEN`).

Full auto-revert of a merge commit is multi-step; the live path opens the revert
PR via `gh` where supported and otherwise returns the precise manual/CI steps,
rather than pretend to force a change. Published on host port 4006.
"""

import json
import logging
import os
import subprocess
from typing import Optional

from mcp.server.fastmcp import FastMCP

from guardrails import allowed_repos, guardrail_check

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

REPO = os.getenv("GITHUB_REPO", "")

port = int(os.getenv("HTTP_PORT", "3000"))
host = os.getenv("HOST", "0.0.0.0")
mcp = FastMCP("github-exec-mcp-server", host=host, port=port)


def _refused(action: str, reason: str) -> str:
    return json.dumps({"tool": action, "repo": REPO, "status": "REFUSED", "reason": reason}, indent=2)


def _gh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)


@mcp.tool()
async def github_exec_health() -> str:
    """Report connectivity and the operator-configured safety envelope."""
    gh_ok = False
    try:
        gh_ok = _gh(["--version"]).returncode == 0
    except Exception:
        gh_ok = False
    return json.dumps({"status": "healthy" if gh_ok else "degraded", "gh_cli": gh_ok,
                       "repo": REPO, "allowed_repos": sorted(allowed_repos())}, indent=2)


@mcp.tool()
async def create_revert_pr(identifier: str, dry_run: bool = True) -> str:
    """Open a PR that reverts a bad commit/PR. `identifier` = commit SHA or PR number."""
    allowed, reason = guardrail_check("create_revert_pr", REPO, {"identifier": identifier})
    if not allowed:
        return _refused("create_revert_pr", reason)

    plan = (
        f"revert {identifier} in {REPO}: create branch revert-{identifier}, "
        f"apply the inverse diff, open a PR titled 'Revert {identifier}'"
    )
    if dry_run:
        return json.dumps({"tool": "create_revert_pr", "repo": REPO, "identifier": identifier,
                           "dry_run": True, "applied": False, "plan": plan, "status": "DRY_RUN"}, indent=2)

    # Live: best-effort via gh. GitHub has no single-call revert; if the org has a
    # revert workflow/label, trigger it; otherwise return the precise steps.
    try:
        label = os.getenv("GITHUB_EXEC_REVERT_LABEL")
        if label and str(identifier).isdigit():
            proc = _gh(["pr", "edit", str(identifier), "--add-label", label, "-R", REPO])
            ok = proc.returncode == 0
            return json.dumps({"tool": "create_revert_pr", "repo": REPO, "identifier": identifier,
                               "dry_run": False, "applied": ok, "status": "REVERT_REQUESTED" if ok else "ERROR",
                               "detail": (proc.stdout + proc.stderr).strip(),
                               "note": f"labelled PR #{identifier} '{label}' to trigger the revert workflow"}, indent=2)
        return json.dumps({"tool": "create_revert_pr", "repo": REPO, "identifier": identifier,
                           "dry_run": False, "applied": False, "status": "MANUAL_REQUIRED",
                           "plan": plan, "note": "set GITHUB_EXEC_REVERT_LABEL to auto-trigger a revert workflow"}, indent=2)
    except Exception as e:
        return json.dumps({"tool": "create_revert_pr", "status": "ERROR", "error": str(e)}, indent=2)


@mcp.tool()
async def comment_on_pr(pr_number: int, body: str, dry_run: bool = True) -> str:
    """Post a comment on a PR (e.g. the agent's revert rationale)."""
    allowed, reason = guardrail_check("comment_on_pr", REPO, {"pr_number": pr_number})
    if not allowed:
        return _refused("comment_on_pr", reason)
    if dry_run:
        return json.dumps({"tool": "comment_on_pr", "repo": REPO, "pr_number": pr_number,
                           "dry_run": True, "applied": False, "body": body, "status": "DRY_RUN"}, indent=2)
    try:
        proc = _gh(["pr", "comment", str(pr_number), "-R", REPO, "--body", body])
        ok = proc.returncode == 0
        return json.dumps({"tool": "comment_on_pr", "repo": REPO, "pr_number": pr_number,
                           "dry_run": False, "applied": ok, "status": "OK" if ok else "ERROR",
                           "detail": (proc.stdout + proc.stderr).strip()}, indent=2)
    except Exception as e:
        return json.dumps({"tool": "comment_on_pr", "status": "ERROR", "error": str(e)}, indent=2)


if __name__ == "__main__":
    logger.info("Starting GitHub-exec MCP server...")
    from mcp_auth import run_authenticated_sse
    run_authenticated_sse(mcp, host=host, port=port)
