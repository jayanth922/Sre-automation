#!/usr/bin/env python3
"""Source-contract tests preventing credential disclosure regressions."""

import ast
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_SECRET_FIELDS = {
    "token",
    "token_hash",
    "k8s_token",
    "github_token",
    "notion_api_key",
    "llm_api_key",
}


def _class_fields(path: Path, class_name: str) -> set[str]:
    tree = ast.parse(path.read_text())
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.ClassDef) and item.name == class_name
    )
    return {
        item.target.id
        for item in node.body
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
    }


def test_cluster_response_contains_no_credential_fields():
    fields = _class_fields(_ROOT / "backend" / "schemas.py", "ClusterResponse")
    assert fields.isdisjoint(_SECRET_FIELDS)


def test_cluster_token_lookup_uses_hash_not_ciphertext_or_plaintext():
    source = (_ROOT / "backend" / "crud.py").read_text()
    start = source.index("async def get_cluster_by_token")
    block = source[start : source.index("\nasync def ", start + 1)]
    assert "credential_lookup_hash(token)" in block
    assert "Cluster.token_hash == token_hash" in block
    assert "Cluster.token == token" not in block


def test_all_confirmed_cluster_credentials_use_encrypted_type():
    source = (_ROOT / "backend" / "models.py").read_text()
    tree = ast.parse(source)
    cluster = next(
        item
        for item in tree.body
        if isinstance(item, ast.ClassDef) and item.name == "Cluster"
    )
    assignments = {
        item.target.id: ast.get_source_segment(source, item) or ""
        for item in cluster.body
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
    }
    for field in ("token", "k8s_token", "github_token", "notion_api_key", "llm_api_key"):
        assert "EncryptedString()" in assignments[field]


def test_data_migration_covers_every_confirmed_plaintext_column():
    source = (
        _ROOT
        / "backend"
        / "alembic"
        / "versions"
        / "b1c2d3e4f5a6_encrypt_cluster_credentials.py"
    ).read_text()
    for field in ("token", "k8s_token", "github_token", "notion_api_key", "llm_api_key"):
        assert f'"{field}"' in source
    assert "encrypt_value(" in source
    assert "credential_lookup_hash(" in source
    assert '"key_version"' in source
    assert 'down_revision: Union[str, None] = "9c0d1e2f3a4b"' in source


def test_invalid_cluster_token_is_not_logged_even_partially():
    source = (_ROOT / "sre_agent" / "api" / "v1" / "alerts.py").read_text()
    assert "token[-4:]" not in source
    assert "Invalid cluster token provided" in source
