#!/usr/bin/env python3
"""P07: canonical ORM Base / schema consistency contracts."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_backend_models():
    # Prefer package import when available.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import backend.models as models

    return models


def test_agent_audit_log_lives_on_canonical_base():
    models = _load_backend_models()
    assert hasattr(models, "AgentAuditLog")
    assert models.AgentAuditLog.__tablename__ == "agent_audit_logs"
    assert models.AgentAuditLog.__table__.metadata is models.Base.metadata
    assert "agent_audit_logs" in models.Base.metadata.tables


def test_sre_agent_models_is_shim_not_second_base():
    source = (ROOT / "sre_agent" / "models.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    class_names = {
        node.name for node in tree.body if isinstance(node, ast.ClassDef)
    }
    assert "Base" not in class_names
    assert "Organization" not in class_names
    assert "AgentAuditLog" not in class_names
    assert "from backend.models import" in source or "import backend.models" in source


def test_shim_reexports_same_agent_audit_log_class():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import backend.models as backend_models
    import sre_agent.models as shim

    assert shim.AgentAuditLog is backend_models.AgentAuditLog
    assert shim.Base is backend_models.Base


def test_refresh_session_repr_is_not_slo():
    source = (ROOT / "backend" / "models.py").read_text(encoding="utf-8")
    # Locate RefreshSession class body and assert its __repr__ is self-named.
    start = source.index("class RefreshSession")
    end = source.index("\nclass ", start + 1)
    body = source[start:end]
    assert "def __repr__" in body
    assert "RefreshSession" in body
    assert 'SLO(name=' not in body
    assert "target={self.target}" not in body


def test_tablename_uniqueness_on_canonical_metadata():
    models = _load_backend_models()
    names = [t.name for t in models.Base.metadata.sorted_tables]
    assert len(names) == len(set(names))
    # Core SaaS tables that must remain on the single Base.
    required = {
        "organizations",
        "users",
        "clusters",
        "incidents",
        "audit_logs",
        "audit_events",
        "agent_audit_logs",
        "approval_requests",
        "refresh_sessions",
    }
    missing = required - set(names)
    assert not missing, f"missing tables on canonical Base: {missing}"


def test_alembic_env_targets_backend_base():
    env = (ROOT / "backend" / "alembic" / "env.py").read_text(encoding="utf-8")
    assert "from backend.models import Base" in env
    assert "sre_agent.models" not in env


def test_agent_audit_migration_exists_and_is_head_child():
    mig = (
        ROOT
        / "backend"
        / "alembic"
        / "versions"
        / "d3ac85ffcc7d_add_agent_audit_logs.py"
    )
    assert mig.is_file()
    text = mig.read_text(encoding="utf-8")
    assert 'revision: str = "d3ac85ffcc7d"' in text
    assert 'down_revision: Union[str, None] = "b1c7ceb2036b"' in text
    assert "agent_audit_logs" in text


def test_alembic_single_head_includes_agent_audit():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert heads == ["a3f7c1d9b2e4"], heads
