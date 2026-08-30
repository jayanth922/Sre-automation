"""Multi-tenant secure access: Slack OAuth install flow and GitHub App
installation linking (Phase 4).

Both flows follow the same shape: an authenticated "start" endpoint mints a
short-lived, signed state token (via ``backend.auth.create_access_token``)
that carries the org/cluster identity, and an unauthenticated "callback"
endpoint — hit directly by Slack's/GitHub's redirect, so it cannot carry a
bearer token — decodes that state token instead of relying on a session.
This needs no new server-side storage and works across multiple API workers.
"""
import logging
import uuid
from datetime import timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend import crud, database, models, schemas
from backend.auth import create_access_token, decode_access_token
from sre_agent.api.v1.auth_deps import get_current_user_and_org
from sre_agent.multitenant import github_app, slack_oauth

logger = logging.getLogger(__name__)

_STATE_TTL = timedelta(minutes=10)

router = APIRouter(tags=["multitenant"])


def _decode_state(state: str, *, purpose: str) -> Dict[str, Any]:
    payload = decode_access_token(state)
    if not payload or payload.get("purpose") != purpose:
        raise HTTPException(status_code=400, detail="Invalid or expired state")
    return payload


# ----------------------------------------------------------------------
# Slack OAuth ("Add to Slack")
# ----------------------------------------------------------------------

@router.get("/organizations/slack/install-url")
async def slack_install_url(
    redirect_uri: Optional[str] = Query(default=None),
    user: models.User = Depends(get_current_user_and_org),
) -> Dict[str, str]:
    if not slack_oauth.slack_oauth_configured():
        raise HTTPException(status_code=400, detail="Slack app is not configured on this deployment")
    state = create_access_token(
        {"purpose": "slack_oauth", "org_id": str(user.org_id)}, expires_delta=_STATE_TTL
    )
    return {"install_url": slack_oauth.build_install_url(state, redirect_uri=redirect_uri)}


@router.get("/organizations/slack/callback")
async def slack_oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
    redirect_uri: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(database.get_db),
) -> Dict[str, Any]:
    payload = _decode_state(state, purpose="slack_oauth")
    try:
        result = await slack_oauth.exchange_code_for_token(code, redirect_uri=redirect_uri)
    except slack_oauth.SlackOAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    org = await crud.set_org_slack_installation(
        db,
        uuid.UUID(payload["org_id"]),
        bot_token=result["bot_token"],
        team_id=result["team_id"],
    )
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    return {"status": "connected", "team_id": result["team_id"], "team_name": result.get("team_name")}


# ----------------------------------------------------------------------
# GitHub App installation linking
# ----------------------------------------------------------------------

@router.get("/clusters/{cluster_id}/github-app/install-url")
async def github_app_install_url(
    cluster_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
) -> Dict[str, str]:
    if not github_app.github_app_configured():
        raise HTTPException(status_code=400, detail="GitHub App is not configured on this deployment")
    cluster = await crud.get_cluster_by_id(db, cluster_id)
    if not cluster or cluster.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Cluster not found")
    state = create_access_token(
        {
            "purpose": "github_app_link",
            "org_id": str(user.org_id),
            "cluster_id": str(cluster_id),
        },
        expires_delta=_STATE_TTL,
    )
    try:
        return {"install_url": github_app.install_url(state)}
    except github_app.GitHubAppError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/clusters/github-app/callback")
async def github_app_callback(
    installation_id: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(database.get_db),
) -> Dict[str, Any]:
    payload = _decode_state(state, purpose="github_app_link")
    cluster_id = uuid.UUID(payload["cluster_id"])
    org_id = uuid.UUID(payload["org_id"])
    cluster = await crud.update_cluster(
        db,
        cluster_id,
        org_id,
        schemas.ClusterUpdate(github_app_installation_id=installation_id),
    )
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    return {"status": "connected", "cluster_id": str(cluster_id), "installation_id": installation_id}
