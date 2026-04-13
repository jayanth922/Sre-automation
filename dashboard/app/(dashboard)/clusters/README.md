# Cluster Routes

This subtree contains the cluster-scoped dashboard pages. It is where operators move from the cluster list into the incident table, then into the full incident conversation workspace, and optionally into the audit trail.

## Route Flow

1. [id]/page.tsx redirects the cluster root into the incident list.
2. [id]/incidents/page.tsx shows the table of incidents for that cluster.
3. [id]/incidents/[incidentId]/ opens the incident workspace.
4. [id]/audit/page.tsx shows the audit log for remediation and operator actions.

## What This Subtree Represents

Cluster identity is the pivot for most dashboard interactions. Once a cluster is selected, the rest of the experience stays scoped to that cluster so the operator can follow incidents, actions, and transcripts without losing context.

This subtree is the operator’s path from overview to action:

- cluster selection,
- incident review,
- incident conversation and follow-up,
- and audit inspection.

## How The Pages Relate

The redirect page keeps the cluster root from becoming a dead end. The incidents page provides the operational table view. The incident page is the main workspace. The audit page gives the compliance and remediation record that closes the loop.

## Extension Notes

If you add another cluster-scoped screen, consider whether it belongs beside the incidents page or beside the audit trail. Either way, keep the route names cluster-first so the scope remains obvious.

## Related Docs

- [../README.md](../README.md)
- [../../README.md](../../README.md)