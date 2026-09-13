#!/usr/bin/env bash
# Runs on every Codespace start/resume (wired into .devcontainer/devcontainer.json's
# postStartCommand, before watch_meridian_deploy.sh). Docker-in-Docker's ephemeral
# storage does not survive a Codespace stop/resume, so k3s's *process* dies even
# though its on-disk state (etcd, pod specs) persists — this script brings it back
# and re-wires the one thing that silently breaks along with it: Alertmanager's
# webhook to the platform.
#
# Alertmanager's webhook used to point at a `cloudflared` quick tunnel
# (trycloudflare.com). That was fragile by construction: a bare background
# process with no supervisor, and a brand-new random hostname every time it's
# restarted — so it required manual re-wiring after every Codespace resume.
# Pods can reach the platform's `sre-agent-api` directly via the k3s node's own
# internal IP (it's on the same Docker network as the host-published port), so
# this script points the webhook there instead — no tunnel, no public exposure,
# and it self-heals if that IP ever changes across resumes.
set -uo pipefail

LOG_PREFIX="[codespace-boot]"
log() { echo "$LOG_PREFIX $(date -Is) $*"; }

NAMESPACE="${MERIDIAN_NAMESPACE:-meridian}"
SRE_AGENT_PORT="${SRE_AGENT_PORT:-8080}"

if ! pgrep -f "k3s server" > /dev/null; then
  # --node-ip is not optional here. k3s persists the node's InternalIP in its
  # datastore, but the Codespace gets a new eth0 address on most resumes, so
  # without it k3s starts against the *stored* IP, finds no interface holding
  # it, and kills itself seconds later with "failed to start networking:
  # unable to initialize network policy controller: error getting node subnet".
  # That looks like a crash long after this script has already logged success.
  HOST_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')
  if [ -n "$HOST_IP" ]; then
    log "k3s not running, starting it (node-ip $HOST_IP)"
    NODE_IP_ARG="--node-ip $HOST_IP"
  else
    log "k3s not running, starting it (could not detect host IP; letting k3s choose)"
    NODE_IP_ARG=""
  fi
  # setsid is required: a plain `... & disown` still dies when the Codespace's
  # postStartCommand shell session tears down, since it stays in that session.
  # shellcheck disable=SC2086  # NODE_IP_ARG must word-split into two args
  sudo setsid nohup k3s server --docker $NODE_IP_ARG > /tmp/k3s.log 2>&1 < /dev/null &
else
  log "k3s already running"
fi

log "waiting for the k3s API server"
ready=false
for _ in $(seq 1 60); do
  if sudo k3s kubectl get nodes > /dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 2
done

if [ "$ready" != true ]; then
  log "k3s API server did not come up in time — skipping k8s-dependent steps"
else
  log "k3s API server is up"

  NODE_IP=$(sudo k3s kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || true)
  if [ -z "$NODE_IP" ]; then
    log "could not determine node IP — skipping alertmanager webhook patch"
  else
    WEBHOOK_URL="http://${NODE_IP}:${SRE_AGENT_PORT}/api/v1/alerts/webhook"
    CURRENT_URL=$(
      sudo k3s kubectl get configmap alertmanager-config -n "$NAMESPACE" \
        -o jsonpath='{.data.alertmanager\.yml}' 2>/dev/null \
        | grep -o 'url: "[^"]*"' | head -1 | sed 's/url: "//;s/"//'
    )
    if [ -z "$CURRENT_URL" ]; then
      log "alertmanager-config not found in namespace $NAMESPACE — skipping"
    elif [ "$CURRENT_URL" = "$WEBHOOK_URL" ]; then
      log "alertmanager webhook already points at $WEBHOOK_URL"
    else
      log "repointing alertmanager webhook: $CURRENT_URL -> $WEBHOOK_URL"
      sudo k3s kubectl get configmap alertmanager-config -n "$NAMESPACE" \
        -o jsonpath='{.data.alertmanager\.yml}' > /tmp/alertmanager.yml
      sed -i "s|url: \"[^\"]*\"|url: \"${WEBHOOK_URL}\"|" /tmp/alertmanager.yml
      sudo k3s kubectl create configmap alertmanager-config -n "$NAMESPACE" \
        --from-file=alertmanager.yml=/tmp/alertmanager.yml --dry-run=client -o yaml \
        | sudo k3s kubectl apply -f -
      sudo k3s kubectl rollout restart deployment/alertmanager -n "$NAMESPACE"
      sudo k3s kubectl rollout status deployment/alertmanager -n "$NAMESPACE" --timeout=90s \
        || log "alertmanager rollout did not finish in time — check it manually"
    fi
  fi
fi

if [ -d /workspaces/Sre-automation/platform ]; then
  log "ensuring platform docker-compose stack is up"
  (cd /workspaces/Sre-automation/platform && docker compose --env-file ../.env up -d) \
    >> /tmp/codespace_boot_compose.log 2>&1
fi

# Re-check k3s at the very end rather than trusting the readiness loop above.
# k3s can answer the API for a few seconds and *then* self-terminate (see the
# --node-ip note), which previously let this script log "done" over a cluster
# that was already gone — the failure only surfaced later as every kubectl
# call refusing to connect.
if pgrep -f "k3s server" > /dev/null && sudo k3s kubectl get nodes > /dev/null 2>&1; then
  log "done (k3s healthy)"
else
  log "WARNING: k3s is not running at end of boot — see /tmp/k3s.log"
  exit 1
fi
