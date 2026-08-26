#!/usr/bin/env bash
set -euo pipefail

chart_path="${1:-deploy/helm/sentinel}"
ingress_render="$(mktemp)"
port_forward_render="$(mktemp)"
trap 'rm -f "$ingress_render" "$port_forward_render"' EXIT

helm template sentinel "$chart_path" \
  --namespace sentinel \
  --set secrets.create=false \
  --set secrets.existingSecret=sentinel-ci-secrets \
  --set ingress.enabled=true >"$ingress_render"

helm template sentinel "$chart_path" \
  --namespace sentinel \
  --set secrets.create=false \
  --set secrets.existingSecret=sentinel-ci-secrets >"$port_forward_render"

awk '
  /name: NEXT_PUBLIC_WS_BASE/ { websocket_env = 1; next }
  websocket_env && /value: ""/ { empty_default = 1; websocket_env = 0 }
  /- path: \/ws$/ { websocket_path = 1; next }
  websocket_path && /name: sentinel-api$/ { websocket_api = 1 }
  /- path: \/$/ { root_path = 1; next }
  root_path && /name: sentinel-web$/ { root_web = 1 }
  END { exit !(empty_default && websocket_path && websocket_api && root_path && root_web) }
' "$ingress_render"

awk '
  /name: NEXT_PUBLIC_WS_BASE/ { websocket_env = 1; next }
  websocket_env && /value: "ws:\/\/localhost:8080"/ { port_forward_default = 1 }
  END { exit !port_forward_default }
' "$port_forward_render"

echo "Helm WebSocket routing checks passed"
