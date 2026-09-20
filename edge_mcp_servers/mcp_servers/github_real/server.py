#!/usr/bin/env python3
"""
Real GitHub MCP Server

This MCP server directly uses the PyGithub library to interact with
GitHub repositories instead of calling mock APIs. It provides production-ready
GitHub operations through the Model Context Protocol.
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

from github import Github
from github.GithubException import GithubException
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from payload import (
    MAX_COMMITS_SCANNED,
    MAX_FILES_SCANNED,
    parse_iso,
    shape_commit,
    shape_commit_summary,
    shape_pull_request,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Initialize GitHub client
github_client = None
github_repo = None


def initialize_github_client():
    """Initialize GitHub client with token and repository."""
    global github_client, github_repo

    github_token = os.getenv("GITHUB_TOKEN")
    github_repo_name = os.getenv("GITHUB_REPO")  # Format: "owner/repo"

    if not github_token:
        logger.warning("⚠️ GITHUB_TOKEN not set, server will not function")
        return

    if not github_repo_name:
        logger.warning("⚠️ GITHUB_REPO not set, server will not function")
        return

    try:
        github_client = Github(github_token)
        # Test connection
        user = github_client.get_user()
        logger.info(f"✅ Connected to GitHub as {user.login}")

        # Get repository
        github_repo = github_client.get_repo(github_repo_name)
        logger.info(f"✅ Repository: {github_repo.full_name}")

    except GithubException as e:
        logger.error(f"❌ GitHub API error: {e}")
        raise
    except Exception as e:
        logger.error(f"❌ Failed to initialize GitHub client: {e}")
        raise


# Initialize on import
try:
    initialize_github_client()
except Exception as e:
    logger.warning(f"⚠️ GitHub client initialization failed: {e}")
    logger.warning("⚠️ Server will start but tools will fail until GITHUB_TOKEN and GITHUB_REPO are set")

# Bounded cache of relayed (token, repo) -> Repository, so a multi-tenant
# control plane relaying different clusters' credentials across requests
# doesn't force a fresh GitHub API lookup on every tool call.
_relay_repo_cache: Dict[tuple, Any] = {}
_RELAY_REPO_CACHE_MAX = 8


def _active_repo():
    """The repository to act against for the in-flight request.

    Prefers a per-request relayed credential (one control plane managing
    many Cluster rows) over this process's static single-tenant
    GITHUB_TOKEN/GITHUB_REPO configuration, which remains the fallback for
    a self-hosted deployment that never relays per-cluster credentials.
    """
    try:
        from relay_credentials import get_relay_credential
    except ImportError:
        return github_repo

    token = get_relay_credential("github_token")
    repo_name = get_relay_credential("github_repo")
    if not token or not repo_name:
        return github_repo

    cache_key = (token, repo_name)
    cached = _relay_repo_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        repo = Github(token).get_repo(repo_name)
    except Exception as e:
        logger.warning(f"relay: failed to resolve relayed repository {repo_name}: {e}")
        return github_repo

    if len(_relay_repo_cache) >= _RELAY_REPO_CACHE_MAX:
        _relay_repo_cache.pop(next(iter(_relay_repo_cache)))
    _relay_repo_cache[cache_key] = repo
    return repo


# Create FastMCP server
port = int(os.getenv("HTTP_PORT", "3000"))
host = os.getenv("HOST", "0.0.0.0")

mcp = FastMCP("github-real-mcp-server", host=host, port=port)


# Tool parameter models
class ListCommitsParams(BaseModel):
    """Parameters for list_commits tool."""

    since: Optional[str] = Field(
        None, description="Only commits after this date (ISO 8601 format)"
    )
    until: Optional[str] = Field(
        None, description="Only commits before this date (ISO 8601 format)"
    )
    author: Optional[str] = Field(None, description="Filter by author email or username")
    path: Optional[str] = Field(None, description="Filter by file path")
    limit: int = Field(default=50, ge=1, le=100, description="Maximum number of commits")


class GetCommitParams(BaseModel):
    """Parameters for get_commit tool."""
    sha: str = Field(..., description="Commit SHA (full or partial)")


class ListPullRequestsParams(BaseModel):
    """Parameters for list_pull_requests tool."""
    state: Optional[str] = Field(
        "all", description="Filter by state: open, closed, or all"
    )
    author: Optional[str] = Field(None, description="Filter by author username")
    limit: int = Field(default=50, ge=1, le=100, description="Maximum number of PRs")


class GetPullRequestParams(BaseModel):
    """Parameters for get_pull_request tool."""
    pr_number: int = Field(..., description="Pull request number")


class ListRepositoryFilesParams(BaseModel):
    """Parameters for list_repository_files tool."""

    path: Optional[str] = Field(
        "", description="Repository path to list from, or empty string for repo root"
    )
    recursive: bool = Field(
        True, description="Whether to recursively traverse nested directories"
    )
    limit: int = Field(default=200, ge=1, le=1000, description="Maximum number of files to return")


class GetRepositoryFileParams(BaseModel):
    """Parameters for get_repository_file tool."""

    path: str = Field(..., description="Repository file path to read")
    max_chars: int = Field(
        default=20000, ge=1, le=100000, description="Maximum number of characters to return"
    )


# Implementation Helpers

async def handle_list_commits(params: ListCommitsParams) -> str:
    """List commits from repository."""
    logger.info(
        "Listing commits (since=%s until=%s path=%s limit=%s)",
        params.since,
        params.until,
        params.path,
        params.limit,
    )

    repo = _active_repo()
    if not repo:
        return "Error: GitHub client not initialized."

    loop = asyncio.get_event_loop()

    # `since`/`until`/`path` are server-side filters on GitHub's commits
    # endpoint. They used to be applied here instead: `repo.get_commits()` with
    # no arguments returns a lazy page over the repository's *entire* history,
    # and a non-matching commit hit `continue`, so a window with no commits in
    # it walked every commit in the repo — one API call per page — before
    # returning an empty list. Path filtering was a literal `pass`.
    try:
        since = parse_iso(params.since)
        until = parse_iso(params.until)
    except ValueError as exc:
        # Dropping an unparseable bound would silently restore the full-history
        # walk, which is the defect, so refuse instead.
        return f"Error listing commits: invalid since/until ({exc})"

    kwargs: Dict[str, Any] = {}
    if since:
        kwargs["since"] = since
    if until:
        kwargs["until"] = until
    if params.path:
        kwargs["path"] = params.path

    try:
        commits = await loop.run_in_executor(
            None, lambda: repo.get_commits(**kwargs)
        )

        results = []
        scanned = 0
        for commit in commits:
            if len(results) >= params.limit:
                break
            # `author` stays a client-side substring match — GitHub's
            # server-side `author` is exact, and narrowing that silently
            # would turn a partial name into an empty result. A filter that
            # matches nothing must not page through the window forever, so
            # the scan itself is bounded.
            scanned += 1
            if scanned > MAX_COMMITS_SCANNED:
                break

            if params.author and params.author.lower() not in commit.commit.author.email.lower():
                if params.author.lower() not in (commit.author.login.lower() if commit.author else ""):
                    continue

            results.append(
                shape_commit_summary(
                    {
                        "sha": commit.sha,
                        "message": commit.commit.message,
                        "author": {
                            "name": commit.commit.author.name,
                            "email": commit.commit.author.email,
                            "login": commit.author.login if commit.author else None,
                        },
                        "timestamp": commit.commit.author.date.isoformat(),
                        "url": commit.html_url,
                    }
                )
            )

        return json.dumps({"commits": results}, separators=(",", ":"))
    except Exception as e:
        logger.error(f"Error listing commits: {e}")
        return f"Error listing commits: {e}"


async def handle_get_commit(params: GetCommitParams) -> str:
    """Get commit details with diff."""
    logger.info(f"Getting commit: {params.sha}")

    repo = _active_repo()
    if not repo:
        return "Error: GitHub client not initialized."

    loop = asyncio.get_event_loop()
    try:
        commit = await loop.run_in_executor(None, repo.get_commit, params.sha)

        # The diff lives on the per-file entries, not on the commit.
        # `commit.patch` used to be read here behind a `hasattr` guard;
        # `github.Commit.Commit` has no such property and `GithubObject`
        # defines no `__getattr__`, so the guard always failed and this tool
        # returned `"diff":null` on every call while `list(commit.files)` —
        # the data that does hold the patches — was walked and discarded.
        def _scan_files():
            rows = []
            truncated = False
            for entry in commit.files:
                if len(rows) >= MAX_FILES_SCANNED:
                    truncated = True
                    break
                rows.append(
                    {
                        "filename": entry.filename,
                        "status": entry.status,
                        "additions": entry.additions,
                        "deletions": entry.deletions,
                        "changes": entry.changes,
                        "patch": entry.patch,
                    }
                )
            return rows, truncated

        files, scan_truncated = await loop.run_in_executor(None, _scan_files)

        result = shape_commit(
            sha=commit.sha,
            message=commit.commit.message,
            author={
                "name": commit.commit.author.name,
                "email": commit.commit.author.email,
                "login": commit.author.login if commit.author else None,
            },
            timestamp=commit.commit.author.date.isoformat(),
            url=commit.html_url,
            files=files,
            additions=commit.stats.additions,
            deletions=commit.stats.deletions,
            scan_truncated=scan_truncated,
        )

        return json.dumps(result, separators=(",", ":"))
    except Exception as e:
        logger.error(f"Error getting commit: {e}")
        return f"Error getting commit: {e}"


async def handle_list_pull_requests(params: ListPullRequestsParams) -> str:
    """List pull requests."""
    logger.info(f"Listing pull requests (state: {params.state}, limit: {params.limit})")

    repo = _active_repo()
    if not repo:
        return "Error: GitHub client not initialized."

    loop = asyncio.get_event_loop()
    try:
        prs = await loop.run_in_executor(None, repo.get_pulls, params.state)

        results = []
        count = 0
        for pr in prs:
            if count >= params.limit:
                break

            # Filter by author if specified
            if params.author and params.author.lower() not in pr.user.login.lower():
                continue

            pr_data = {
                "number": pr.number,
                "title": pr.title,
                "state": pr.state,
                "author": pr.user.login,
                "created_at": pr.created_at.isoformat(),
                "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
                "base_branch": pr.base.ref,
                "head_branch": pr.head.ref,
                "url": pr.html_url,
            }
            results.append(pr_data)
            count += 1

        return json.dumps({"pull_requests": results}, separators=(",", ":"))
    except Exception as e:
        logger.error(f"Error listing pull requests: {e}")
        return f"Error listing pull requests: {e}"


async def handle_get_pull_request(params: GetPullRequestParams) -> str:
    """Get pull request details."""
    logger.info(f"Getting pull request: #{params.pr_number}")

    repo = _active_repo()
    if not repo:
        return "Error: GitHub client not initialized."

    loop = asyncio.get_event_loop()
    try:
        pr = await loop.run_in_executor(None, repo.get_pull, params.pr_number)

        result = shape_pull_request(
            {
                "number": pr.number,
                "title": pr.title,
                "state": pr.state,
                "author": pr.user.login,
                "created_at": pr.created_at.isoformat(),
                "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
                "base_branch": pr.base.ref,
                "head_branch": pr.head.ref,
                "url": pr.html_url,
                "body": pr.body,
                "mergeable": pr.mergeable,
                "merged": pr.merged,
            }
        )

        return json.dumps(result, separators=(",", ":"))
    except Exception as e:
        logger.error(f"Error getting pull request: {e}")
        return f"Error getting pull request: {e}"


def _format_repo_file_entry(entry) -> Dict[str, Any]:
    """Format a GitHub content entry for JSON output."""
    return {
        "name": entry.name,
        "path": entry.path,
        "type": entry.type,
        "size": getattr(entry, "size", None),
        "sha": getattr(entry, "sha", None),
        "url": getattr(entry, "html_url", None),
    }


async def handle_list_repository_files(params: ListRepositoryFilesParams) -> str:
    """List files and directories in the configured repository."""
    logger.info(
        f"Listing repository files (path: {params.path!r}, recursive: {params.recursive}, limit: {params.limit})"
    )

    repo = _active_repo()
    if not repo:
        return "Error: GitHub client not initialized."

    loop = asyncio.get_event_loop()

    try:
        start_path = params.path.strip() or ""
        queue = [start_path]
        results: List[Dict[str, Any]] = []

        while queue and len(results) < params.limit:
            current_path = queue.pop(0)
            contents = await loop.run_in_executor(None, repo.get_contents, current_path)

            if not isinstance(contents, list):
                results.append(_format_repo_file_entry(contents))
                continue

            for entry in contents:
                if len(results) >= params.limit:
                    break

                formatted = _format_repo_file_entry(entry)
                results.append(formatted)

                if params.recursive and entry.type == "dir":
                    queue.append(entry.path)

        return json.dumps(
            {
                "repository": repo.full_name,
                "path": start_path,
                "recursive": params.recursive,
                "files": results,
                "count": len(results),
            },
            separators=(",", ":"),
        )
    except GithubException as e:
        logger.error(f"Error listing repository files: {e}")
        return f"Error listing repository files: {e}"
    except Exception as e:
        logger.error(f"Unexpected error listing repository files: {e}")
        return f"Error listing repository files: {e}"


async def handle_get_repository_file(params: GetRepositoryFileParams) -> str:
    """Read the contents of a single repository file."""
    logger.info(f"Reading repository file: {params.path}")

    repo = _active_repo()
    if not repo:
        return "Error: GitHub client not initialized."

    loop = asyncio.get_event_loop()

    try:
        content = await loop.run_in_executor(None, repo.get_contents, params.path)

        if isinstance(content, list):
            return json.dumps(
                {
                    "repository": repo.full_name,
                    "path": params.path,
                    "type": "dir",
                    "entries": [_format_repo_file_entry(entry) for entry in content],
                },
                separators=(",", ":"),
            )

        raw_content = content.decoded_content
        if isinstance(raw_content, bytes):
            text = raw_content.decode("utf-8", errors="replace")
        else:
            text = str(raw_content)

        truncated = False
        if len(text) > params.max_chars:
            text = text[: params.max_chars]
            truncated = True

        return json.dumps(
            {
                "repository": repo.full_name,
                "path": params.path,
                "sha": content.sha,
                "size": getattr(content, "size", None),
                "encoding": getattr(content, "encoding", None),
                "truncated": truncated,
                "content": text,
            },
            separators=(",", ":"),
        )
    except GithubException as e:
        logger.error(f"Error reading repository file: {e}")
        return f"Error reading repository file: {e}"
    except UnicodeDecodeError as e:
        logger.error(f"Error decoding repository file: {e}")
        return f"Error decoding repository file: {e}"
    except Exception as e:
        logger.error(f"Unexpected error reading repository file: {e}")
        return f"Error reading repository file: {e}"


# Tool wrappers

@mcp.tool()
async def list_commits(since: str = None, until: str = None, author: str = None, path: str = None, limit: int = 50) -> str:
    """List commits, newest first. `since`/`until` (ISO 8601) and `path` are
    applied by GitHub, so narrowing them costs nothing and widening them is
    what makes this call slow. Each entry carries sha, message (capped at
    2000 chars), author, timestamp and url — no diff; call get_commit for
    that."""
    return await handle_list_commits(
        ListCommitsParams(since=since, until=until, author=author, path=path, limit=limit)
    )

@mcp.tool()
async def get_commit(sha: str) -> str:
    """Get one commit with bounded changed-file evidence. Up to 50 files keep
    a row (filename, status, additions, deletions, changes); patch text is
    included largest-change-first within an 18,000-character encoded-payload
    target, at most 2,000 characters per file. `files_omitted`,
    `patch_chars_omitted` and `files_scan_truncated` report what was left out —
    omitted text is missing from this response, not from the commit."""
    return await handle_get_commit(GetCommitParams(sha=sha))

@mcp.tool()
async def list_pull_requests(state: str = "all", author: str = None, limit: int = 50) -> str:
    """List pull requests with optional filtering."""
    return await handle_list_pull_requests(
        ListPullRequestsParams(state=state, author=author, limit=limit)
    )

@mcp.tool()
async def get_pull_request(pr_number: int) -> str:
    """Get one pull request: number, title, state, author, branches, merge
    status and body (capped at 4000 chars, flagged with `body_truncated`)."""
    return await handle_get_pull_request(GetPullRequestParams(pr_number=pr_number))

@mcp.tool()
async def list_repository_files(path: str = "", recursive: bool = True, limit: int = 200) -> str:
    """List files and directories in the configured repository."""
    return await handle_list_repository_files(
        ListRepositoryFilesParams(path=path, recursive=recursive, limit=limit)
    )

@mcp.tool()
async def get_repository_file(path: str, max_chars: int = 20000) -> str:
    """Read the contents of a repository file."""
    return await handle_get_repository_file(GetRepositoryFileParams(path=path, max_chars=max_chars))


if __name__ == "__main__":
    logger.info("Starting FastMCP server execution...")
    from mcp_auth import run_authenticated_sse
    run_authenticated_sse(mcp, host=host, port=port)
