"""Secure organization invitation creation and acceptance."""

import json
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend import auth, crud, database, models, schemas
from sre_agent.api.v1.auth_deps import get_current_user_and_org, require_admin
from sre_agent.invitation_rules import (
    InvitationStateError,
    invited_user_attributes,
    validate_invitation_state,
)


# Acceptance must remain public because the invited user has no account yet.
router = APIRouter(prefix="/invitations", tags=["invitations"])

# Invitation creation is a separate authenticated router so future routes under
# this prefix inherit user authentication even if they omit a local dependency.
organization_router = APIRouter(
    prefix="/organizations/{org_id}/invitations",
    tags=["invitations"],
    dependencies=[Depends(get_current_user_and_org)],
)


def _role_value(role: models.UserRole | str) -> str:
    return role.value if isinstance(role, models.UserRole) else str(role)


def _audit_event(
    *,
    organization_id: uuid.UUID,
    actor_id: str,
    action_type: str,
    invitation_id: uuid.UUID,
    outcome: str = "SUCCESS",
    details: dict | None = None,
) -> models.AuditEvent:
    return models.AuditEvent(
        organization_id=organization_id,
        cluster_id=None,
        actor_type="USER",
        actor_id=actor_id,
        action_type=action_type,
        resource_target=f"org_invitation/{invitation_id}",
        outcome=outcome,
        details=json.dumps(details, sort_keys=True) if details else None,
    )


@organization_router.post(
    "",
    response_model=schemas.InvitationCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_invitation(
    org_id: uuid.UUID,
    payload: schemas.InvitationCreate,
    admin: models.User = Depends(require_admin),
    db: AsyncSession = Depends(database.get_db),
):
    """Create a single-use invitation and return its raw token once."""
    if admin.org_id != org_id:
        raise HTTPException(status_code=404, detail="Organization not found")

    email = str(payload.email).strip().lower()
    if await crud.get_user_by_email(db, email):
        raise HTTPException(status_code=409, detail="Email already registered")

    now = datetime.now(timezone.utc)
    # Reissuing an invitation invalidates any older live token for the same
    # organization/email pair, so a lost token never blocks an administrator.
    prior_result = await db.execute(
        select(models.OrgInvitation)
        .where(
            models.OrgInvitation.organization_id == org_id,
            models.OrgInvitation.email == email,
            models.OrgInvitation.accepted_at.is_(None),
            models.OrgInvitation.revoked_at.is_(None),
            models.OrgInvitation.expires_at > now,
        )
        .with_for_update()
    )
    for prior in prior_result.scalars().all():
        prior.revoked_at = now

    raw_token = auth.generate_refresh_token()
    invitation = models.OrgInvitation(
        organization_id=org_id,
        email=email,
        token_hash=auth.hash_refresh_token(raw_token),
        role=payload.role,
        invited_by_user_id=admin.id,
        expires_at=now + timedelta(hours=payload.expires_in_hours),
    )
    db.add(invitation)

    try:
        await db.flush()
        db.add(
            _audit_event(
                organization_id=org_id,
                actor_id=str(admin.id),
                action_type="ORG_INVITATION_CREATED",
                invitation_id=invitation.id,
                details={"email": email, "role": _role_value(payload.role)},
            )
        )
        await db.commit()
        await db.refresh(invitation)
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Invitation could not be created") from exc

    return schemas.InvitationCreateResponse(
        id=invitation.id,
        organization_id=invitation.organization_id,
        email=invitation.email,
        role=invitation.role,
        expires_at=invitation.expires_at,
        token=raw_token,
    )


@router.post("/accept", response_model=schemas.UserResponse)
async def accept_invitation(
    payload: schemas.InvitationAccept,
    db: AsyncSession = Depends(database.get_db),
):
    """Atomically consume an invitation and create its server-scoped user."""
    token_hash = auth.hash_refresh_token(payload.token)
    result = await db.execute(
        select(models.OrgInvitation)
        .where(models.OrgInvitation.token_hash == token_hash)
        .with_for_update()
    )
    invitation = result.scalars().first()
    if invitation is None:
        raise HTTPException(status_code=400, detail="Invalid invitation")

    now = datetime.now(timezone.utc)
    try:
        validate_invitation_state(invitation, now)
    except InvitationStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if await crud.get_user_by_email(db, invitation.email):
        raise HTTPException(status_code=409, detail="Email already registered")

    user = models.User(
        **invited_user_attributes(
            invitation,
            hashed_password=auth.get_password_hash(payload.password),
            full_name=payload.full_name,
        )
    )
    db.add(user)
    invitation.accepted_at = now

    try:
        await db.flush()
        db.add(
            _audit_event(
                organization_id=invitation.organization_id,
                actor_id=str(user.id),
                action_type="ORG_INVITATION_ACCEPTED",
                invitation_id=invitation.id,
                details={"email": invitation.email, "role": _role_value(invitation.role)},
            )
        )
        await db.commit()
        await db.refresh(user)
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Email already registered") from exc

    return user
