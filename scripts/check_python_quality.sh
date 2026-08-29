#!/usr/bin/env bash
# Critical Python quality gate for PRs.
# Blocks on syntax / undefined-name / redefinition bugs. Full style cleanup is incremental.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
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

echo "==> ruff critical (E9/F63/F7/F82/F821/F823/F811)"
"${RUFF[@]}" check backend sre_agent tests \
  --select E9,F63,F7,F82,F821,F823,F811

echo "==> mypy curated modules (fail-closed on typed core)"
# Curated allowlist: expand as modules are cleaned. Failures here block merge.
MYPY_TARGETS=(
  backend/models.py
  sre_agent/incident_status.py
  sre_agent/execution_context.py
)
"${MYPY[@]}" \
  --follow-imports=skip \
  --ignore-missing-imports \
  "${MYPY_TARGETS[@]}"

echo "==> python -m compileall"
"${PYTHON[@]}" -m compileall -q backend sre_agent

echo "Python quality checks passed."
