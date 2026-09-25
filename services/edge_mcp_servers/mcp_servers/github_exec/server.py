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

Live outcomes are typed and truthful:
- ``CREATED`` — a revert PR was actually opened (includes ``pr_url``).
- ``MANUAL_REQUIRED`` — no PR was created; operator steps are returned.
- ``REFUSED`` / ``ERROR`` — guardrail or tooling failure.
- Optional label workflows return ``WORKFLOW_TRIGGERED``, never ``CREATED``.

Published on host port 4006.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from typing import Optional

from mcp.server.fastmcp import FastMCP

from guardrails import allowed_repos, guardrail_check

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

REPO = os.getenv("GITHUB_REPO", "")

port = int(os.getenv("HTTP_PORT", "3000"))
host = os.getenv("HOST", "0.0.0.0")
mcp = FastMCP("github-exec-mcp-server", host=host, port=port)

_PR_URL_RE = re.compile(r"https://github\.com/[^\s]+/pull/\d+")


def _refused(action: str, reason: str) -> str:
    return json.dumps(
        {"tool": action, "repo": REPO, "status": "REFUSED", "applied": False, "reason": reason},
        separators=(",", ":"),
    )


def _gh(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)


def _git(repo_dir: str, args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo_dir, *args], capture_output=True, text=True, timeout=timeout)


def _extract_pr_url(text: str) -> Optional[str]:
    match = _PR_URL_RE.search(text or "")
    return match.group(0) if match else None


@mcp.tool()
async def github_exec_health() -> str:
    """Report connectivity and the operator-configured safety envelope."""
    gh_ok = False
    try:
        gh_ok = _gh(["--version"]).returncode == 0
    except Exception:
        gh_ok = False
    return json.dumps(
        {
            "status": "healthy" if gh_ok else "degraded",
            "gh_cli": gh_ok,
            "repo": REPO,
            "allowed_repos": sorted(allowed_repos()),
        },
        separators=(",", ":"),
    )


@mcp.tool()
async def create_revert_pr(identifier: str, dry_run: bool = True) -> str:
    """Open a PR that reverts a bad commit/PR. ``identifier`` = commit SHA or PR number.

    Live success means a revert PR was created (``status=CREATED``, ``pr_url`` set).
    If GitHub cannot open one, returns ``MANUAL_REQUIRED`` or ``ERROR`` — never a
    success that only labelled a PR unless ``GITHUB_EXEC_REVERT_LABEL`` is set,
    which yields the distinct ``WORKFLOW_TRIGGERED`` status.
    """
    allowed, reason = guardrail_check("create_revert_pr", REPO, {"identifier": identifier})
    if not allowed:
        return _refused("create_revert_pr", reason)

    plan = (
        f"revert {identifier} in {REPO}: create branch revert-{identifier}, "
        f"apply the inverse diff, open a PR titled 'Revert {identifier}'"
    )
    if dry_run:
        return json.dumps(
            {
                "tool": "create_revert_pr",
                "repo": REPO,
                "identifier": identifier,
                "dry_run": True,
                "applied": False,
                "plan": plan,
                "status": "DRY_RUN",
            },
            separators=(",", ":"),
        )

    try:
        # Preferred path: gh can open a real revert PR for a merged PR number.
        if str(identifier).isdigit():
            proc = _gh(
                [
                    "pr",
                    "revert",
                    str(identifier),
                    "-R",
                    REPO,
                    "--title",
                    f"Revert #{identifier}",
                    "--body",
                    f"Automated revert of PR #{identifier} requested by Sentinel.",
                ]
            )
            detail = (proc.stdout + proc.stderr).strip()
            pr_url = _extract_pr_url(detail)
            if proc.returncode == 0 and pr_url:
                return json.dumps(
                    {
                        "tool": "create_revert_pr",
                        "repo": REPO,
                        "identifier": identifier,
                        "dry_run": False,
                        "applied": True,
                        "status": "CREATED",
                        "pr_url": pr_url,
                        "detail": detail,
                    },
                    separators=(",", ":"),
                )
            if proc.returncode == 0 and not pr_url:
                # Unexpected: command succeeded but no PR URL — do not claim CREATED.
                return json.dumps(
                    {
                        "tool": "create_revert_pr",
                        "repo": REPO,
                        "identifier": identifier,
                        "dry_run": False,
                        "applied": False,
                        "status": "MANUAL_REQUIRED",
                        "plan": plan,
                        "detail": detail,
                        "note": "gh pr revert returned success without a PR URL",
                    },
                    separators=(",", ":"),
                )

            # Optional operator workflow: label triggers an external revert action.
            # This is NOT create_revert_pr success — typed distinctly.
            label = os.getenv("GITHUB_EXEC_REVERT_LABEL")
            if label:
                label_proc = _gh(["pr", "edit", str(identifier), "--add-label", label, "-R", REPO])
                label_detail = (label_proc.stdout + label_proc.stderr).strip()
                if label_proc.returncode == 0:
                    return json.dumps(
                        {
                            "tool": "create_revert_pr",
                            "repo": REPO,
                            "identifier": identifier,
                            "dry_run": False,
                            "applied": False,
                            "status": "WORKFLOW_TRIGGERED",
                            "pr_url": None,
                            "detail": label_detail,
                            "note": (
                                f"labelled PR #{identifier} with '{label}' to trigger an "
                                "external revert workflow; no revert PR was created by this tool"
                            ),
                            "revert_error": detail,
                        },
                        separators=(",", ":"),
                    )

            return json.dumps(
                {
                    "tool": "create_revert_pr",
                    "repo": REPO,
                    "identifier": identifier,
                    "dry_run": False,
                    "applied": False,
                    "status": "MANUAL_REQUIRED",
                    "plan": plan,
                    "detail": detail,
                    "note": "gh pr revert failed; open a revert PR manually or fix repo permissions",
                },
                separators=(",", ":"),
            )

        # Commit SHA: gh has no single-shot "revert this commit as a PR". Be honest.
        return json.dumps(
            {
                "tool": "create_revert_pr",
                "repo": REPO,
                "identifier": identifier,
                "dry_run": False,
                "applied": False,
                "status": "MANUAL_REQUIRED",
                "plan": plan,
                "note": (
                    "commit SHA reverts require a local branch + PR; pass a merged PR "
                    "number for automated `gh pr revert`, or create the revert PR manually"
                ),
            },
            separators=(",", ":"),
        )
    except Exception as e:
        return json.dumps(
            {
                "tool": "create_revert_pr",
                "repo": REPO,
                "identifier": identifier,
                "dry_run": False,
                "applied": False,
                "status": "ERROR",
                "error": str(e),
            },
            separators=(",", ":"),
        )


