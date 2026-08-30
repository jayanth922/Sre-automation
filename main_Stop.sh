#!/bin/bash
# Stop the Sentinel PLATFORM and its edge tool servers only.
# Client environments are independent (see deploy/examples/ for overlays).

bash edge_mcp_servers/stop.sh
bash platform/stop.sh
