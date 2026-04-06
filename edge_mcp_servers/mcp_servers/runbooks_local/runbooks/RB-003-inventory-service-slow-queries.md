---
title: RB-003 | Inventory Service | Slow Queries
runbook_id: RB-003
service: inventory-service
incident_type: latency
severity: Medium
status: Draft
owner_team: SRE
primary_owner: mihawk
tags:
  - inventory
  - latency
  - database
  - kubernetes
  - incident-response
last_reviewed: ""
version: "0.1"
source_of_truth: Local Markdown
escalation_channel: application-oncall / pager
related_systems:
  - api-gateway
  - checkout-service
  - prometheus
alert_name: InventorySlowQueries
impacted_environment: production
service_tier: tier-0
---

# RB-003 | Inventory Service | Slow Queries

## Summary

RB-003 covers elevated response time in inventory-service when item lookups or list operations become slow. Use this runbook when the service is healthy but query latency rises, item lookups lag, or the gateway starts waiting on inventory responses.

## Metadata

- Runbook ID: RB-003
- Service: inventory-service
- Incident Type: latency
- Severity: Medium
- Status: Draft
- Owner Team: SRE
- Primary Owner: mihawk
- Related Systems: api-gateway, checkout-service, prometheus
- Alert Name: InventorySlowQueries
- Escalation Channel: application-oncall / pager
- Impacted Environment: production
- Service Tier: tier-0
- Version: 0.1
- Source of Truth: Local Markdown

## When to Use

Use this runbook when any of the following are true:

1. inventory-service `/items` or `/items/{item_id}` requests are slow.
2. Prometheus fires InventorySlowQueries.
3. api-gateway latency increases while inventory is in the request path.
4. Logs show slow database query behavior or repeated low-stock warnings that correlate with latency.
5. The service still responds, but the response time is no longer acceptable.

## Preconditions and Required Access

Before making changes, confirm the responder has:

1. Read access to the Kubernetes cluster and demo-app namespace.
2. Permission to inspect inventory-service logs and metrics.
3. Permission to run kubectl exec into inventory-service if needed.
4. Permission to restart the deployment if recovery is required.

## Safety Checks

Before changing anything:

1. Confirm the latency is in inventory-service and not only in the gateway.
2. Confirm whether one item lookup or the whole list endpoint is slow.
3. Confirm whether the pod is resource constrained or restarting.
4. Confirm whether a recent rollout or config change preceded the slowdown.

## Detection Signals

Look for these signals together:

1. Slow response time on `/items` or `/items/{item_id}`.
2. Gateway latency rising when inventory is involved.
3. Logs showing slow query behavior or repeated warnings.
4. Prometheus alert InventorySlowQueries firing.
5. Pod restarts or resource pressure on inventory-service.

## Step-by-Step Resolution

1. Confirm the service is reachable and endpoints are healthy.

   Run:

       kubectl get pods -n demo-app -l app=inventory-service -o wide
       kubectl get svc -n demo-app inventory-service
       kubectl get endpoints -n demo-app inventory-service -o yaml

   Expected result: the pod is Running and Ready and the service has a valid endpoint.

2. Inspect the live request path and logs.

   Run:

       kubectl logs -n demo-app deploy/inventory-service --tail=200
       kubectl exec -n demo-app deploy/api-gateway -- sh -lc 'time curl -fsS http://inventory-service:8002/items'
       kubectl exec -n demo-app deploy/api-gateway -- sh -lc 'time curl -fsS http://inventory-service:8002/items/item-001'

   Expected result: the logs and timing confirm whether the delay is on list queries, single-item lookups, or both.

3. Check resource pressure.

   Run:

       kubectl top pod -n demo-app -l app=inventory-service
       kubectl describe pod -n demo-app -l app=inventory-service

   Expected result: the pod is not CPU-starved, memory-throttled, or repeatedly restarting.

4. Review recent rollout or manifest changes.

   Run:

       kubectl rollout history deployment -n demo-app inventory-service
       kubectl describe deploy -n demo-app inventory-service

   Expected result: you can confirm whether the slowdown began after a deployment change.

5. Recover if the service is stuck in a bad state.

   Run one of:

       kubectl rollout restart deployment -n demo-app inventory-service

   or, if the last revision is known-bad:

       kubectl rollout undo deployment -n demo-app inventory-service

## Verification

The incident is resolved when all of the following are true:

1. inventory-service responses return within the expected time window.
2. api-gateway latency returns to baseline for inventory-backed routes.
3. inventory-service logs no longer show repeated slow query warnings.
4. Prometheus alert InventorySlowQueries clears.

## Rollback or Recovery

Use rollback or recovery only if the slowdown began after a known change.

Safe recovery options:

1. Roll back inventory-service to the last known-good revision.
2. Restart inventory-service after confirming endpoints are intact.
3. Reapply the target-client manifest if ports or selectors drifted.

Do not:

1. Increase gateway timeout values before finding the slow service.
2. Restart unrelated services.

## Escalation Path

Escalate when any of the following are true:

1. Slow responses persist after restart or rollback.
2. The service begins returning 5xxs instead of only slow responses.
3. Resource pressure is present and does not clear after recovery.

Notify:

1. application-oncall / pager
2. SRE owner mihawk
3. platform owner if the issue appears node or cluster related

## Related Runbooks

- RB-001 | Checkout Service | High Error Rate
- RB-002 | API Gateway | High Latency
- RB-006 | Downstream Dependency Failure
- RB-010 | Platform | DNS / Networking Issue

## Change Log

- 0.1 Initial draft created for local Markdown runbooks.
- Steps emphasize visible inventory-service symptoms and standard recovery actions.