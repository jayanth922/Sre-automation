#!/usr/bin/env bash
set -e

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}Starting SRE Edge Relay...${NC}"

if [ ! -f "$script_dir/.env" ]; then
    echo -e "${RED}.env file not found. Copy .env.example and fill in your values:${NC}"
    echo -e "   cp .env.example .env"
    exit 1
fi

if ! command -v docker &> /dev/null; then
    echo -e "${RED}Docker is not installed.${NC}"
    exit 1
fi

echo -e "${GREEN}Building Edge Relay stack...${NC}"
cd "$script_dir"
docker compose -f docker-compose.yaml up -d --build

echo -e "${GREEN}Waiting for services...${NC}"
sleep 5
docker compose -f docker-compose.yaml ps

echo -e ""
echo -e "${GREEN}Edge Relay Running!${NC}"
echo -e ""
echo -e "   The MCP tool servers are now exposed on local ports (4000-4004)."
echo -e "   The SaaS platform will connect to them directly in local development."
echo -e ""
echo -e "   To stop: ./stop.sh"
echo -e "   Logs:    docker compose logs -f"
echo -e ""
