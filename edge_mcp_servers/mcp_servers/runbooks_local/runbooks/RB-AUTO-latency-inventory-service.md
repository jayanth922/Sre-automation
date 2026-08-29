---
title: RB-AUTO-latency-inventory-service | inventory-service | Latency
runbook_id: RB-AUTO-latency-inventory-service
service: inventory-service
incident_type: latency
severity: SEV3
status: Negative exemplar
learning_outcome: incomplete
owner_team: SRE
incident_id: null
skill_id: null
verification_status: incomplete
tags:
- auto-generated
- incident-response
- inventory-service
- kubernetes
- latency
- negative-exemplar
alert_name: InventorySlowQueries
impacted_environment: demo-app
source_of_truth: Auto-generated from incident
generated_from_incident: unknown
generated_at: '2026-08-29T22:37:22.082285+00:00'
version: '0.1'
---

# RB-AUTO-latency-inventory-service | inventory-service | Latency

> Auto-generated from incident `unknown`. Review and promote
> to an owned runbook if the guidance holds.

## Summary

Use this runbook when **InventorySlowQueries** fires on **inventory-service**
(failure class: *latency*, severity SEV3).

## Symptoms

- Alert `InventorySlowQueries` firing (severity: warning).
- Impacted service: `inventory-service` in namespace `demo-app`.

## Root cause (hypothesis)

See investigation summary.

## Remediation (what the agent applied)

1. No automated remediation recorded; investigate manually.

## Verification

- Re-check the Golden Signals for `inventory-service` after remediation.
- Confirm the alert `InventorySlowQueries` clears and error rate / latency return to baseline.
- Current verification status at generation time: **incomplete**.

## Prevention / next steps

- Add or tune an SLO alert for this failure class if missing.
- If this recurs, the agent will propose the stored skill automatically.
- Promote this auto-runbook to an owned, reviewed runbook.
