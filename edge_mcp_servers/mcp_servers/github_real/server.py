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
    logger.info(f"Listing commits (limit: {params.limit})")

    if not github_repo:
        return "Error: GitHub client not initialized."

    loop = asyncio.get_event_loop()

    try:
        # Get commits
        commits = await loop.run_in_executor(None, github_repo.get_commits)

        # Filter and format
        results = []
        count = 0
        for commit in commits:
            if count >= params.limit:
                break

            # Apply filters
            if params.since and commit.commit.author.date.isoformat() < params.since:
                continue
            if params.until and commit.commit.author.date.isoformat() > params.until:
                continue
            if params.author and params.author.lower() not in commit.commit.author.email.lower():
                if params.author.lower() not in (commit.author.login.lower() if commit.author else ""):
                    continue
            if params.path:
                 pass  # Skip path filtering for now

            commit_data = {
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
            results.append(commit_data)
            count += 1

        return json.dumps({"commits": results}, indent=2)
    except Exception as e:
        logger.error(f"Error listing commits: {e}")
        return f"Error listing commits: {e}"


async def handle_get_commit(params: GetCommitParams) -> str:
    """Get commit details with diff."""
    logger.info(f"Getting commit: {params.sha}")

    if not github_repo:
        return "Error: GitHub client not initialized."

    loop = asyncio.get_event_loop()
    try:
        commit = await loop.run_in_executor(None, github_repo.get_commit, params.sha)

        # Get diff (patch)
        patch = commit.patch if hasattr(commit, "patch") else None
        files = list(commit.files)

        result = {
            "sha": commit.sha,
            "message": commit.commit.message,
            "author": {
                "name": commit.commit.author.name,
                "email": commit.commit.author.email,
                "login": commit.author.login if commit.author else None,
            },
            "timestamp": commit.commit.author.date.isoformat(),
            "url": commit.html_url,
            "files_changed": len(files),
            "additions": commit.stats.additions,
            "deletions": commit.stats.deletions,
            "diff": patch,
        }

        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting commit: {e}")
        return f"Error getting commit: {e}"


async def handle_list_pull_requests(params: ListPullRequestsParams) -> str:
    """List pull requests."""
    logger.info(f"Listing pull requests (state: {params.state}, limit: {params.limit})")

    if not github_repo:
        return "Error: GitHub client not initialized."

    loop = asyncio.get_event_loop()
    try:
        prs = await loop.run_in_executor(
            None, github_repo.get_pulls, params.state if params.state != "all" else None
        )

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

        return json.dumps({"pull_requests": results}, indent=2)
    except Exception as e:
        logger.error(f"Error listing pull requests: {e}")
        return f"Error listing pull requests: {e}"


async def handle_get_pull_request(params: GetPullRequestParams) -> str:
    """Get pull request details."""
    logger.info(f"Getting pull request: #{params.pr_number}")

    if not github_repo:
        return "Error: GitHub client not initialized."

    loop = asyncio.get_event_loop()
    try:
        pr = await loop.run_in_executor(None, github_repo.get_pull, params.pr_number)

        result = {
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

        return json.dumps(result, indent=2)
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

    if not github_repo:
        return "Error: GitHub client not initialized."

    loop = asyncio.get_event_loop()

    try:
        start_path = params.path.strip() or ""
        queue = [start_path]
        results: List[Dict[str, Any]] = []

        while queue and len(results) < params.limit:
            current_path = queue.pop(0)
            contents = await loop.run_in_executor(None, github_repo.get_contents, current_path)

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
                "repository": github_repo.full_name,
                "path": start_path,
                "recursive": params.recursive,
                "files": results,
                "count": len(results),
            },
            indent=2,
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

    if not github_repo:
        return "Error: GitHub client not initialized."

    loop = asyncio.get_event_loop()

    try:
        content = await loop.run_in_executor(None, github_repo.get_contents, params.path)

        if isinstance(content, list):
            return json.dumps(
                {
                    "repository": github_repo.full_name,
                    "path": params.path,
                    "type": "dir",
                    "entries": [_format_repo_file_entry(entry) for entry in content],
                },
                indent=2,
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
                "repository": github_repo.full_name,
                "path": params.path,
                "sha": content.sha,
                "size": getattr(content, "size", None),
                "encoding": getattr(content, "encoding", None),
                "truncated": truncated,
                "content": text,
            },
            indent=2,
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
    """List commits from the repository with optional filtering."""
    return await handle_list_commits(
        ListCommitsParams(since=since, until=until, author=author, path=path, limit=limit)
    )

@mcp.tool()
async def get_commit(sha: str) -> str:
    """Get detailed information about a specific commit including diff."""
    return await handle_get_commit(GetCommitParams(sha=sha))

@mcp.tool()
async def list_pull_requests(state: str = "all", author: str = None, limit: int = 50) -> str:
    """List pull requests with optional filtering."""
    return await handle_list_pull_requests(
        ListPullRequestsParams(state=state, author=author, limit=limit)
    )

@mcp.tool()
async def get_pull_request(pr_number: int) -> str:
    """Get detailed information about a specific pull request."""
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
    mcp.run(transport="sse")
