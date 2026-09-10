#!/usr/bin/env bash
# Polls meridian-shop's master branch for new commits and redeploys only the
# services whose source changed into the local `kind` cluster. This exists
# because the kind cluster only lives inside this codespace with no public
# ingress, so a GitHub-hosted Actions runner can't reach it to deploy on
# push — this loop is the in-codespace equivalent of that CD step.
set -uo pipefail

REPO_DIR="${MERIDIAN_REPO_DIR:-/workspaces/meridian-shop-deploy}"
REPO_URL="${MERIDIAN_REPO_URL:-https://github.com/jayanth922/meridian-shop.git}"
POLL_INTERVAL="${POLL_INTERVAL:-20}"
KIND_CLUSTER="${KIND_CLUSTER:-meridian}"
NAMESPACE="${MERIDIAN_NAMESPACE:-meridian}"
STATE_FILE="$REPO_DIR/.last_deployed_sha"
LOG_PREFIX="[watch-meridian-deploy]"

declare -A SERVICE_MAP=(
  [services/api-gateway]=api-gateway
  [services/checkout-service]=checkout-service
  [services/payment-service]=payment-service
  [services/inventory-service]=inventory-service
  [load-generator]=load-generator
  [mcp/meridian-signals]=meridian-signals
)

declare -A IMAGE_MAP=(
  [api-gateway]=meridian-api-gateway
  [checkout-service]=meridian-checkout-service
  [payment-service]=meridian-payment-service
  [inventory-service]=meridian-inventory-service
  [load-generator]=meridian-load-generator
  [meridian-signals]=meridian-signals
)

log() { echo "$LOG_PREFIX $(date -Is) $*"; }

if [ ! -d "$REPO_DIR/.git" ]; then
  log "cloning $REPO_URL into $REPO_DIR"
  git clone --quiet "$REPO_URL" "$REPO_DIR"
fi
cd "$REPO_DIR" || exit 1

if [ ! -f "$STATE_FILE" ]; then
  git rev-parse HEAD > "$STATE_FILE"
fi

log "starting, last deployed sha $(cat "$STATE_FILE")"

while true; do
  if git fetch --quiet origin master; then
    NEW_SHA=$(git rev-parse origin/master)
    OLD_SHA=$(cat "$STATE_FILE")

    if [ "$NEW_SHA" != "$OLD_SHA" ]; then
      log "new commit(s) detected: $OLD_SHA..$NEW_SHA"
      git reset --hard --quiet "$NEW_SHA"

      CHANGED=$(git diff --name-only "$OLD_SHA" "$NEW_SHA")
      deployed_any=false

      for path in "${!SERVICE_MAP[@]}"; do
        svc="${SERVICE_MAP[$path]}"
        if echo "$CHANGED" | grep -q "^${path}/"; then
          image="${IMAGE_MAP[$svc]}"
          log "rebuilding $svc ($image) from $path"
          if docker build -q -t "$image:latest" "$path" \
             && kind load docker-image "$image:latest" --name "$KIND_CLUSTER" \
             && kubectl rollout restart "deployment/$svc" -n "$NAMESPACE" \
             && kubectl rollout status "deployment/$svc" -n "$NAMESPACE" --timeout=120s; then
            log "deployed $svc successfully"
          else
            log "FAILED to deploy $svc — leaving previous image running"
          fi
          deployed_any=true
        fi
      done

      if echo "$CHANGED" | grep -q "^k8s/"; then
        log "k8s/ manifests changed — applying"
        kubectl apply -f k8s/ 2>&1 | sed "s/^/$LOG_PREFIX /"
        deployed_any=true
      fi

      if [ "$deployed_any" = false ]; then
        log "no deployable path changed (docs/CI-only commit) — skipping deploy"
      fi

      echo "$NEW_SHA" > "$STATE_FILE"
    fi
  else
    log "git fetch failed, will retry"
  fi

  sleep "$POLL_INTERVAL"
done
