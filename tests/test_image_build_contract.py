"""The agent image must be able to import the code it just copied.

PR #56 moved the production packages under `src/`. `infra/local/Dockerfile`
was updated to copy from the new location, but the `ENV PYTHONPATH="/app/src"`
that makes `src/` importable stayed where it had always been -- in the runtime
environment block near the bottom, *after* the two `RUN` steps that import
`sre_agent.runtime_preflight` to freeze the runtime manifest. Every rebuild of
the image died at `ModuleNotFoundError: No module named 'sre_agent'`.

Nothing caught it for a month because the running containers predated the
refactor: an image nobody rebuilds cannot fail to build. The preflight exists
precisely to prove the shared API/worker imports are complete at build time,
so a preflight that cannot import anything at all is the one failure it must
never have.

This is the same producer/consumer drift the rest of the suite pins in Python
-- a consumer (the Dockerfile) reading a layout a producer (the repo) changed
underneath it -- so it is guarded the same way: statically, with no daemon.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCKERFILE = Path(__file__).resolve().parents[1] / "infra" / "local" / "Dockerfile"

# `sre_agent` and `backend` both live here; the image has no site-packages copy.
IMPORT_ROOT = "/app/src"


def _instructions() -> list[tuple[int, str, str]]:
    """(line number, verb, body) per Dockerfile instruction, continuations joined."""
    lines = DOCKERFILE.read_text(encoding="utf-8").splitlines()
    out: list[tuple[int, str, str]] = []
    buffer: list[str] = []
    start = 0
    for number, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not buffer and (not stripped or stripped.startswith("#")):
            continue
        if not buffer:
            start = number
        buffer.append(stripped.removesuffix("\\").strip())
        if stripped.endswith("\\"):
            continue
        joined = " ".join(part for part in buffer if part)
        buffer = []
        verb, _, body = joined.partition(" ")
        out.append((start, verb.upper(), body))
    return out


def test_the_dockerfile_exists_where_compose_builds_it() -> None:
    assert DOCKERFILE.is_file(), f"{DOCKERFILE} is the image compose builds"


def test_python_path_is_declared_before_anything_imports_the_copied_code() -> None:
    instructions = _instructions()

    env_lines = [
        line
        for line, verb, body in instructions
        if verb == "ENV" and "PYTHONPATH" in body and IMPORT_ROOT in body
    ]
    assert env_lines, (
        f"no ENV sets PYTHONPATH to {IMPORT_ROOT}; the image copies the packages "
        "to /app/src and installs nothing into site-packages, so without it "
        "`import sre_agent` fails at build time and at runtime"
    )

    importing = [
        line
        for line, verb, body in instructions
        if verb == "RUN" and re.search(r"python\s+-m\s+sre_agent", body)
    ]
    assert importing, (
        "no RUN imports sre_agent -- the build-time preflight that proves the "
        "shared API/worker imports are complete has gone missing"
    )

    assert min(env_lines) < min(importing), (
        f"PYTHONPATH is set at line {min(env_lines)} but a RUN imports sre_agent "
        f"at line {min(importing)}. A Dockerfile ENV only applies to instructions "
        "after it, so the preflight runs without /app/src on sys.path and the "
        "build dies at ModuleNotFoundError."
    )


def test_python_path_is_declared_after_the_source_it_points_at_is_copied() -> None:
    """Ordering has two edges: the ENV must also not drift above the COPY."""
    instructions = _instructions()
    copies = [
        line
        for line, verb, body in instructions
        if verb == "COPY" and "src/sre_agent" in body
    ]
    importing = [
        line
        for line, verb, body in instructions
        if verb == "RUN" and re.search(r"python\s+-m\s+sre_agent", body)
    ]
    assert copies, "the image no longer copies src/sre_agent/"
    assert max(copies) < min(importing), (
        "the preflight RUN precedes the COPY that puts sre_agent in the image"
    )


@pytest.mark.parametrize("package", ["src/sre_agent", "src/backend"])
def test_the_image_copies_both_packages_the_preflight_checks(package: str) -> None:
    bodies = [body for _, verb, body in _instructions() if verb == "COPY"]
    assert any(package in body for body in bodies), (
        f"{package} is not copied into the image, but runtime_preflight imports "
        "both the API and worker entrypoints and will fail on the missing half"
    )
