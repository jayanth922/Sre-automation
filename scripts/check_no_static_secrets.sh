#!/usr/bin/env bash
# Fail if committed files contain credential-shaped literals (P11).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Patterns that should never appear as hardcoded values in tracked sources.
# Allow documentation ellipsis forms like xoxb-... and YOUR_KEY placeholders.
PATTERN='cl_[0-9a-f]{20,}|ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|xoxb-[0-9]{5,}-[A-Za-z0-9-]{10,}'

HITS="$(git grep -nE "$PATTERN" -- \
  ':!.env' \
  ':!*.lock' \
  ':!uv.lock' \
  ':!docs/session-nvidia-nim-benchmark.md' \
  ':!archive/**' \
  ':!tests/test_docs_truthfulness.py' \
  2>/dev/null || true)"

if [[ -n "$HITS" ]]; then
  echo "Credential-shaped literals found in tracked files:" >&2
  echo "$HITS" >&2
  echo >&2
  echo "Use runtime fixtures / env vars instead (see benchmarks/fixtures.py)." >&2
  exit 1
fi

# Explicitly reject known historical demo defaults if they reappear in benches.
BENCH_HITS="$(git grep -nE 'CLUSTER_TOKEN[[:space:]]*=[[:space:]]*"cl_|ADMIN_PASSWORD[[:space:]]*=[[:space:]]*"admin"' -- \
  'benchmarks/*.py' \
  ':!benchmarks/fixtures.py' \
  2>/dev/null || true)"
if [[ -n "$BENCH_HITS" ]]; then
  echo "Benchmark files still embed static demo credentials" >&2
  echo "$BENCH_HITS" >&2
  exit 1
fi

echo "Secret scan passed."
