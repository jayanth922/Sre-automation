#!/bin/bash
# =============================================================================
# Sentinel — start the PLATFORM and its EDGE tool servers only.
#
# The monitored target is the CUSTOMER's own infrastructure. It is intentionally
# NOT started here and has no coupling to the platform lifecycle. The reference
# client environment (Meridian Commerce) lives in its own repo/folder alongside
# this one; bring it up from there:   ../meridian-shop/start.sh
# =============================================================================
set -e

bash platform/start.sh

echo "▶ Starting Edge MCP Servers..."
cd edge_mcp_servers
docker compose --progress=quiet up -d --build
cd ..

echo ""
echo "✅ Platform + edge tool servers running."
echo "   Connect a cluster in the console (http://localhost:3002). To test against"
echo "   the reference client, bring up ../meridian-shop (./start.sh) separately."
