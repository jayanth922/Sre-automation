"""P11: documentation and fixture truthfulness assertions."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_benchmarks_do_not_ship_static_cluster_tokens():
    for path in (ROOT / "benchmarks").glob("*.py"):
        if path.name == "fixtures.py":
            continue
        text = path.read_text()
        assert not re.search(r"cl_[0-9a-f]{20,}", text), path.name
        assert 'ADMIN_PASSWORD = "admin"' not in text


def test_env_example_has_no_seed_secrets():
    text = (ROOT / ".env.example").read_text()
    assert 'SEED_ADMIN_PASSWORD="admin"' not in text
    assert "cl_58f71c23a54e4b5ab1d10c8defccfc6d" not in text
    assert 'SEED_ADMIN_PASSWORD=""' in text


def test_architecture_readme_points_at_canonical_runtime():
    text = (ROOT / "docs" / "architecture" / "README.md").read_text()
    assert "agent-runtime-flow" in text
    assert "quickstart_smoke.sh" in text
    assert "agent_runtime.py" in text


def test_historical_docs_are_labeled():
    session = (ROOT / "docs" / "session-nvidia-nim-benchmark.md").read_text()
    assert "HISTORICAL" in session
    assert "fixtures.py" in session


def test_fixtures_module_requires_env_or_bootstrap():
    from benchmarks.fixtures import BenchConfigError, load_credentials

    for key in (
        "BENCH_ADMIN_EMAIL",
        "BENCH_ADMIN_PASSWORD",
        "BENCH_CLUSTER_ID",
        "BENCH_CLUSTER_TOKEN",
    ):
        os.environ.pop(key, None)
    with pytest.raises(BenchConfigError):
        load_credentials()


def test_readme_documents_bootstrap_bench_path():
    readme = (ROOT / "README.md").read_text()
    assert "BENCH_BOOTSTRAP" in readme
    assert "fixtures.py" in readme
    assert "quickstart_smoke.sh" in readme
