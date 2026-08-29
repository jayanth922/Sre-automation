#!/usr/bin/env python3
"""Single resolve path for the markdown runbook corpus.

The API catalog, runbook generator, and runbooks MCP server must all read/write
the same directory so auto-generated (and later reviewed) runbooks are
agent-retrievable. Prefer ``RUNBOOKS_DIR``; otherwise use the local MCP corpus.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_CORPUS = (
    _REPO_ROOT / "edge_mcp_servers" / "mcp_servers" / "runbooks_local" / "runbooks"
)
_CONTAINER_CORPUS = Path("/app/runbooks")


def resolve_runbooks_dir(*, create: bool = False) -> Optional[Path]:
    """Return the corpus directory, or None when listing and nothing exists.

    When ``create`` is True (generator write path), ensure the preferred
    directory exists and return it even if empty.
    """
    env = (os.getenv("RUNBOOKS_DIR") or "").strip()
    if env:
        path = Path(env).expanduser().resolve()
        if create:
            path.mkdir(parents=True, exist_ok=True)
            return path
        return path if path.is_dir() else None

    for candidate in (_DEFAULT_CORPUS, _CONTAINER_CORPUS):
        if candidate.is_dir():
            return candidate.resolve()

    if create:
        _DEFAULT_CORPUS.mkdir(parents=True, exist_ok=True)
        return _DEFAULT_CORPUS.resolve()
    return None


def default_runbooks_dir() -> Path:
    """Corpus path for writers (always returns a concrete directory)."""
    path = resolve_runbooks_dir(create=True)
    assert path is not None
    return path
