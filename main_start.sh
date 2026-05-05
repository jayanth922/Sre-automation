#!/bin/bash

bash Target_Client/start.sh
bash platform/start.sh
cd edge_mcp_servers && docker compose up -d
cd ..
docker compose ps
kubectl get all 