@mcp.tool()
async def create_fix_pr(
    branch_name: str,
    patch: str,
    title: str,
    body: str,
    base_branch: str = "",
    dry_run: bool = True,
) -> str:
    """Open a PR carrying an arbitrary AI-generated, sandbox-verified patch.

    Phase 5's Approval Gate 2 (docs/ai/PHASE5_DETERMINISTIC_PIPELINE_PLAN.md):
    only called after ``CodeFixVerificationWorkflow`` returned RESOLVED and a
    human approved raising the PR. Clones the repo, applies ``patch`` on a new
    branch, pushes, and opens a PR for a human/CI to merge — never merges
    anything itself.

    Live success means a PR was actually opened (``status=CREATED``, ``pr_url``
    set). If the patch doesn't apply, or the branch can't be pushed, or ``gh``
    can't open the PR, this returns ``MANUAL_REQUIRED`` or ``ERROR`` — never a
    false ``CREATED``.
    """
    allowed, reason = guardrail_check(
        "create_fix_pr", REPO, {"branch_name": branch_name, "patch": patch}
    )
    if not allowed:
        return _refused("create_fix_pr", reason)

    plan = (
        f"clone {REPO}, branch '{branch_name}' off {base_branch or '<default>'}, "
        f"apply the provided patch, push, and open a PR titled '{title}'"
    )
    if dry_run:
        return json.dumps(
            {
                "tool": "create_fix_pr",
                "repo": REPO,
                "branch_name": branch_name,
                "dry_run": True,
                "applied": False,
                "plan": plan,
                "status": "DRY_RUN",
            },
            separators=(",", ":"),
        )

    tmpdir = tempfile.mkdtemp(prefix="sentinel-fix-")
    patch_file = os.path.join(tmpdir, ".sentinel-fix.patch")
    try:
        # `gh repo clone` authenticates the clone itself but leaves plain `git
        # push` afterward with no credentials. Registering gh's credential
        # helper here (idempotent, cheap) lets the later `git push` succeed.
        _gh(["auth", "setup-git"], timeout=30)

        clone_args = ["repo", "clone", REPO, tmpdir, "--", "--depth", "1"]
        if base_branch:
            clone_args = ["repo", "clone", REPO, tmpdir, "--", "--depth", "1", "--branch", base_branch]
        clone_proc = _gh(clone_args, timeout=120)
        if clone_proc.returncode != 0:
            return json.dumps(
                {
                    "tool": "create_fix_pr",
                    "repo": REPO,
                    "branch_name": branch_name,
                    "dry_run": False,
                    "applied": False,
                    "status": "ERROR",
                    "error": (clone_proc.stdout + clone_proc.stderr).strip(),
                },
                separators=(",", ":"),
            )

        checkout_proc = _git(tmpdir, ["checkout", "-b", branch_name])
        if checkout_proc.returncode != 0:
            return json.dumps(
                {
                    "tool": "create_fix_pr",
                    "repo": REPO,
                    "branch_name": branch_name,
                    "dry_run": False,
                    "applied": False,
                    "status": "ERROR",
                    "error": (checkout_proc.stdout + checkout_proc.stderr).strip(),
                },
                separators=(",", ":"),
            )

        with open(patch_file, "w") as f:
            f.write(patch)

        apply_proc = _git(tmpdir, ["apply", "--index", patch_file])
        if apply_proc.returncode != 0:
            return json.dumps(
                {
                    "tool": "create_fix_pr",
                    "repo": REPO,
                    "branch_name": branch_name,
                    "dry_run": False,
                    "applied": False,
                    "status": "MANUAL_REQUIRED",
                    "plan": plan,
                    "detail": (apply_proc.stdout + apply_proc.stderr).strip(),
                    "note": "patch did not apply cleanly against the current default branch",
                },
                separators=(",", ":"),
            )

        commit_proc = _git(
            tmpdir,
            [
                "-c", "user.email=sentinel-bot@sentinel.local",
                "-c", "user.name=Sentinel Bot",
                "commit", "-m", title,
            ],
        )
        if commit_proc.returncode != 0:
            return json.dumps(
                {
                    "tool": "create_fix_pr",
                    "repo": REPO,
                    "branch_name": branch_name,
                    "dry_run": False,
                    "applied": False,
                    "status": "ERROR",
                    "error": (commit_proc.stdout + commit_proc.stderr).strip(),
                },
                separators=(",", ":"),
            )

        push_proc = _git(tmpdir, ["push", "-u", "origin", branch_name], timeout=120)
        if push_proc.returncode != 0:
            return json.dumps(
                {
                    "tool": "create_fix_pr",
                    "repo": REPO,
                    "branch_name": branch_name,
                    "dry_run": False,
                    "applied": False,
                    "status": "MANUAL_REQUIRED",
                    "plan": plan,
                    "detail": (push_proc.stdout + push_proc.stderr).strip(),
                    "note": "patch applied and committed locally, but the branch could not be pushed",
                },
                separators=(",", ":"),
            )

        pr_args = ["pr", "create", "-R", REPO, "--head", branch_name, "--title", title, "--body", body]
        if base_branch:
            pr_args += ["--base", base_branch]
        pr_proc = _gh(pr_args)
        pr_detail = (pr_proc.stdout + pr_proc.stderr).strip()
        pr_url = _extract_pr_url(pr_detail)
        if pr_proc.returncode == 0 and pr_url:
            return json.dumps(
                {
                    "tool": "create_fix_pr",
                    "repo": REPO,
                    "branch_name": branch_name,
                    "dry_run": False,
                    "applied": True,
                    "status": "CREATED",
                    "pr_url": pr_url,
                    "detail": pr_detail,
                },
                separators=(",", ":"),
            )

        return json.dumps(
            {
                "tool": "create_fix_pr",
                "repo": REPO,
                "branch_name": branch_name,
                "dry_run": False,
                "applied": False,
                "status": "MANUAL_REQUIRED",
                "plan": plan,
                "detail": pr_detail,
                "note": (
                    f"branch '{branch_name}' was pushed but `gh pr create` did not return a PR URL; "
                    "open the PR manually from that branch"
                ),
            },
            separators=(",", ":"),
        )
    except subprocess.TimeoutExpired as e:
        return json.dumps(
            {
                "tool": "create_fix_pr",
                "repo": REPO,
                "branch_name": branch_name,
                "dry_run": False,
                "applied": False,
                "status": "ERROR",
                "error": f"timed out: {e}",
            },
            separators=(",", ":"),
        )
    except Exception as e:
        return json.dumps(
            {
                "tool": "create_fix_pr",
                "repo": REPO,
                "branch_name": branch_name,
                "dry_run": False,
                "applied": False,
                "status": "ERROR",
                "error": str(e),
            },
            separators=(",", ":"),
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@mcp.tool()
async def comment_on_pr(pr_number: int, body: str, dry_run: bool = True) -> str:
    """Post a comment on a PR (e.g. the agent's revert rationale)."""
    allowed, reason = guardrail_check("comment_on_pr", REPO, {"pr_number": pr_number})
    if not allowed:
        return _refused("comment_on_pr", reason)
    if dry_run:
        return json.dumps(
            {
                "tool": "comment_on_pr",
                "repo": REPO,
                "pr_number": pr_number,
                "dry_run": True,
                "applied": False,
                "body": body,
                "status": "DRY_RUN",
            },
            separators=(",", ":"),
        )
    try:
        proc = _gh(["pr", "comment", str(pr_number), "-R", REPO, "--body", body])
        ok = proc.returncode == 0
        return json.dumps(
            {
                "tool": "comment_on_pr",
                "repo": REPO,
                "pr_number": pr_number,
                "dry_run": False,
                "applied": ok,
                "status": "OK" if ok else "ERROR",
                "detail": (proc.stdout + proc.stderr).strip(),
            },
            separators=(",", ":"),
        )
    except Exception as e:
        return json.dumps(
            {
                "tool": "comment_on_pr",
                "status": "ERROR",
                "applied": False,
                "error": str(e),
            },
            separators=(",", ":"),
        )


if __name__ == "__main__":
    logger.info("Starting GitHub-exec MCP server...")
    from mcp_auth import run_authenticated_sse

    run_authenticated_sse(mcp, host=host, port=port)
