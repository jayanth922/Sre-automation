"""Runbook catalog — lists the markdown runbook corpus with parsed frontmatter.

Reads the same runbook documents the runbooks MCP server indexes, so the
console shows the real source-of-truth catalog (id, title, service, type).
"""
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend import crud, database, models
from sre_agent.api.v1.auth_deps import get_current_user_and_org

router = APIRouter(prefix="/clusters", tags=["runbooks"])

_CANDIDATE_DIRS = [
    os.getenv("RUNBOOKS_DIR", ""),
    "/app/runbooks",
    str(Path(__file__).resolve().parents[2] / "edge_mcp_servers" / "mcp_servers" / "runbooks_local" / "runbooks"),
]


def _runbooks_dir() -> Path | None:
    for d in _CANDIDATE_DIRS:
        if d and Path(d).is_dir():
            return Path(d)
    return None


def _parse_frontmatter(text: str) -> Dict[str, str]:
    meta: Dict[str, str] = {}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return meta
    for line in m.group(1).splitlines():
        if ":" in line and not line.strip().startswith("-"):
            key, _, val = line.partition(":")
            v = val.strip().strip('"').strip("'")
            if v:
                meta[key.strip().lower()] = v
    return meta


@router.get("/{cluster_id}/runbooks")
async def list_runbooks(
    cluster_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
) -> List[Dict[str, Any]]:
    """List runbook documents from the local corpus."""
    cluster = await crud.get_cluster_by_id(db, cluster_id)
    if not cluster or cluster.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Cluster not found")

    root = _runbooks_dir()
    if not root:
        return []

    out: List[Dict[str, Any]] = []
    for path in sorted(root.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        try:
            meta = _parse_frontmatter(path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        out.append(
            {
                "id": meta.get("runbook_id") or path.stem,
                "title": meta.get("title") or path.stem,
                "service": meta.get("service") or "—",
                "incident_type": meta.get("incident_type") or "—",
                "severity": meta.get("severity") or "—",
                "path": path.name,
            }
        )
    return out


@router.get("/{cluster_id}/runbooks/{runbook_id}")
async def get_runbook(
    cluster_id: uuid.UUID,
    runbook_id: str,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
) -> Dict[str, Any]:
    """Return a single runbook's metadata + full markdown body."""
    cluster = await crud.get_cluster_by_id(db, cluster_id)
    if not cluster or cluster.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Cluster not found")

    root = _runbooks_dir()
    if not root:
        raise HTTPException(status_code=404, detail="No runbook corpus available")

    for path in root.glob("*.md"):
        if path.name.lower() == "readme.md":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        meta = _parse_frontmatter(text)
        rid = meta.get("runbook_id") or path.stem
        if rid == runbook_id or path.stem == runbook_id:
            body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.DOTALL)
            return {
                "id": rid,
                "title": meta.get("title") or path.stem,
                "service": meta.get("service") or "—",
                "incident_type": meta.get("incident_type") or "—",
                "severity": meta.get("severity") or "—",
                "path": path.name,
                "content": body.strip(),
            }
    raise HTTPException(status_code=404, detail="Runbook not found")
