#!/usr/bin/env bash
set -euo pipefail

service_account_namespace="${1:-sentinel}"
target_namespace="${2:-default}"
other_namespace="${3:-kube-system}"
actuator="system:serviceaccount:${service_account_namespace}:sentinel-actuator"

assert_allowed() {
  if ! kubectl auth can-i --as="$actuator" "$@" >/dev/null; then
    echo "expected allow: kubectl auth can-i $*" >&2
    exit 1
  fi
}

assert_denied() {
  if kubectl auth can-i --as="$actuator" "$@" >/dev/null; then
    echo "expected deny: kubectl auth can-i $*" >&2
    exit 1
  fi
}

assert_allowed delete pods --namespace "$target_namespace"
assert_denied delete services --namespace "$target_namespace"
assert_denied delete pods --namespace "$other_namespace"
assert_denied delete nodes

echo "Live actuator RBAC checks passed"
