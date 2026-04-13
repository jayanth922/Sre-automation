# dashboard app

This directory contains the Next.js App Router tree for the dashboard. The route groups are intentionally used to keep the public auth pages, the protected operator shell, and the future preview/demo paths separated without changing the visible URL structure.

## Route Structure

- `(auth)/` contains the public login and registration screens.
- `(dashboard)/` contains the protected workspace, cluster pages, incident views, and audit trail.
- `(preview)/` is reserved for preview or demo-only routes.
- `layout.tsx` is the root application layout and applies the global font and auth provider.

## Navigation And Auth

The dashboard relies on two layers of access control:

1. `middleware.ts` checks for the token cookie and redirects anonymous users to the public auth pages.
2. `lib/auth-context.tsx` restores the session on the client and keeps token state in sync between the cookie and localStorage.

This is why the app can gate protected routes before React renders while still having a live token state in components once the app is loaded.

## Key Pages

- `/login` and `/register` are the public auth pages.
- `/clusters/[id]` redirects into the cluster incident list.
- `/clusters/[id]/incidents` shows the incident table for the selected cluster.
- `/clusters/[id]/incidents/[incidentId]` opens the incident workspace.
- `/clusters/[id]/audit` opens the audit trail.

## Layout Model

The root layout provides fonts and the auth provider. The protected dashboard layout adds the shell chrome, page titles, and account menu. The incident workspace pages deliberately live in the protected route group so they can own the viewport and not inherit the same scroll behavior as a generic content page.

## What To Extend Here

If you add a route, ask whether it belongs in:

- the public auth group,
- the protected dashboard shell,
- or the future preview/demo area.

That decision matters because it affects authentication, layout chrome, and data fetching behavior.

## Related Docs

- [../README.md](../README.md)
- [(auth)/README.md](%28auth%29/README.md)
- [(dashboard)/README.md](%28dashboard%29/README.md)