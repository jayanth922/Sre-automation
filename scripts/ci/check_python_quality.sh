#!/usr/bin/env bash
# Critical Python quality gate for PRs.
# Blocks on syntax / undefined-name / redefinition bugs. Full style cleanup is incremental.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

if [ -x .venv/bin/ruff ] && [ -x .venv/bin/mypy ]; then
  RUFF=(.venv/bin/ruff)
  MYPY=(.venv/bin/mypy)
  PYTHON=(.venv/bin/python)
elif command -v ruff >/dev/null 2>&1 && command -v mypy >/dev/null 2>&1; then
  RUFF=(ruff)
  MYPY=(mypy)
  PYTHON=(python3)
else
  RUFF=(python3 -m ruff)
  MYPY=(python3 -m mypy)
  PYTHON=(python3)
fi

# A dependency added to pyproject.toml without re-locking is invisible to CI —
# `uv sync --frozen` installs the stale lock and says nothing — but it is fatal
# in the container: the entrypoint's first `uv run` re-locks, rewrites
# `uv.lock`, and `uv.lock` is one of the files the runtime manifest
# fingerprints, so the API kills itself at startup with "runtime files differ
# from the image manifest". That is how `tiktoken>=0.7.0` (added by the
# context-budget work) took the stack down on first deploy.
if command -v uv >/dev/null 2>&1; then
  echo "==> uv lock is in sync with pyproject.toml"
  uv lock --check
else
  echo "==> uv not on PATH; skipping lockfile drift check"
fi

echo "==> ruff critical (E9/F63/F7/F82/F821/F823/F811)"
# `benchmarks` is in this list because leaving it out cost a campaign:
# sre_bench.py called resolve_credentials() without importing it, which is a
# NameError raised only once a run is already underway, and F821 would have
# caught it statically the whole time.
"${RUFF[@]}" check src tests evals scripts \
  --select E9,F63,F7,F82,F821,F823,F811

echo "==> mypy curated modules (fail-closed on typed core)"
# Curated allowlist: expand as modules are cleaned. Failures here block merge.
MYPY_TARGETS=(
  src/backend/models.py
  src/sre_agent/incident_status.py
  src/sre_agent/execution_context.py
)
"${MYPY[@]}" \
  --follow-imports=skip \
  --ignore-missing-imports \
  "${MYPY_TARGETS[@]}"

echo "==> python -m compileall"
"${PYTHON[@]}" -m compileall -q src evals

echo "Python quality checks passed."
