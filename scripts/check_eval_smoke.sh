#!/usr/bin/env bash
# Lightweight eval / invariant gate — deterministic unit tests that encode
# product safety invariants (status transitions, execution context, etc.).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"

EXISTING=()
for t in tests/test_incident_status.py tests/test_execution_context.py; do
  if [ -f "$t" ]; then
    EXISTING+=("$t")
  fi
done

if [ "${#EXISTING[@]}" -eq 0 ]; then
  echo "ERROR: no eval smoke tests found" >&2
  exit 1
fi

echo "==> eval smoke: ${EXISTING[*]}"
if [ -x .venv/bin/pytest ]; then
  .venv/bin/pytest -q "${EXISTING[@]}"
elif command -v pytest >/dev/null 2>&1; then
  pytest -q "${EXISTING[@]}"
else
  python3 -m pytest -q "${EXISTING[@]}"
fi

echo "Eval smoke passed."
