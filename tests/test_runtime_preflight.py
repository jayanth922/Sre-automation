"""The API and Temporal worker must be the same intact runtime."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import check_runtime_parity as crp
from sre_agent import runtime_preflight as rp


def _runtime_tree(root: Path) -> None:
    for package in ("backend", "sre_agent"):
        directory = root / package
        directory.mkdir()
        (directory / "__init__.py").write_text("", encoding="utf-8")
    (root / "sre_agent" / "worker.py").write_text("VALUE = 1\n", encoding="utf-8")


def test_manifest_detects_runtime_file_drift(tmp_path, monkeypatch):
    _runtime_tree(tmp_path)
    manifest = tmp_path / "runtime.json"
    rp.write_runtime_manifest(manifest, root=tmp_path, code_sha="abc123")
    monkeypatch.setenv(rp.CODE_SHA_ENV, "abc123")
    monkeypatch.setattr(rp, "verify_required_imports", lambda: None)

    identity = rp.verify_runtime("worker", root=tmp_path, manifest_path=manifest)
    assert identity.code_sha == "abc123"
    assert identity.file_count == 3

    (tmp_path / "sre_agent" / "worker.py").write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(rp.RuntimePreflightError, match="differ from the image"):
        rp.verify_runtime("worker", root=tmp_path, manifest_path=manifest)


def test_manifest_rejects_deployment_revision_mismatch(tmp_path, monkeypatch):
    _runtime_tree(tmp_path)
    manifest = tmp_path / "runtime.json"
    rp.write_runtime_manifest(manifest, root=tmp_path, code_sha="image-sha")
    monkeypatch.setenv(rp.CODE_SHA_ENV, "different-deploy-sha")
    monkeypatch.setattr(rp, "verify_required_imports", lambda: None)

    with pytest.raises(rp.RuntimePreflightError, match="revision mismatch"):
        rp.verify_runtime("api", root=tmp_path, manifest_path=manifest)


def test_api_worker_parity_compares_revision_fingerprint_and_file_count():
    identity = {
        "code_sha": "abc123",
        "fingerprint": "f" * 64,
        "file_count": 20,
    }
    rp.compare_runtime_identities(identity, dict(identity))

    worker = dict(identity, fingerprint="0" * 64)
    with pytest.raises(rp.RuntimePreflightError, match="fingerprint"):
        rp.compare_runtime_identities(identity, worker)
    with pytest.raises(crp.RuntimeParityError, match="fingerprint"):
        crp._compare(identity, worker)


def test_worker_import_preflight_fails_with_a_named_missing_module(monkeypatch):
    monkeypatch.setattr(
        rp,
        "_REQUIRED_SHARED_SYMBOLS",
        (("sre_agent.module_that_does_not_exist", "MissingSymbol"),),
    )

    with pytest.raises(rp.RuntimePreflightError, match="module_that_does_not_exist"):
        rp.verify_required_imports()


def test_docker_and_compose_build_one_verified_runtime():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "platform" / "Dockerfile").read_text()
    compose = (root / "platform" / "docker-compose.yaml").read_text()
    worker = (root / "sre_agent" / "sandbox_worker.py").read_text()
    deploy = (root / "scripts" / "deploy_agent_runtimes.sh").read_text()

    assert "python -m sre_agent.runtime_preflight" in dockerfile
    assert "--write-manifest /app/.sentinel-runtime.json" in dockerfile
    assert "SENTINEL_RUNTIME_MANIFEST=/app/.sentinel-runtime.json" in dockerfile
    assert "&sentinel-api-image" in compose
    assert compose.count("<<: *sentinel-api-image") == 2
    assert "image: sentinel/api:local" in compose
    assert "from .runtime_preflight import verify_runtime" in worker
    assert 'build sre-agent-api\n' in deploy
    assert "--force-recreate sre-agent-api temporal-worker" in deploy
    assert "check_runtime_parity.py" in deploy


def test_manifest_json_contains_no_source_payload(tmp_path):
    _runtime_tree(tmp_path)
    manifest = tmp_path / "runtime.json"
    rp.write_runtime_manifest(manifest, root=tmp_path, code_sha="abc123")

    payload = json.loads(manifest.read_text())
    assert set(payload) == {
        "code_sha",
        "file_count",
        "fingerprint",
        "schema_version",
    }
    assert "VALUE" not in manifest.read_text()
