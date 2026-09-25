# Cluster routes

This subtree is the authenticated, cluster-scoped operator workspace.

- `[id]/page.tsx`: cluster landing page.
- `[id]/incidents/`: incident list and incident detail.
- `[id]/services/`: discovered service inventory and service detail.
- `[id]/slos/`: service-level objectives.
- `[id]/runbooks/`: runbook inventory and detail.
- `[id]/jobs/`: durable work status.
- `[id]/analytics/` and `[id]/insights/`: operational summaries.
- `[id]/audit/`: tenant- and cluster-scoped audit history.
- `[id]/settings/` and `[id]/team/`: integration, policy, and membership
  management.

Incident detail renders persisted workflow state, evidence, timeline events,
verification, and approval controls. New pages must retain cluster scope in
both the route and every API call; client-supplied organization identity is not
an authorization boundary.
