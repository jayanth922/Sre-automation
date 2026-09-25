# Dashboard components

- [`ui/`](ui/): generic visual primitives with no incident or tenant knowledge.
- [`console/`](console/): shared operator-console layout and navigation.

Keep API calls and tenant-aware behavior in feature components or route pages,
not in visual primitives. Shared API access goes through `lib/auth-context.tsx`
so refresh, authorization headers, and error handling remain consistent.

Incident, metric, SLO, and account workflows live in their route pages under
`app/`. Do not add speculative shared components: extract one only after at
least one mounted route uses it.
