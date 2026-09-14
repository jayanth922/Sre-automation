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

# --node-ip is not optional here. k3s persists the node's InternalIP in its
# datastore, but the Codespace gets a new eth0 address on most resumes, so
# without it k3s starts against the *stored* IP, finds no interface holding
# it, and kills itself seconds later with "failed to start networking:
# unable to initialize network policy controller: error getting node subnet".
# That looks like a crash long after this script has already logged success.
HOST_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')
if [ -n "$HOST_IP" ]; then
  NODE_IP_ARG="--node-ip $HOST_IP"
else
  log "could not detect host IP; letting k3s choose its node IP"
  NODE_IP_ARG=""
fi

# How long k3s must keep answering before we believe it. It dies ~15s in when
# it dies at all, so this has to outlast that.
K3S_SETTLE_SECONDS="${K3S_SETTLE_SECONDS:-30}"

k3s_api_up() { sudo k3s kubectl get nodes > /dev/null 2>&1; }
k3s_alive() { pgrep -f "k3s server" > /dev/null; }

ready=false
# Two attempts, because --node-ip only fixes *kubelet*. kube-router (the
# network policy controller) reads the InternalIP off the **Node object**,
# which still carries the previous resume's address until kubelet patches it —
# so the first start after an IP change can serve the API for a few seconds
# and then exit with the "node subnet" error above. Kubelet patches the node
# on the way down, so the retry starts against a correct Node and sticks.
for attempt in 1 2; do
  if k3s_alive; then
    log "k3s already running"
  else
    log "starting k3s (attempt $attempt${HOST_IP:+, node-ip $HOST_IP})"
    # setsid is required: a plain `... & disown` still dies when the Codespace's
    # postStartCommand shell session tears down, since it stays in that session.
    # shellcheck disable=SC2086  # NODE_IP_ARG must word-split into two args
    sudo setsid nohup k3s server --docker $NODE_IP_ARG > /tmp/k3s.log 2>&1 < /dev/null &
  fi

  log "waiting for the k3s API server (attempt $attempt)"
  for _ in $(seq 1 60); do
    if k3s_api_up; then
      break
    fi
    sleep 2
  done

  # One successful probe proves nothing — that is exactly the window in which
  # k3s answers and then self-terminates. Watch it stay alive instead.
  log "confirming k3s stays up for ${K3S_SETTLE_SECONDS}s"
  settled=true
  for _ in $(seq 1 "$K3S_SETTLE_SECONDS"); do
    sleep 1
    if ! k3s_alive; then
      settled=false
      break
    fi
  done

  if [ "$settled" = true ] && k3s_api_up; then
    ready=true
    break
  fi
  log "k3s did not stay up on attempt $attempt — see /tmp/k3s.log"
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
  # Report the failure instead of swallowing it. The redirect is part of what
  # can fail: run under sudo against a log file the normal user owns and bash
  # aborts the command before compose ever starts, which previously looked
  # like a successful boot with no platform stack behind it.
  COMPOSE_LOG="${COMPOSE_LOG:-/tmp/codespace_boot_compose.log}"
  if ! (cd /workspaces/Sre-automation/platform && docker compose --env-file ../.env up -d) \
      >> "$COMPOSE_LOG" 2>&1; then
    log "WARNING: docker compose up did not succeed — see $COMPOSE_LOG"
  fi
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
