#!/bin/bash
# =============================================================================
# Sentinel — start the PLATFORM and its EDGE tool servers only.
#
# The monitored target is the CUSTOMER's own infrastructure. It is intentionally
# NOT started here and has no coupling to the platform lifecycle. To run the
# bundled demo workload (a self-contained sample cluster) for local testing,
# start it separately:   ./demo_target.sh
# =============================================================================
set -e

bash platform/start.sh

echo "▶ Starting Edge MCP Servers..."
cd edge_mcp_servers
docker compose --progress=quiet up -d --build
cd ..

echo ""
echo "✅ Platform + edge tool servers running."
echo "   Connect a cluster in the console (http://localhost:3002), or run"
echo "   ./demo_target.sh to bring up the bundled sample workload for testing."
