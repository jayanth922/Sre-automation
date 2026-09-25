"""P11: documentation and fixture truthfulness assertions."""

from __future__ import annotations

import os
import re
import runpy
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_benchmarks_do_not_ship_static_cluster_tokens():
    for path in (ROOT / "evals" / "benchmarks").glob("*.py"):
        if path.name == "fixtures.py":
            continue
        text = path.read_text()
        assert not re.search(r"cl_[0-9a-f]{20,}", text), path.name
        assert not re.search(r'ADMIN_PASSWORD\s*=\s*"admin"', text), path.name


def test_env_example_ships_no_seed_account():
    """The SEED_* block advertised a path no code ever implemented.

    Six variables described an auto-seeded admin and cluster; nothing in this
    repo has ever read one of them. The first admin now comes from the
    first-run claim page, so the block is gone -- and must not come back. A
    documented seed account is precisely the default credential the claim page
    exists to avoid, and one that never worked is worse than one that did.
    """
    text = (ROOT / ".env.example").read_text()
    for dead in (
        "SEED_ADMIN_EMAIL",
        "SEED_ADMIN_PASSWORD",
        "SEED_ADMIN_ORG",
        "SEED_CLUSTER_TOKEN",
        "SEED_CLUSTER_NAME",
        "SEED_CLUSTER_STATUS",
    ):
        assert dead not in text, dead

    # CLUSTER_TOKEN survives the cull: agent_runtime.py reads it for the
    # self-hosted single-cluster runtime. It must still ship empty.
    assert 'CLUSTER_TOKEN=""' in text
    assert not re.search(r'CLUSTER_TOKEN="cl_[0-9a-f]{20,}"', text)


def test_env_example_is_declared_optional():
    """A fresh install needs no .env at all, and the file has to say so.

    Every secret it used to demand is either generated into the keystore
    volume or configured per-cluster in the dashboard. If this file goes back
    to reading like a required checklist, the zero-config first run stops
    being discoverable even though it still works.
    """
    text = (ROOT / ".env.example").read_text()
    assert "OPTIONAL" in text.splitlines()[1]
    assert "claim page" in text


def test_env_example_does_not_advertise_the_dead_act_switch():
    """ACT_PHASE_ENABLED gates nothing.

    graph_builder._act_phase_enabled() returns True unconditionally, so
    documenting an off-by-default switch told operators the system was a
    read-only advisor when it had not been one for some time.
    """
    text = (ROOT / ".env.example").read_text()
    assert "ACT_PHASE_ENABLED" not in text


def test_architecture_readme_points_at_canonical_runtime():
    text = (ROOT / "docs" / "architecture" / "README.md").read_text()
    assert "agent-runtime-flow" in text
    assert "generate-diagrams" in text
    assert "source and SVG" in text


def test_obsolete_provider_session_doc_is_not_published():
    assert not (ROOT / "docs" / "session-nvidia-nim-benchmark.md").exists()
    scanner = (ROOT / "scripts" / "ci" / "check_no_static_secrets.sh").read_text()
    assert "session-nvidia-nim-benchmark.md" not in scanner
    assert "nvapi-" in scanner


def test_fixtures_module_requires_env_or_bootstrap():
    fixtures = runpy.run_path(str(ROOT / "evals" / "benchmarks" / "fixtures.py"))
    bench_config_error = fixtures["BenchConfigError"]
    load_credentials = fixtures["load_credentials"]

    for key in (
        "BENCH_ADMIN_EMAIL",
        "BENCH_ADMIN_PASSWORD",
        "BENCH_CLUSTER_ID",
        "BENCH_CLUSTER_TOKEN",
    ):
        os.environ.pop(key, None)
    with pytest.raises(bench_config_error):
        load_credentials()


def test_readme_documents_bootstrap_bench_path():
    readme = (ROOT / "README.md").read_text()
    assert "BENCH_BOOTSTRAP" in readme
    assert "fixtures.py" in readme
    assert "quickstart_smoke.sh" in readme


def test_public_markdown_links_resolve():
    documents = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", ROOT / "SECURITY.md"]
    for subtree in ("apps", "docs", "evals", "infra", "services", "src", "tests"):
        documents.extend((ROOT / subtree).rglob("*.md"))

    broken = []
    for path in documents:
        relative = path.relative_to(ROOT)
        if (
            {"archive", "node_modules", ".next"} & set(relative.parts)
            or relative == Path("docs/ai/DECISIONS.md")
        ):
            continue
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", text):
            raw = match.group(1).strip()
            if raw.startswith(("#", "http://", "https://", "mailto:", "app://")):
                continue
            target = unquote(raw.split("#", 1)[0].strip("<>"))
            if target and not (path.parent / target).resolve().exists():
                broken.append(f"{relative}:{target}")
    assert broken == []


def test_architecture_sources_and_generated_images_are_paired():
    architecture = ROOT / "docs" / "architecture"
    sources = {path.stem for path in architecture.glob("*.mmd")}
    images = {path.stem for path in (architecture / "images").glob("*.svg")}
    assert sources == images
    assert "target-client-architecture" not in sources


def test_docs_do_not_ship_orphaned_demo_screenshots():
    assert list((ROOT / "docs").glob("*.png")) == []
