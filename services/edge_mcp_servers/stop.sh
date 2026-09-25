#!/usr/bin/env bash
set -e

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${YELLOW}🛑 Stopping Edge Agent...${NC}"
cd "$script_dir"
docker compose -f docker-compose.yaml down
echo -e "${GREEN}✅ Edge Agent stopped.${NC}"
