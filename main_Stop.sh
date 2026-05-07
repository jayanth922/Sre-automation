#!/bin/bash

bash edge_mcp_servers/stop.sh
bash platform/stop.sh
bash Target_Client/start.sh --down