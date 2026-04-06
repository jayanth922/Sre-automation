---
title: RB-001 | Checkout Service | High Error Rate
runbook_id: RB-001
service: checkout-service
incident_type: application error
severity: High
status: Draft
owner_team: SRE
primary_owner: mihawk
tags:
  - checkout
  - errors
  - payments
  - kubernetes
  - incident-response
last_reviewed: ""
version: "0.1"
source_of_truth: Local Markdown
escalation_channel: application-oncall / pager
related_systems:
  - api-gateway
  - inventory-service
  - payment-processing
alert_name: CheckoutHighErrorRate
impacted_environment: production
service_tier: tier-0
---

# RB-001 | Checkout Service | High Error Rate

## Summary

RB-001 covers elevated 5xx responses from checkout-service in the demo-app Kubernetes stack. Use this runbook when checkout requests begin failing, the service returns payment or processing errors, or the error rate climbs sharply while the pod is still reachable.

## Metadata

- Runbook ID: RB-001
- Service: checkout-service
- Incident Type: application error
- Severity: High
- Status: Draft
- Owner Team: SRE
- Primary Owner: mihawk
- Related Systems: api-gateway, inventory-service, payment-processing, kubernetes
- Alert Name: CheckoutHighErrorRate
- Escalation Channel: application-oncall / pager
- Impacted Environment: production
- Service Tier: tier-0
- Version: 0.1
- Source of Truth: Local Markdown

## When to Use

Use this runbook when any of the following are true:

1. checkout-service is returning elevated 500s, 502s, or 503s.
2. api-gateway logs show upstream errors when calling checkout-service.
3. Prometheus fires CheckoutHighErrorRate or PaymentFailureSpike.
4. checkout-service logs show payment, validation, database, or dependency failures.
5. The service is reachable but request success rate drops below the expected baseline.
6. Error spikes appear after a deployment, config change, or node disruption.

## Preconditions and Required Access

Before making changes, confirm the responder has:

1. Read access to the Kubernetes cluster and demo-app namespace.
2. Permission to inspect checkout-service logs and metrics.
3. Permission to run kubectl exec into checkout-service if needed.
4. Permission to restart the deployment if recovery is required.
5. Access to Prometheus and kube events for diagnosis.

## Safety Checks

Before changing anything:

1. Confirm the failure is isolated to checkout-service and not the entire gateway.
2. Confirm the pod is Running and Ready.
3. Confirm the service still has endpoints.
4. Confirm whether a recent rollout or config change occurred.
5. Confirm whether errors are deterministic or intermittent.

## Detection Signals

Look for these signals together:

1. Elevated 5xxs on checkout-service.
2. Error spikes in api-gateway when routing to checkout-service.
3. Logs containing payment failure, database error, timeout, or validation failure messages.
4. Increased restart counts or crash loops on the checkout pod.
5. Prometheus alert CheckoutHighErrorRate firing.

## Step-by-Step Resolution

1. Confirm the service and pod state.

   Run:

       kubectl get pods -n demo-app -l app=checkout-service -o wide
       kubectl get svc -n demo-app checkout-service
       kubectl get endpoints -n demo-app checkout-service -o yaml

   Expected result: the pod is Running and Ready, the service exists, and endpoints contain the pod IP.

2. Inspect checkout-service logs for the failure pattern.

   Run:

       kubectl logs -n demo-app deploy/checkout-service --tail=200

   Look for:
   - payment gateway timeout
   - database error
   - validation failure
   - connection refused
   - unhandled exception

3. Check the live health and basic endpoint behavior.

   Run:

       kubectl exec -n demo-app deploy/api-gateway -- sh -lc 'curl -fsS http://checkout-service:8001/health'
       kubectl exec -n demo-app deploy/api-gateway -- sh -lc 'curl -fsS -X POST http://checkout-service:8001/process?order_id=test-order'

   Expected result: health returns normally and the process endpoint produces either a valid response or a repeatable error pattern that matches the logs.

4. Verify recent rollout or configuration changes.

   Run:

       kubectl rollout history deployment -n demo-app checkout-service
       kubectl describe deploy -n demo-app checkout-service

   Expected result: you can identify whether the issue began after a rollout or manifest update.

5. Check resource pressure and pod stability.

   Run:

       kubectl top pod -n demo-app -l app=checkout-service
       kubectl describe pod -n demo-app -l app=checkout-service

   Expected result: the pod is not CPU-starved, memory-throttled, or repeatedly restarting.

6. Recover the service if the failure is due to a bad pod state or rollout regression.

   Run one of:

       kubectl rollout restart deployment -n demo-app checkout-service

   or, if the last change is known-bad:

       kubectl rollout undo deployment -n demo-app checkout-service

   Expected result: checkout-service becomes healthy and the error rate drops.

## Verification

The incident is resolved when all of the following are true:

1. checkout-service returns successful responses for normal requests.
2. api-gateway no longer logs upstream checkout errors.
3. Pod restarts are stable and endpoints remain populated.
4. Prometheus alert CheckoutHighErrorRate clears.

If verification fails, re-check the logs and recent rollout before restarting again.

## Rollback or Recovery

Use rollback or recovery only if the issue began after a known deployment or config change.

Safe recovery options:

1. Roll back checkout-service to the last known-good revision.
2. Restart checkout-service after confirming service endpoints are intact.
3. Reapply the target-client manifest if selectors or ports drifted.

Do not:

1. Delete the namespace.
2. Scale unrelated services.
3. Change api-gateway to hide checkout errors.

If rollback does not stabilize the service, escalate to the application owner.

## Escalation Path

Escalate when any of the following are true:

1. Error rate remains high after rollback or restart.
2. Logs indicate a dependency failure that is outside checkout-service.
3. The pod repeatedly crashes after startup.
4. The failure affects payment processing or customer-facing checkout flow.

Notify:

1. application-oncall / pager
2. SRE owner mihawk
3. platform owner if the issue appears node or cluster related

## Related Runbooks

- RB-002 | API Gateway | High Latency
- RB-006 | Downstream Dependency Failure
- RB-010 | Platform | DNS / Networking Issue

## Change Log

- 0.1 Initial draft created for local Markdown runbooks.
- Steps focus on observable checkout-service symptoms and standard Kubernetes recovery actions.