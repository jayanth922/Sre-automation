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
fi

# SECRET_KEY, CREDENTIAL_ENCRYPTION_KEY, and MCP_SERVICE_TOKEN are internal
# secrets, not real credentials — safe to generate whenever they're still
# blank/placeholder, whether .env was just created above or already existed
# (e.g. from a manual `cp .env.example .env`). Checked by current value, not
# by whether this script created the file, so it's idempotent and never
# overwrites a real key you've already set. The LLM key is the one real
# credential and is deliberately NOT generated here: the platform boots
# without it (see sre_agent/provider_config.py) and it's set per-cluster
# later from the dashboard's Settings page, or in this .env up front.
GENERATED_ANY=0
if grep -qE '^SECRET_KEY="?(sre_platform_dev_key_change_in_production)?"?$' "$repo_root/.env"; then
    GENERATED_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    sed -i.bak "s|^SECRET_KEY=.*|SECRET_KEY=\"${GENERATED_SECRET_KEY}\"|" "$repo_root/.env"
    GENERATED_ANY=1
fi
if grep -qE '^CREDENTIAL_ENCRYPTION_KEY=""$' "$repo_root/.env"; then
    GENERATED_ENC_KEY="$(python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')"
    sed -i.bak "s|^CREDENTIAL_ENCRYPTION_KEY=\"\"\$|CREDENTIAL_ENCRYPTION_KEY=\"${GENERATED_ENC_KEY}\"|" "$repo_root/.env"
    GENERATED_ANY=1
fi
if grep -qE '^MCP_SERVICE_TOKEN=""$' "$repo_root/.env"; then
    GENERATED_MCP_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    sed -i.bak "s|^MCP_SERVICE_TOKEN=\"\"\$|MCP_SERVICE_TOKEN=\"${GENERATED_MCP_TOKEN}\"|" "$repo_root/.env"
    GENERATED_ANY=1
fi
# Langfuse hard-requires both at boot (ENCRYPTION_KEY as a 64-hex-char/32-byte
# key) and exits immediately if either is blank — while everything else in
# the stack has no such requirement and stays healthy, which is why a blank
# .env.example default here shows up as "only the langfuse containers exited."
if grep -qE '^LANGFUSE_SALT=""$' "$repo_root/.env"; then
    GENERATED_LANGFUSE_SALT="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    sed -i.bak "s|^LANGFUSE_SALT=\"\"\$|LANGFUSE_SALT=\"${GENERATED_LANGFUSE_SALT}\"|" "$repo_root/.env"
    GENERATED_ANY=1
fi
if grep -qE '^LANGFUSE_ENCRYPTION_KEY=""$' "$repo_root/.env"; then
    GENERATED_LANGFUSE_ENC_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    sed -i.bak "s|^LANGFUSE_ENCRYPTION_KEY=\"\"\$|LANGFUSE_ENCRYPTION_KEY=\"${GENERATED_LANGFUSE_ENC_KEY}\"|" "$repo_root/.env"
    GENERATED_ANY=1
fi
rm -f "$repo_root/.env.bak"

if [ "$GENERATED_ANY" = "1" ]; then
    echo -e "${GREEN}✅ .env internal secrets auto-generated (SECRET_KEY / CREDENTIAL_ENCRYPTION_KEY / MCP_SERVICE_TOKEN / LANGFUSE_SALT / LANGFUSE_ENCRYPTION_KEY, whichever were still blank).${NC}"
    echo -e "${YELLOW}   No LLM key yet? That's fine — add ANTHROPIC_API_KEY (or GOOGLE_API_KEY) later, in .env or per-cluster in the dashboard.${NC}"
fi

# edge_mcp_servers is a second, independently-deployable Docker Compose
# project with its own .env — main_start.sh brings it up directly, so
# bootstrap it here too, and sync MCP_SERVICE_TOKEN so the two stacks trust
# each other without a manual copy-paste.
EDGE_DIR="$repo_root/edge_mcp_servers"
if [ ! -f "$EDGE_DIR/.env" ] && [ -f "$EDGE_DIR/.env.example" ]; then
    cp "$EDGE_DIR/.env.example" "$EDGE_DIR/.env"
    echo -e "${GREEN}✅ edge_mcp_servers/.env created.${NC}"
    echo -e "${YELLOW}   Set a real GITHUB_TOKEN (and GITHUB_REPO) in edge_mcp_servers/.env before continuing.${NC}"
fi

# Sync by current value, not by whether the file above was just created --
# if this .env was created on an earlier run before the root MCP_SERVICE_TOKEN
# existed yet (e.g. this script failed partway through back then), it would
# otherwise be stuck blank forever since the block above only runs once.
if [ -f "$EDGE_DIR/.env" ] && grep -qE '^MCP_SERVICE_TOKEN=$' "$EDGE_DIR/.env"; then
    MCP_TOKEN_VALUE="$(grep -E '^MCP_SERVICE_TOKEN=' "$repo_root/.env" | head -1 | cut -d'"' -f2)"
    if [ -n "$MCP_TOKEN_VALUE" ]; then
        sed -i.bak "s|^MCP_SERVICE_TOKEN=.*|MCP_SERVICE_TOKEN=${MCP_TOKEN_VALUE}|" "$EDGE_DIR/.env"
        rm -f "$EDGE_DIR/.env.bak"
        echo -e "${GREEN}✅ edge_mcp_servers/.env MCP_SERVICE_TOKEN synced from platform .env.${NC}"
    fi
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

# Stamp clean local builds with their exact revision. Dirty worktrees remain
# deliberately unknown so their incident runs cannot be mistaken for
# reproducible evaluation evidence.
if [ -z "${SENTINEL_CODE_SHA:-}" ] && git -C "$repo_root" rev-parse HEAD >/dev/null 2>&1; then
    if [ -n "$(git -C "$repo_root" status --porcelain)" ]; then
        export SENTINEL_CODE_SHA=unknown
    else
        export SENTINEL_CODE_SHA="$(git -C "$repo_root" rev-parse HEAD)"
    fi
fi

# Fail closed on SECRET_KEY / an unsupported LLM_PROVIDER value before compose
# build or migrations; a merely missing/placeholder LLM key only warns
# (provider_config.py) and doesn't block startup — set it later.
echo -e "${GREEN}🔎 Validating startup configuration...${NC}"
if ! (
    cd "$repo_root"
    PYTHONPATH="$repo_root" python3 -m sre_agent.provider_config
); then
    echo -e "${RED}❌ Startup configuration is invalid. Fix .env (see messages above) and retry.${NC}"
    echo -e "${YELLOW}   Supported LLM_PROVIDER: anthropic | gemini${NC}"
    exit 1
fi

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
echo -e "   👉 To connect a cluster's tools (Prometheus/Loki/GitHub/runbooks/exec): see edge_mcp_servers/"
echo -e "   👉 To stop: ./stop.sh"
echo -e "   👉 Logs: docker compose --env-file .env -f platform/docker-compose.yaml logs -f sre-agent-api"
echo -e ""
