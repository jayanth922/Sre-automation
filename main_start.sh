#!/bin/bash

bash Target_Client/start.sh
bash platform/start.sh

echo "▶ Starting Edge MCP Servers..."
cd edge_mcp_servers
docker compose --progress=quiet up -d --build
cd ..

# echo "▶ Running MCP Server Smoke Test..."
# if command -v uv >/dev/null 2>&1; then
# 	uv run python test_mcp_servers.py
# else
# 	python test_mcp_servers.py
# fi