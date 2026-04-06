---
title: RB-006 | Downstream Dependency Failure
runbook_id: RB-006
service: api-gateway
incident_type: dependency failure
severity: High
status: Draft
owner_team: SRE
primary_owner: mihawk
tags:
  - dependency
  - api-gateway
  - checkout
  - inventory
  - kubernetes
  - incident-response
last_reviewed: ""
version: "0.1"
source_of_truth: Local Markdown
escalation_channel: application-oncall / pager
related_systems:
  - checkout-service
  - inventory-service
  - dns
alert_name: DownstreamDependencyFailure
impacted_environment: production
service_tier: tier-0
---

# RB-006 | Downstream Dependency Failure

## Summary

RB-006 covers failures where api-gateway cannot complete a request because checkout-service or inventory-service is unavailable, slow, or returning errors. Use this runbook when the gateway itself is healthy but downstream dependency behavior is causing user-facing failures.

## Metadata

- Runbook ID: RB-006
- Service: api-gateway
- Incident Type: dependency failure
- Severity: High
- Status: Draft
- Owner Team: SRE
- Primary Owner: mihawk
- Related Systems: checkout-service, inventory-service, dns
- Alert Name: DownstreamDependencyFailure
- Escalation Channel: application-oncall / pager
- Impacted Environment: production
- Service Tier: tier-0
- Version: 0.1
- Source of Truth: Local Markdown

## When to Use

Use this runbook when any of the following are true:

1. api-gateway logs show upstream timeout, connection, or 5xx errors from downstream services.
2. checkout-service or inventory-service is unhealthy, slow, or returning errors.
3. Requests fail only when the gateway tries to call one specific dependency.
4. Prometheus alerts point to a downstream service rather than the gateway.
5. The dependency path is broken even though the gateway pod itself is Running.

## Preconditions and Required Access

Before making changes, confirm the responder has:

1. Read access to the Kubernetes cluster and demo-app namespace.
2. Permission to inspect gateway and downstream service logs.
3. Permission to run kubectl exec into api-gateway.
4. Permission to restart the affected deployment if recovery is required.

## Safety Checks

Before changing anything:

1. Confirm which dependency is failing.
2. Confirm whether the issue is error, latency, or reachability.
3. Confirm whether endpoints are still present for the dependency.
4. Confirm whether a recent rollout or DNS change preceded the problem.

## Detection Signals

Look for these signals together:

1. api-gateway upstream errors.
2. Slow or failed health checks to checkout-service or inventory-service.
3. Missing endpoints or unhealthy pods for the dependency.
4. Prometheus alerts involving the dependency service.
5. Logs showing connection refused, no such host, timeout, or 5xx responses.

## Step-by-Step Resolution

1. Identify the failing dependency.

   Run:

       kubectl logs -n demo-app deploy/api-gateway --tail=200
       kubectl get pods -n demo-app -l app=checkout-service,app=inventory-service -o wide
       kubectl get endpoints -n demo-app checkout-service inventory-service -o yaml

   Expected result: you can identify which dependency is failing and whether its endpoints are present.

2. Test the dependency directly from inside the cluster.

   Run:

       kubectl exec -n demo-app deploy/api-gateway -- sh -lc 'curl -fsS http://checkout-service:8001/health'
       kubectl exec -n demo-app deploy/api-gateway -- sh -lc 'curl -fsS http://inventory-service:8002/health'

   Expected result: the failing dependency is obvious from the direct health check.

3. Inspect the dependency logs and pod state.

   Run one of:

       kubectl logs -n demo-app deploy/checkout-service --tail=200
       kubectl logs -n demo-app deploy/inventory-service --tail=200

   Also check:

       kubectl describe pod -n demo-app -l app=checkout-service
       kubectl describe pod -n demo-app -l app=inventory-service

4. Recover the dependency or gateway path.

   Run one of:

       kubectl rollout restart deployment -n demo-app checkout-service
       kubectl rollout restart deployment -n demo-app inventory-service
       kubectl rollout restart deployment -n demo-app api-gateway

   If the problem began after a known rollout, use the last known-good revision instead of a restart.

## Verification

The incident is resolved when all of the following are true:

1. api-gateway can reach the downstream service again.
2. The dependency returns successful health checks.
3. Gateway upstream errors clear.
4. Prometheus alerts related to the dependency clear.

## Rollback or Recovery

Use rollback or recovery only if the issue began after a known deployment or config change.

Safe recovery options:

1. Roll back the failing dependency to the last known-good revision.
2. Restart the dependency after confirming endpoints are intact.
3. Reapply the target-client manifest if selectors or ports drifted.

Do not:

1. Restart every service at once.
2. Change unrelated service routes to hide the failure.

## Escalation Path

Escalate when any of the following are true:

1. The dependency remains unhealthy after restart or rollback.
2. The root cause appears to be cluster networking or DNS.
3. The gateway still fails even though the dependency is healthy.

Notify:

1. application-oncall / pager
2. SRE owner mihawk
3. platform owner if the issue appears network or cluster related

## Related Runbooks

- RB-001 | Checkout Service | High Error Rate
- RB-002 | API Gateway | High Latency
- RB-003 | Inventory Service | Slow Queries
- RB-010 | Platform | DNS / Networking Issue

## Change Log

- 0.1 Initial draft created for local Markdown runbooks.
- This runbook covers gateway-visible symptoms caused by downstream service failure.