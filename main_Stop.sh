#!/bin/bash
# Stop the Sentinel PLATFORM and its edge tool servers only.
# The client environment (Meridian Commerce) is independent — tear it down from
# its own repo:  ../meridian-shop/start.sh --down

bash edge_mcp_servers/stop.sh
bash platform/stop.sh
