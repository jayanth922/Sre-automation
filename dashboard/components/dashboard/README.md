# Feature Components

This folder contains the dashboard-specific React components that power objective health, metrics, and the account menu.

These are not generic UI primitives. They are the components that turn API responses into the operator workflow the product is trying to deliver.

## Components

- [MetricSparklines.tsx](MetricSparklines.tsx) renders compact visualizations for signal trends.
- [SLOOverview.tsx](SLOOverview.tsx) summarizes objective health and error budget status.
- [UserAccountMenu.tsx](UserAccountMenu.tsx) shows the signed-in user menu and logout control.

## Behavioral Notes

The incident workspace is not here. `IncidentCommandCenter` and `IncidentChatPanel` were removed with `POST /incidents/{id}/message`: both composed a follow-up that queued a full agent turn, nothing imported either of them, and conversation belongs in the incident's Slack thread. The live incident surface is the console under `app/(dashboard)/clusters/[id]`.

What remains still carries workflow meaning:

- `SLOOverview` is a summary of service health, not merely a chart.
- `MetricSparklines` are compact but still carry operational meaning, because they show trends the operator should interpret.
- `UserAccountMenu` is the account-level control surface and depends on the auth session.

## Data Flow Expectations

The feature components usually depend on:

- the auth context and its `api` helper,
- route parameters from the App Router,
- and live backend responses that may update while the page is open.

Because of that they stay client-side and handle refresh or polling behavior themselves.

## When To Add More

Add a new feature component here when it owns a reusable dashboard interaction that spans more than one page. If the component is just a small helper for a single page, keep it near that page instead.

## Design Expectations

Feature components should feel like product surfaces, not generic demo widgets. They should present the data in a way that helps the operator answer the next question quickly: what is happening, what changed, what is the current status, and what should be done next.

## Related Docs

- [../README.md](../README.md)
- [../../app/README.md](../../app/README.md)
- [../../lib/README.md](../../lib/README.md)