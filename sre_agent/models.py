"""Compatibility shim — canonical ORM models live in ``backend.models``.

Historically this module defined a second ``DeclarativeBase`` and overlapping
tables (Organization/User/Cluster/…), which diverged from Alembic metadata and
caused audit-table drift. New models must be added only to ``backend.models``.
"""

from __future__ import annotations

from backend.models import AgentAuditLog, Base

__all__ = ["AgentAuditLog", "Base"]
