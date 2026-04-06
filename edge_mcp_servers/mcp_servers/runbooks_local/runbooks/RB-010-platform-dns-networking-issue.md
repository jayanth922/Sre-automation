---
title: RB-010 | Platform | DNS / Networking Issue
runbook_id: RB-010
service: api-gateway
incident_type: network issue
severity: Critical
status: Draft
owner_team: SRE
primary_owner: mihawk
tags:
  - networking
  - dns
  - kubernetes
  - incident-response
last_reviewed: ""
version: "0.1"
source_of_truth: Local Markdown
escalation_channel: network-oncall / pager
related_systems:
  - api-gateway
  - checkout-service
  - inventory-service
alert_name: DNSFailuresHigh
impacted_environment: production
service_tier: tier-0
---

# RB-010 | Platform | DNS / Networking Issue

## Summary

RB-010 covers platform and service-discovery failures in the demo-app Kubernetes stack, especially cases where api-gateway cannot resolve or reach checkout-service or inventory-service. Use this runbook when DNS lookups fail, service endpoints disappear, or in-cluster traffic starts timing out with lookup, connection, or routing errors.

## Metadata

- Runbook ID: RB-010
- Service: api-gateway
- Incident Type: network issue
- Severity: Critical
- Status: Draft
- Owner Team: SRE
- Primary Owner: mihawk
- Related Systems: dns, cloud-load-balancer, kubernetes
- Alert Name: DNSFailuresHigh
- Escalation Channel: network-oncall / pager
- Impacted Environment: production
- Service Tier: tier-0
- Version: 0.1
- Source of Truth: Local Markdown

## When to Use

Use this runbook when any of the following are true:

1. api-gateway returns elevated 5xxs, timeouts, or gateway errors and the failure pattern suggests downstream resolution or routing rather than business logic.
2. Pods in demo-app cannot resolve checkout-service or inventory-service by short name or by full cluster DNS name.
3. `kubectl get endpoints` shows missing or empty endpoints for api-gateway, checkout-service, or inventory-service.
4. Prometheus fires DNSFailuresHigh or a similar DNS and networking alert.
5. api-gateway logs show errors such as no such host, lookup failed, connection refused, or i/o timeout.
6. Traffic reaches api-gateway inconsistently, or the service has an external LoadBalancer address problem.
7. The issue appears broader than one application and may involve CoreDNS, kube-system, service selectors, cluster networking, or load balancer exposure.

## Preconditions and Required Access

Before making changes, confirm the responder has:

1. Read access to the Kubernetes cluster and demo-app namespace.
2. Permission to inspect kube-system for DNS-related components.
3. Permission to run kubectl exec into api-gateway and related pods.
4. Permission to restart deployments if remediation is required.
5. Permission to reapply `Target_Client/k8s/services.yaml` if the service objects need repair.
6. Access to Prometheus, Loki, and kubectl logs for diagnosis.
7. Access to the current deployment context for the demo-app environment.

## Safety Checks

Before changing anything:

1. Confirm whether the problem is isolated to api-gateway or affects multiple services in demo-app.
2. Confirm whether the issue is DNS resolution, service endpoints, service selectors, or actual network transport.
3. Confirm whether a recent manifest change, rollout, or cluster restart preceded the outage.
4. Confirm that checkout-service and inventory-service deployments still exist and are healthy.
5. Confirm that service selectors still match pod labels.
6. Confirm whether CoreDNS or kube-dns is healthy before restarting application pods.
7. Do not restart or reapply objects blindly if the problem is clearly kube-system or cluster-wide.
8. Treat LoadBalancer exposure problems separately from internal service DNS failures.

## Detection Signals

Look for these signals together:

1. Alertmanager or Prometheus alert DNSFailuresHigh.
2. Elevated 5xxs at api-gateway.
3. Requests timing out while calling checkout-service or inventory-service.
4. Logs showing lookup failures, no such host, dial tcp, connection refused, or i/o timeout.
5. Empty or missing endpoints in the demo-app namespace.
6. Service objects present but pods not reachable from api-gateway.
7. CoreDNS pods failing, restarting, or not ready.
8. LoadBalancer service for api-gateway missing an external address or stuck in pending state.
9. Pod-to-pod traffic failing while pod health itself still appears normal.

## Step-by-Step Resolution

1. Confirm the scope and blast radius.

   Run:

       kubectl get pods -n demo-app -o wide
       kubectl get svc -n demo-app
       kubectl get endpoints -n demo-app api-gateway checkout-service inventory-service
       kubectl get events -n demo-app --sort-by=.lastTimestamp | tail -n 20

   Expected result: you can see which pods are Running, which services exist, and whether endpoints are populated. If endpoints are empty, the issue is likely selector, readiness, or pod placement related.

2. Test name resolution from inside api-gateway.

   Run:

       kubectl exec -n demo-app deploy/api-gateway -- sh -lc 'getent hosts checkout-service && getent hosts inventory-service'
       kubectl exec -n demo-app deploy/api-gateway -- sh -lc 'getent hosts checkout-service.demo-app.svc.cluster.local && getent hosts inventory-service.demo-app.svc.cluster.local'

   Expected result: both short names and fully qualified service DNS names resolve to cluster IPs. If resolution fails, suspect CoreDNS, kube-dns, or cluster DNS config.

3. Test in-cluster HTTP reachability to the downstream services.

   Run:

       kubectl exec -n demo-app deploy/api-gateway -- sh -lc 'curl -fsS http://checkout-service:8001/health || curl -fsS http://checkout-service:8001/'
       kubectl exec -n demo-app deploy/api-gateway -- sh -lc 'curl -fsS http://inventory-service:8002/health || curl -fsS http://inventory-service:8002/'

   Expected result: each command returns a valid HTTP response. If DNS resolves but HTTP fails, the problem is connectivity, service routing, port mismatch, or an application-side refusal.

