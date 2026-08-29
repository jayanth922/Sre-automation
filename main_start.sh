#!/bin/bash
# =============================================================================
# Sentinel — start the PLATFORM and its EDGE tool servers only.
#
# The monitored target is the CUSTOMER's own infrastructure. It is intentionally
# NOT started here and has no coupling to the platform lifecycle. Optional
# reference-client wiring (e.g. Meridian) lives under deploy/examples/.
# =============================================================================
set -e

bash platform/start.sh

echo "▶ Starting Edge MCP Servers..."
cd edge_mcp_servers
docker compose --progress=quiet up -d --build
cd ..

echo ""
echo "✅ Platform + edge tool servers running."
echo "   Connect a cluster in the console (http://localhost:3002)."
echo "   Optional Meridian overlay: deploy/examples/meridian/"
