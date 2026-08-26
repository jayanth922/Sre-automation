"""Pure invitation validation shared by the API and lightweight tests."""

from datetime import datetime, timezone
from typing import Any, Dict, Optional


class InvitationStateError(ValueError):
    """An invitation exists but is no longer eligible for acceptance."""


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def validate_invitation_state(invitation: Any, now: Optional[datetime] = None) -> None:
    """Reject replayed, revoked, or expired invitations."""
    current = _as_utc(now or datetime.now(timezone.utc))
    if invitation.accepted_at is not None:
        raise InvitationStateError("Invitation already accepted")
    if invitation.revoked_at is not None:
        raise InvitationStateError("Invitation revoked")
    if _as_utc(invitation.expires_at) <= current:
        raise InvitationStateError("Invitation expired")


def invited_user_attributes(
    invitation: Any,
    *,
    hashed_password: str,
    full_name: Optional[str],
) -> Dict[str, Any]:
    """Build server-owned identity fields for an invited user.

    Role, organization, and email come only from the persisted invitation; no
    client-supplied identity or privilege field is accepted here.
    """
    return {
        "email": invitation.email,
        "hashed_password": hashed_password,
        "full_name": full_name,
        "role": invitation.role,
        "org_id": invitation.organization_id,
    }
