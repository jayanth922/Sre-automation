#!/usr/bin/env python3
"""Invitation security regression tests for T05."""

import ast
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from sre_agent.invitation_rules import (
    InvitationStateError,
    invited_user_attributes,
    validate_invitation_state,
)


_ROOT = Path(__file__).resolve().parents[1]


def _invitation(**overrides):
    now = datetime.now(timezone.utc)
    values = {
        "email": "invitee@example.com",
        "organization_id": "org-a",
        "role": "member",
        "expires_at": now + timedelta(hours=1),
        "accepted_at": None,
        "revoked_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _function_source(path: Path, function_name: str) -> str:
    source = path.read_text()
    tree = ast.parse(source)
    node = next(
        item
        for item in ast.walk(tree)
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == function_name
    )
    lines = source.splitlines()
    return "\n".join(lines[node.lineno - 1 : node.end_lineno])


def test_live_invitation_is_acceptable():
    validate_invitation_state(_invitation())


@pytest.mark.parametrize(
    ("field", "detail"),
    [
        ("accepted_at", "Invitation already accepted"),
        ("revoked_at", "Invitation revoked"),
    ],
)
def test_replay_and_revocation_are_rejected(field, detail):
    invitation = _invitation(**{field: datetime.now(timezone.utc)})
    with pytest.raises(InvitationStateError, match=detail):
        validate_invitation_state(invitation)


def test_expired_invitation_is_rejected_with_naive_database_timestamp():
    invitation = _invitation(
        expires_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
    )
    with pytest.raises(InvitationStateError, match="Invitation expired"):
        validate_invitation_state(invitation)


def test_invitation_fixes_role_organization_and_email_server_side():
    invitation = _invitation(role="member")
    malicious_client_role = "admin"
    attributes = invited_user_attributes(
        invitation,
        hashed_password="hashed",
        full_name="Invited User",
    )

    assert malicious_client_role != invitation.role
    assert attributes["role"] == invitation.role
    assert attributes["org_id"] == invitation.organization_id
    assert attributes["email"] == invitation.email
    assert "requested_role" not in inspect.signature(invited_user_attributes).parameters


def test_accept_route_hashes_and_locks_token_without_using_client_role():
    path = _ROOT / "sre_agent" / "api" / "v1" / "invitations.py"
    block = _function_source(path, "accept_invitation")
    assert "auth.hash_refresh_token(payload.token)" in block
    assert ".with_for_update()" in block
    assert "validate_invitation_state(invitation, now)" in block
    assert "payload.role" not in block
    assert "invited_user_attributes(" in block


def test_creation_is_admin_only_org_scoped_and_returns_raw_token_once():
    source = (_ROOT / "sre_agent" / "api" / "v1" / "invitations.py").read_text()
    block = _function_source(
        _ROOT / "sre_agent" / "api" / "v1" / "invitations.py",
        "create_invitation",
    )
    assert "dependencies=[Depends(get_current_user_and_org)]" in source
    assert "admin: models.User = Depends(require_admin)" in block
    assert "admin.org_id != org_id" in block
    assert "token_hash=auth.hash_refresh_token(raw_token)" in block
    assert "token=raw_token" in block
    assert "ORG_INVITATION_CREATED" in block


def test_acceptance_writes_canonical_organization_audit_event():
    block = _function_source(
        _ROOT / "sre_agent" / "api" / "v1" / "invitations.py",
        "accept_invitation",
    )
    assert "models.AuditEvent" in (
        _ROOT / "sre_agent" / "api" / "v1" / "invitations.py"
    ).read_text()
    assert "ORG_INVITATION_ACCEPTED" in block
    assert "organization_id=invitation.organization_id" in block


def test_registration_always_creates_a_new_organization():
    block = _function_source(_ROOT / "backend" / "crud.py", "create_user")
    assert "get_org_by_name" not in block
    assert "models.Organization(" in block
    assert "role=models.UserRole.ADMIN" in block


def test_model_and_migration_store_only_token_hash_and_extend_audit_scope():
    models_source = (_ROOT / "backend" / "models.py").read_text()
    migration_source = (
        _ROOT
        / "backend"
        / "alembic"
        / "versions"
        / "9c0d1e2f3a4b_add_org_invitations.py"
    ).read_text()
    invitation_model = models_source.split("class OrgInvitation", 1)[1]
    assert "token_hash" in invitation_model
    assert "raw_token" not in invitation_model
    assert 'down_revision: Union[str, None] = "0a1b2c3d4e5f"' in migration_source
    assert '"org_invitations"' in migration_source
    assert '"organization_id"' in migration_source
    assert '"audit_events"' in migration_source
    assert "nullable=True" in migration_source


def test_invitation_routers_are_mounted():
    source = (_ROOT / "sre_agent" / "agent_runtime.py").read_text()
    assert 'app.include_router(invitations.organization_router, prefix="/api/v1")' in source
    assert 'app.include_router(invitations.router, prefix="/api/v1")' in source
