#!/bin/bash
# ════════════════════════════════════════════════════════════════════════════
#  Target Client — One-Click Startup Script (Docker Desktop Native)
#
#  Usage (from Git Bash):
#    ./start.sh              Build images, inject to K8s, and deploy
#    ./start.sh --no-build   Deploy only (skip Docker builds & injection)
#    ./start.sh --down       Tear down the entire system
# ════════════════════════════════════════════════════════════════════════════

set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"

# ── Colors ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
GRAY='\033[0;37m'
NC='\033[0m'

step()  { echo -e "\n${CYAN}▶ $1${NC}"; }
ok()    { echo -e "  ${GREEN}✓ $1${NC}"; }
warn()  { echo -e "  ${YELLOW}⚠ $1${NC}"; }
err()   { echo -e "  ${RED}✗ $1${NC}"; }

# ── Parse args ──────────────────────────────────────────────────────────────
NO_BUILD=false
DOWN=false
for arg in "$@"; do
    case "$arg" in
        --no-build) NO_BUILD=true ;;
        --down)     DOWN=true ;;
    esac
done

# ── Tear-down mode ──────────────────────────────────────────────────────────
if $DOWN; then
    step "Tearing down Target Client system"
    kubectl delete namespace demo-app --ignore-not-found 2>/dev/null || true
    # Clean up temp tars if they exist
    rm -f "$ROOT/"*.tar
    ok "Namespace demo-app deleted"
    echo -e "\n${GREEN}Done. All Target Client resources removed.${NC}\n"
    exit 0
fi

# ── Banner ──────────────────────────────────────────────────────────────────
echo ""
echo -e "${MAGENTA}╔═══════════════════════════════════════════════════════╗${NC}"
echo -e "${MAGENTA}║       TARGET CLIENT — ONE-CLICK STARTUP SCRIPT       ║${NC}"
echo -e "${MAGENTA}╚═══════════════════════════════════════════════════════╝${NC}"

# ── Pre-flight checks ──────────────────────────────────────────────────────
step "Pre-flight checks"

if docker version --format '{{.Server.Version}}' > /dev/null 2>&1; then
    ok "Docker Engine is running"
else
    err "Docker Desktop is not running. Please start it and try again."
    exit 1
fi

if kubectl cluster-info > /dev/null 2>&1; then
    ok "Kubernetes cluster is reachable"
else
    err "Kubernetes is not reachable. Ensure it is enabled in Docker Desktop."
    exit 1
fi

# ── Build & Inject Docker images ───────────────────────────────────────────
if ! $NO_BUILD; then
    step "Building & Injecting Docker images (this ensures latest code is used)"

    declare -A IMAGES=(
        ["demo-api-gateway"]="$ROOT/services/api-gateway"
        ["demo-checkout-service"]="$ROOT/services/checkout-service"
        ["demo-inventory-service"]="$ROOT/services/inventory-service"
        ["demo-load-generator"]="$ROOT/load-generator"
        ["demo-chaos-panel"]="$ROOT/chaos-panel"
    )

    for name in "${!IMAGES[@]}"; do
        path="${IMAGES[$name]}"
        echo -ne "  ${GRAY}Processing ${name}...${NC}"

        # 1. Build the image locally
        docker build -t "${name}:latest" "$path" > /dev/null 2>&1
        
        # 2. Save it to a tar archive
        docker save "${name}:latest" -o "${ROOT}/${name}.tar"
        
        # 3. Copy into the K8s node container
        docker cp "${ROOT}/${name}.tar" desktop-control-plane:/${name}.tar
        
        # 4. Import the tar into Kubernetes internal engine (using // to bypass GitBash translation)
        docker exec desktop-control-plane ctr -n k8s.io images import //${name}.tar > /dev/null 2>&1
        
        # Cleanup tar
        rm -f "${ROOT}/${name}.tar"

        echo -e " ${GREEN}built & injected!${NC}"
    done
    ok "All 5 images successfully injected into Kubernetes"
else
    warn "Skipping image builds and injection (--no-build flag)"
fi

# ── Deploy infrastructure ──────────────────────────────────────────────────
step "Deploying infrastructure"
kubectl apply -f "$ROOT/k8s/namespace.yaml" > /dev/null 2>&1
kubectl apply -f "$ROOT/k8s/monitoring/" > /dev/null 2>&1
ok "Monitoring stack applied"

