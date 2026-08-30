---
title: RB-AUTO-high_error_rate-checkout-service | checkout-service | High Error Rate
runbook_id: RB-AUTO-high_error_rate-checkout-service
service: checkout-service
incident_type: high_error_rate
severity: UNKNOWN
status: Negative exemplar
review_state: pending_review
agent_retrievable: true
learning_outcome: blocked
owner_team: SRE
incident_id: null
skill_id: null
verification_status: blocked
tags:
- auto-generated
- checkout-service
- high_error_rate
- incident-response
- kubernetes
- negative-exemplar
alert_name: CheckoutHighErrorRate
impacted_environment: demo-app
source_of_truth: Auto-generated from incident
generated_from_incident: unknown
generated_at: '2026-08-30T02:31:51.657212+00:00'
version: '0.1'
---

# RB-AUTO-high_error_rate-checkout-service | checkout-service | High Error Rate

> Auto-generated from incident `unknown`. Review and promote
> to an owned runbook if the guidance holds.

## Summary

Use this runbook when **CheckoutHighErrorRate** fires on **checkout-service**
(failure class: *high_error_rate*, severity UNKNOWN).

## Symptoms

- Alert `CheckoutHighErrorRate` firing (severity: critical).
- Impacted service: `checkout-service` in namespace `demo-app`.

## Root cause (hypothesis)

See investigation summary.

## Remediation (what the agent applied)

1. **rollback** `checkout-service`
   ```bash
   kubectl rollout undo deployment/checkout-service -n demo-app
   ```

## Verification

- Re-check the Golden Signals for `checkout-service` after remediation.
- Confirm the alert `CheckoutHighErrorRate` clears and error rate / latency return to baseline.
- Current verification status at generation time: **blocked**.

## Prevention / next steps

- Add or tune an SLO alert for this failure class if missing.
- If this recurs, the agent will propose the stored skill automatically.
- Promote this auto-runbook to an owned, reviewed runbook.