4. Inspect api-gateway logs for lookup and transport errors.

   Run:

       kubectl logs -n demo-app deploy/api-gateway --tail=200

   Look for:
   - no such host
   - lookup failed
   - connection refused
   - i/o timeout
   - upstream unreachable
   - service unavailable

   Expected result: either you confirm a DNS/network failure or you rule it out and move to the next layer.

5. Verify service selectors and endpoints.

   Run:

       kubectl describe svc -n demo-app api-gateway
       kubectl describe svc -n demo-app checkout-service
       kubectl describe svc -n demo-app inventory-service
       kubectl get pods -n demo-app --show-labels
       kubectl get endpoints -n demo-app api-gateway checkout-service inventory-service -o yaml

   Known target-client labels and selectors from the manifest:
   - api-gateway uses app: api-gateway
   - checkout-service uses app: checkout-service
   - inventory-service uses app: inventory-service

   Expected result: service selectors match pod labels and endpoints list pod IPs. If selectors or endpoints are broken, fix the manifest or reapply the service objects.

6. Check cluster DNS health in kube-system.

   Run:

       kubectl get pods -n kube-system | grep -E 'coredns|kube-dns'
       kubectl get svc -n kube-system | grep -E 'kube-dns|coredns'
       kubectl logs -n kube-system deploy/coredns --tail=200

   Expected result: DNS pods are Running and Ready, and logs do not show repeated crash loops or query failures. If the cluster uses kube-dns instead of coredns, inspect the DNS deployment that actually exists.

7. Check for network policy or cluster networking interference.

   Run:

       kubectl get networkpolicy -n demo-app
       kubectl describe pod -n demo-app -l app=api-gateway

   Expected result: there is no deny-all policy or obvious node/network event blocking pod-to-pod communication. If policies exist, validate that api-gateway can reach checkout-service and inventory-service.

8. Repair the application manifests if service wiring is wrong.

   If service selectors, ports, or endpoints are incorrect, reapply the known-good service manifest from the target client repository.

   Run:

       kubectl apply -f Target_Client/k8s/services.yaml
       kubectl rollout restart deployment -n demo-app api-gateway checkout-service inventory-service

   Expected result: services regain endpoints, pods restart cleanly, and internal DNS plus HTTP routing recover.

9. Recover DNS if CoreDNS or kube-dns is unhealthy.

   Restart the DNS deployment that exists in kube-system.

   Run one of:

       kubectl rollout restart deployment coredns -n kube-system

   or, if the cluster uses kube-dns:

       kubectl rollout restart deployment kube-dns -n kube-system

   Expected result: DNS pods restart cleanly and service resolution returns for api-gateway and the other pods.

10. If internal DNS is healthy but the gateway still cannot reach downstream services, restart only the affected application pods.

   Run:

       kubectl rollout restart deployment -n demo-app api-gateway

   Expected result: api-gateway reconnects to checkout-service and inventory-service and the error rate drops.

11. If external access is the issue, verify the LoadBalancer service for api-gateway.

   Run:

       kubectl get svc -n demo-app api-gateway -o wide

   Expected result: the service has a valid external address or the cluster’s expected LoadBalancer exposure. If the external address is missing or stuck pending, escalate to the platform/network owner after confirming internal DNS is healthy.

## Verification

The incident is resolved when all of the following are true:

1. api-gateway can resolve checkout-service and inventory-service from inside the cluster.
2. api-gateway can call checkout-service:8001 and inventory-service:8002 successfully.
3. `kubectl get endpoints` shows valid endpoints for the affected services.
4. CoreDNS or kube-dns is healthy if it was part of the issue.
5. api-gateway logs no longer show lookup or connection errors.
6. Prometheus alert DNSFailuresHigh clears, or the equivalent DNS/network alert returns to normal.
7. The service responds normally from the expected access path for the environment.

If verification fails, do not keep restarting components blindly. Re-check whether the failure is in service selectors, DNS, or cluster networking and escalate if the issue persists.

## Rollback or Recovery

Use rollback or recovery only if the issue began after a known manifest change or rollout.

Safe recovery options:

1. Reapply the known-good Target_Client/k8s/services.yaml manifest.
2. Restart only the affected deployment after DNS is confirmed healthy.
3. Restore the api-gateway service to type LoadBalancer if it was changed.
4. Restore selectors or ports if a manifest edit introduced an endpoint mismatch.

Do not:

1. Scale services to zero.
2. Delete services or namespaces as a first response.
3. Restart CoreDNS repeatedly without checking logs and pod state.
4. Change unrelated application code to mask a networking issue.

If rollback does not stabilize the service, treat the issue as a platform or cluster problem and escalate to the network or platform owner.

## Escalation Path

Escalate when any of the following are true:

1. DNS resolution fails from multiple pods or across multiple namespaces.
2. CoreDNS or kube-dns remains unhealthy after one restart.
3. Service endpoints are correct but transport still fails.
4. The LoadBalancer address for api-gateway remains unavailable.
5. The issue appears to involve cluster networking, CNI, or kube-system rather than one app deployment.

Notify:

1. network-oncall / pager
2. SRE owner mihawk
3. platform or cluster owner if the issue spans kube-system or node networking

## Related Runbooks

- RB-001 | Payments | High Error Rate
- RB-002 | API Gateway | High Latency
- RB-003 | Checkout | Service Outage / Crash Loop
- RB-006 | Payments | Downstream Dependency Failure

## Change Log

- 0.1 Initial draft created from the local markdown runbooks migration.
- Placeholders were replaced with concrete operational guidance based on the target-client Kubernetes manifests and architecture.
- Chaos dashboard and load generator were intentionally excluded from the procedure.