# ── Deploy core services ───────────────────────────────────────────────────
kubectl apply -f "$ROOT/k8s/services.yaml" > /dev/null 2>&1
kubectl apply -f "$ROOT/k8s/chaos-panel.yaml" > /dev/null 2>&1
ok "Core Application & Chaos Panel updated"

# ── Force code synchronization ─────────────────────────────────────────────
# If the pods are already running, Kubernetes won't automatically restart them 
# just because the image store changed. We force a rollout restart so they grab the new code.
kubectl rollout restart deployment -n demo-app api-gateway checkout-service inventory-service load-generator chaos-panel > /dev/null 2>&1 || true
ok "Forced pods to restart with newest code!"


# ── Wait for pods to be ready ──────────────────────────────────────────────
step "Waiting for all pods to become ready (timeout: 180s)"

TIMEOUT=180
ELAPSED=0
ALL_READY=false

while ! $ALL_READY && [ $ELAPSED -lt $TIMEOUT ]; do
    sleep 3
    ELAPSED=$((ELAPSED + 3))

    PODS=$(kubectl get pods -n demo-app --no-headers 2>/dev/null || true)
    if [ -z "$PODS" ]; then continue; fi

    TOTAL=$(echo "$PODS" | wc -l | tr -d ' ')
    READY=$(echo "$PODS" | grep "Running" | grep "1/1" | wc -l | tr -d ' ')

    if [ "$TOTAL" -gt 0 ]; then
        PCT=$((READY * 100 / TOTAL))
    else
        PCT=0
    fi

    echo -ne "\r  ${GRAY}Pods: ${READY}/${TOTAL} ready (${PCT}%) — ${ELAPSED}s elapsed${NC}  "

    if [ "$READY" -eq "$TOTAL" ] && [ "$TOTAL" -gt 0 ]; then
        ALL_READY=true
    fi
done

echo ""

if $ALL_READY; then
    ok "All pods are running!"
else
    warn "Some pods are not ready yet. Current status:"
    echo ""
    kubectl get pods -n demo-app 2>/dev/null
    echo ""
fi

# ── Print access summary ───────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔═══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║              TARGET CLIENT IS RUNNING                    ║${NC}"
echo -e "${GREEN}╠═══════════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║                                                           ║${NC}"
echo -e "${GREEN}║  🎮  Chaos Panel       ${NC}http://localhost:8888${GREEN}              ║${NC}"
echo -e "${GREEN}║                                                           ║${NC}"
echo -e "${GREEN}║  📡  API Gateway       ${NC}http://localhost:8000${GREEN}              ║${NC}"
echo -e "${GREEN}║  💳  Checkout Service  ${NC}http://localhost:8001${GREEN}              ║${NC}"
echo -e "${GREEN}║  📦  Inventory Service ${NC}http://localhost:8002${GREEN}              ║${NC}"
echo -e "${GREEN}║  🔄  Load Generator    ${NC}http://localhost:8003${GREEN}              ║${NC}"
echo -e "${GREEN}║                                                           ║${NC}"
echo -e "${GREEN}║  📈  Prometheus        ${NC}http://localhost:9090${GREEN}              ║${NC}"
echo -e "${GREEN}║  📊  Grafana           ${NC}http://localhost:3001${GREEN}              ║${NC}"
echo -e "${GREEN}║  🔔  Alertmanager      ${NC}http://localhost:9093${GREEN}              ║${NC}"
echo -e "${GREEN}║  📝  Loki              ${NC}http://localhost:3100${GREEN}              ║${NC}"
echo -e "${GREEN}║                                                           ║${NC}"
echo -e "${GREEN}╠═══════════════════════════════════════════════════════════╣${NC}"
echo -e "${YELLOW}║  Tear down:   ./start.sh --down                          ║${NC}"
echo -e "${YELLOW}║  Rebuild:     ./start.sh                                 ║${NC}"
echo -e "${YELLOW}║  No rebuild:  ./start.sh --no-build                      ║${NC}"
echo -e "${GREEN}╚═══════════════════════════════════════════════════════════╝${NC}"
echo ""
