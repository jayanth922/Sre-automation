"""The one moment the platform has no users: claiming a fresh install.

A new deployment ships with no accounts and, since the keystore work, no
``.env`` either. The alternative to this router is seeding a default admin from
environment variables — a password identical on every install that nobody ever
rotates — which is exactly the configuration-by-env this platform is moving
away from. Instead the first person to reach the dashboard claims it: they
create the founding organisation and its admin, and the route then closes for
good. This is the GitLab/Grafana first-run pattern.

"Closed" is defined by a fact rather than a flag — ``users`` being non-empty.
There is no separate piece of state to drift out of step with reality, and
restoring a database backup restores the closed-ness along with it.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend import crud, database, schemas
from backend.rate_limit import rate_limit

# Reuse the login path's session helpers rather than reimplementing them: a
# claimed session must be indistinguishable from one obtained by signing in,
# down to refresh-token rotation and the httpOnly cookie.
from backend.routers.auth import (
    _build_access_token,
    _issue_refresh,
    open_registration_enabled,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/setup", tags=["setup"])


@router.get("/status", response_model=schemas.SetupStatus)
async def setup_status(db: AsyncSession = Depends(database.get_db)) -> schemas.SetupStatus:
    """Whether this installation still needs its first administrator.

    Unauthenticated by necessity: the login page asks this before anyone can
    possibly hold credentials. It discloses only whether the install has been
    claimed, which an attacker learns anyway the moment they try to claim it.
    """
    return schemas.SetupStatus(
        needs_setup=await crud.count_users(db) == 0,
        open_registration=open_registration_enabled(),
    )


@router.post(
    "/claim",
    response_model=schemas.Token,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit(5, 60))],
)
async def claim_installation(
    payload: schemas.UserCreate,
    response: Response,
    db: AsyncSession = Depends(database.get_db),
) -> dict:
    """Create the founding organisation and its admin, then sign them in."""
    await crud.lock_for_claim(db)
    if await crud.count_users(db) > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This installation has already been set up. Sign in, or ask an "
                "administrator for an invitation."
            ),
        )

    try:
        user = await crud.create_user(db=db, user=payload)
    except ValueError as exc:  # pragma: no cover - unreachable while the count is 0
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # Warning level on purpose. This is the only chance to tell the operator,
    # and losing the keystore costs them every credential they are about to
    # save through Settings.
    logger.warning(
        "Installation claimed by %s. Back up the secret keystore (the "
        "sentinel_keys volume, or wherever SENTINEL_SECRET_STORE points) "
        "together with the database: without it, credentials saved in Settings "
        "cannot be decrypted.",
        user.email,
    )

    await _issue_refresh(db, response, user, family_id=uuid.uuid4())
    return {"access_token": _build_access_token(user), "token_type": "bearer"}
