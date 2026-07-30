#!/usr/bin/env bash
set -e

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}🚀 Starting SRE SaaS Platform...${NC}"

# Check for .env file at project root
if [ ! -f "$repo_root/.env" ]; then
    echo -e "${YELLOW}⚠️  .env file not found. Creating from .env.example...${NC}"
    if [ ! -f "$repo_root/.env.example" ]; then
        echo -e "${RED}❌ Missing $repo_root/.env.example. Create it first.${NC}"
        exit 1
    fi
    cp "$repo_root/.env.example" "$repo_root/.env"
    echo -e "${GREEN}✅ .env created. Edit it to set SECRET_KEY and other values.${NC}"
fi

if ! command -v docker &> /dev/null; then
    echo -e "${RED}❌ Docker is not installed.${NC}"
    exit 1
fi

# Load the repo-root environment so docker compose sees the same values
# regardless of whether it is invoked from the repo root or platform/.
set -a
source "$repo_root/.env"
set +a

echo -e "${GREEN}📦 Building SaaS Platform...${NC}"
cd "$script_dir"
docker compose -f docker-compose.yaml up -d --build

echo -e "${GREEN}⏳ Waiting for health checks...${NC}"
sleep 5
docker compose -f docker-compose.yaml ps

echo -e ""
echo -e "${GREEN}✅ SaaS Platform Running!${NC}"
echo -e ""
echo -e "   🖥️  ${YELLOW}Dashboard:${NC}    http://localhost:3002"
echo -e "   🧠  ${YELLOW}API Server:${NC}   http://localhost:8080/docs"
echo -e ""
echo -e "   👉 To connect a customer cluster: see customer/ directory"
echo -e "   👉 To stop: ./stop.sh"
echo -e "   👉 Logs: docker compose -f platform/docker-compose.yaml logs -f sre-agent-api"
echo -e ""
