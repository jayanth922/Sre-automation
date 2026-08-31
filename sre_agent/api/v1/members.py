"""Organization member management.

Any authenticated member can view the roster. Only admins can change a
member's role or activation status. Guardrails prevent an org from locking
itself out by demoting or deactivating its last active admin.
"""
import uuid
import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend import schemas, crud, models, database
from sre_agent.api.v1.auth_deps import get_current_user_and_org, require_admin

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/organization/members",
    tags=["members"],
    dependencies=[Depends(get_current_user_and_org)],
)

# Separate router (same file, sibling prefix) for org-level info such as
# whether Slack is connected — kept out of the /members prefix since it's
# not member data.
organization_router = APIRouter(
    prefix="/organization",
    tags=["organization"],
    dependencies=[Depends(get_current_user_and_org)],
)


@router.get("", response_model=List[schemas.OrgMemberResponse])
async def list_members(
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
):
    """List all members of the caller's organization."""
    return await crud.get_users_for_org(db, user.org_id)


@organization_router.get("", response_model=schemas.OrgResponse)
async def get_organization(
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
):
    """Return the caller's organization, including Slack-connected status."""
    org = await crud.get_org_by_id(db, user.org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org


async def _member_in_org(db: AsyncSession, member_id: uuid.UUID, org_id: uuid.UUID) -> models.User:
    member = await crud.get_user_by_id(db, member_id)
    if not member or member.org_id != org_id:
        raise HTTPException(status_code=404, detail="Member not found")
    return member


@router.patch("/{member_id}/role", response_model=schemas.OrgMemberResponse)
async def update_member_role(
    member_id: uuid.UUID,
    payload: schemas.MemberRoleUpdate,
    admin: models.User = Depends(require_admin),
    db: AsyncSession = Depends(database.get_db),
):
    """Assign or revoke admin for a member (admin only)."""
    member = await _member_in_org(db, member_id, admin.org_id)

    # Prevent removing the last active admin (would lock the org out).
    if (
        member.role == models.UserRole.ADMIN
        and payload.role != models.UserRole.ADMIN
        and await crud.count_active_admins(db, admin.org_id) <= 1
    ):
        raise HTTPException(
            status_code=400,
            detail="Cannot demote the last remaining admin. Promote another member first.",
        )

    return await crud.update_user_role(db, member, payload.role)


@router.patch("/{member_id}/status", response_model=schemas.OrgMemberResponse)
async def update_member_status(
    member_id: uuid.UUID,
    payload: schemas.MemberStatusUpdate,
    admin: models.User = Depends(require_admin),
    db: AsyncSession = Depends(database.get_db),
):
    """Activate or deactivate a member (admin only). Deactivating logs them out."""
    member = await _member_in_org(db, member_id, admin.org_id)

    if member.id == admin.id and not payload.is_active:
        raise HTTPException(status_code=400, detail="You cannot deactivate your own account.")

    if (
        not payload.is_active
        and member.role == models.UserRole.ADMIN
        and await crud.count_active_admins(db, admin.org_id) <= 1
    ):
        raise HTTPException(
            status_code=400,
            detail="Cannot deactivate the last remaining admin.",
        )

    return await crud.set_user_active(db, member, payload.is_active)
