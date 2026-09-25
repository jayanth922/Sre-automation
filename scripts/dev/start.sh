#!/usr/bin/env bash
# =============================================================================
# Sentinel — start the PLATFORM and its EDGE tool servers only.
#
# The monitored target is the CUSTOMER's own infrastructure. It is intentionally
# NOT started here and has no coupling to the platform lifecycle. Optional
# reference-client wiring (e.g. Meridian) lives under examples/.
# =============================================================================
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

bash "$repo_root/infra/local/start.sh"

echo "▶ Starting Edge MCP Servers..."
docker compose \
  --project-directory "$repo_root/services/edge_mcp_servers" \
  -f "$repo_root/services/edge_mcp_servers/docker-compose.yaml" \
  --progress=quiet up -d --build

echo ""
echo "✅ Platform + edge tool servers running."
echo "   Connect a cluster in the console (http://localhost:3002)."
echo "   Optional Meridian overlay: examples/meridian/deployment/"
