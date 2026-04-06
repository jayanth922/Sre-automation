---
title: RB-002 | API Gateway | High Latency
runbook_id: RB-002
service: api-gateway
incident_type: latency
severity: High
status: Draft
owner_team: SRE
primary_owner: mihawk
tags:
  - api-gateway
  - latency
  - routing
  - kubernetes
  - incident-response
last_reviewed: ""
version: "0.1"
source_of_truth: Local Markdown
escalation_channel: application-oncall / pager
related_systems:
  - checkout-service
  - inventory-service
  - prometheus
alert_name: ApiGatewayHighLatency
impacted_environment: production
service_tier: tier-0
---

# RB-002 | API Gateway | High Latency

## Summary

RB-002 covers elevated response times at api-gateway when requests to checkout-service or inventory-service slow down the request path. Use this runbook when the gateway is healthy but user-facing requests are taking too long to complete or are timing out under normal load.

## Metadata

- Runbook ID: RB-002
- Service: api-gateway
- Incident Type: latency
- Severity: High
- Status: Draft
- Owner Team: SRE
- Primary Owner: mihawk
- Related Systems: checkout-service, inventory-service, prometheus
- Alert Name: ApiGatewayHighLatency
- Escalation Channel: application-oncall / pager
- Impacted Environment: production
- Service Tier: tier-0
- Version: 0.1
- Source of Truth: Local Markdown

## When to Use

Use this runbook when any of the following are true:

1. api-gateway p95 latency is above the expected threshold.
2. Requests to `/checkout` or `/inventory` are timing out.
3. Prometheus fires ApiGatewayHighLatency or a similar latency alert.
4. The gateway is healthy, but downstream calls are slow or blocked.
5. Users report slow page loads or intermittent request failures.

## Preconditions and Required Access

Before making changes, confirm the responder has:

1. Read access to the Kubernetes cluster and demo-app namespace.
2. Permission to inspect api-gateway, checkout-service, and inventory-service logs.
3. Permission to query Prometheus.
4. Permission to restart a deployment if needed.

## Safety Checks

Before changing anything:

1. Confirm whether latency affects all gateway routes or only checkout and inventory.
2. Confirm whether the slowdown is in the gateway itself or in downstream calls.
3. Confirm whether any pods are restarting or resource constrained.
4. Confirm whether a recent rollout or manifest change preceded the issue.

## Detection Signals

Look for these signals together:

1. p95 latency rising on api-gateway.
2. Timeout errors in gateway logs.
3. Slow responses from checkout-service or inventory-service.
4. Increased request duration in Prometheus.
5. Elevated upstream error counts or retry behavior.

## Step-by-Step Resolution

1. Measure the current gateway latency and error shape.

   Run:

       kubectl logs -n demo-app deploy/api-gateway --tail=200
       kubectl top pod -n demo-app -l app=api-gateway

   Expected result: you can tell whether the gateway itself is saturated or waiting on downstream calls.

2. Test downstream service responsiveness from inside the cluster.

   Run:

       kubectl exec -n demo-app deploy/api-gateway -- sh -lc 'time curl -fsS http://checkout-service:8001/health'
       kubectl exec -n demo-app deploy/api-gateway -- sh -lc 'time curl -fsS http://inventory-service:8002/health'

   Expected result: downstream calls should return quickly. If one is slow, that service is likely the bottleneck.

3. Check service endpoints and pod readiness.

   Run:

       kubectl get endpoints -n demo-app api-gateway checkout-service inventory-service -o yaml
       kubectl get pods -n demo-app -l app=api-gateway,app=checkout-service,app=inventory-service -o wide

   Expected result: all services have valid endpoints and the pods are Ready.

4. Inspect recent rollouts and application logs for regression clues.

   Run:

       kubectl rollout history deployment -n demo-app api-gateway
       kubectl rollout history deployment -n demo-app checkout-service
       kubectl rollout history deployment -n demo-app inventory-service

   Expected result: you can identify whether latency began after a deployment change.

5. Recover the bottlenecked service or the gateway path.

   If the gateway is waiting on a single slow downstream service, restart that service first.

   Run one of:

       kubectl rollout restart deployment -n demo-app checkout-service
       kubectl rollout restart deployment -n demo-app inventory-service

   If the gateway itself appears unhealthy, restart it after downstream health is confirmed:

       kubectl rollout restart deployment -n demo-app api-gateway

## Verification

The incident is resolved when all of the following are true:

1. api-gateway latency returns to the expected baseline.
2. Downstream health checks complete quickly from inside the cluster.
3. Gateway logs no longer show timeout or upstream wait errors.
4. Prometheus alert ApiGatewayHighLatency clears.

## Rollback or Recovery

Use rollback or recovery only if a recent rollout introduced the delay.

Safe recovery options:

1. Roll back api-gateway to the last known-good revision.
2. Roll back the slow downstream service if it regressed after deployment.
3. Reapply the known-good target-client manifest if endpoints or ports drifted.

Do not:

1. Mask the issue by increasing client timeouts without finding the slow service.
2. Restart multiple deployments at once without confirming the slow hop.

## Escalation Path

Escalate when any of the following are true:

1. Latency remains high after checking downstream services.
2. The gateway is healthy but a dependency remains slow or unavailable.
3. Timeouts continue after rollback.

Notify:

1. application-oncall / pager
2. SRE owner mihawk
3. platform owner if the issue appears node or cluster related

## Related Runbooks

- RB-001 | Checkout Service | High Error Rate
- RB-003 | Inventory Service | Slow Queries
- RB-006 | Downstream Dependency Failure
- RB-010 | Platform | DNS / Networking Issue

## Change Log

- 0.1 Initial draft created for local Markdown runbooks.
- Steps focus on gateway-visible symptoms and service-to-service remediation.