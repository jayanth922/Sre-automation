#!/usr/bin/env bash
# Apply Alembic migrations to an empty database (CI / local smoke).
# Uses async DATABASE_URL (postgresql+asyncpg://...) as expected by backend.alembic.env.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

: "${DATABASE_URL:?DATABASE_URL must be set}"

# Normalize to asyncpg URL for env.py / async_engine_from_config
if [[ "$DATABASE_URL" == postgresql://* ]]; then
  export DATABASE_URL="postgresql+asyncpg://${DATABASE_URL#postgresql://}"
elif [[ "$DATABASE_URL" == postgres://* ]]; then
  export DATABASE_URL="postgresql+asyncpg://${DATABASE_URL#postgres://}"
fi

echo "==> alembic upgrade head"
if command -v uv >/dev/null 2>&1; then
  uv run alembic upgrade head
  echo "==> alembic current"
  uv run alembic current
else
  alembic upgrade head
  alembic current
fi

echo "Migrations applied successfully."
