#!/usr/bin/env bash
# Build one revision once, recreate both entrypoints, then prove parity.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

if ! git -C "$ROOT" diff --quiet || ! git -C "$ROOT" diff --cached --quiet; then
  echo "refusing runtime deploy from a dirty tracked working tree" >&2
  exit 1
fi

SENTINEL_CODE_SHA="$(git -C "$ROOT" rev-parse HEAD)"
export SENTINEL_CODE_SHA

compose=(
  docker compose
  --env-file "$ROOT/.env"
  -f "$ROOT/infra/local/docker-compose.yaml"
)

echo "building shared sentinel/api:local from ${SENTINEL_CODE_SHA}"
"${compose[@]}" build sre-agent-api
"${compose[@]}" up -d --no-build --force-recreate sre-agent-api temporal-worker

for attempt in $(seq 1 30); do
  if python3 "$ROOT/scripts/ci/check_runtime_parity.py"; then
    exit 0
  fi
  if [ "$attempt" -eq 30 ]; then
    echo "runtime parity did not become healthy after 60 seconds" >&2
    exit 1
  fi
  sleep 2
done
