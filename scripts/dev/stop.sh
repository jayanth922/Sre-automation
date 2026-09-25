#!/usr/bin/env bash
# Stop the Sentinel PLATFORM and its edge tool servers only.
# Client environments are independent (see examples/ for overlays).

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

bash "$repo_root/services/edge_mcp_servers/stop.sh"
bash "$repo_root/infra/local/stop.sh"
