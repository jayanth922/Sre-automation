"""Runbook catalog — reads each cluster's Notion-hosted runbooks.

Notion is the only runbook source: there is no local corpus fallback. A
cluster without Notion credentials configured has no runbooks to list.
"""
import logging
import uuid
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend import crud, database, models
from sre_agent.api.v1.auth_deps import get_current_user_and_org

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/clusters",
    tags=["runbooks"],
    dependencies=[Depends(get_current_user_and_org)],
)


@router.get("/{cluster_id}/runbooks")
async def list_runbooks(
    cluster_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
) -> List[Dict[str, Any]]:
    """List runbook documents from the cluster's Notion database."""
    cluster = await crud.get_cluster_by_id(db, cluster_id)
    if not cluster or cluster.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Cluster not found")

    if not (cluster.notion_api_key and cluster.notion_database_id):
        return []

    from sre_agent.notion_runbooks import list_notion_runbooks

    try:
        return await list_notion_runbooks(cluster.notion_api_key, cluster.notion_database_id)
    except Exception as e:
        logger.warning(f"Notion runbooks fetch failed: {e}")
        raise HTTPException(status_code=502, detail="Failed to fetch runbooks from Notion")


@router.get("/{cluster_id}/runbooks/{runbook_id}")
async def get_runbook(
    cluster_id: uuid.UUID,
    runbook_id: str,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
) -> Dict[str, Any]:
    """Return a single runbook's metadata + full markdown body (runbook_id is the Notion page id)."""
    cluster = await crud.get_cluster_by_id(db, cluster_id)
    if not cluster or cluster.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Cluster not found")

    if not (cluster.notion_api_key and cluster.notion_database_id):
        raise HTTPException(status_code=404, detail="No Notion runbook database configured for this cluster")

    from sre_agent.notion_runbooks import get_notion_runbook

    try:
        return await get_notion_runbook(cluster.notion_api_key, runbook_id)
    except Exception as e:
        logger.warning(f"Notion runbook fetch failed: {e}")
        raise HTTPException(status_code=404, detail="Runbook not found in Notion")
