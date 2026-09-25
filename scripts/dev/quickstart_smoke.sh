#!/usr/bin/env bash
# Clean-environment quickstart smoke (P11) — no live cluster required.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

echo "==> secret scan"
bash scripts/ci/check_no_static_secrets.sh

echo "==> compile Python packages"
python3 -m compileall -q src evals

echo "==> docs truthfulness unit checks"
if [[ -x .venv/bin/pytest ]]; then
  PYTHONPATH="$ROOT/src:$ROOT/evals" .venv/bin/pytest -q tests/test_docs_truthfulness.py
elif command -v pytest >/dev/null 2>&1; then
  PYTHONPATH="$ROOT/src:$ROOT/evals" pytest -q tests/test_docs_truthfulness.py
elif command -v uv >/dev/null 2>&1; then
  PYTHONPATH="$ROOT/src:$ROOT/evals" uv run pytest -q tests/test_docs_truthfulness.py
else
  PYTHONPATH="$ROOT/src:$ROOT/evals" python3 -m pytest -q tests/test_docs_truthfulness.py
fi

echo "==> helm RBAC / WS defaults"
bash scripts/ci/check_helm_rbac.sh
bash scripts/ci/check_helm_ws.sh

echo "Quickstart smoke passed."
