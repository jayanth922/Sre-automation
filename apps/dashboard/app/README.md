# App Router

Route groups separate public authentication from the protected operator
console without changing visible URLs.

- `(auth)`: setup, login, registration, and invitation acceptance.
- `(dashboard)`: cluster, incident, service, SLO, runbook, job, audit, settings,
  and team routes.
- `layout.tsx`: global styles and the authentication provider.

Middleware treats the presence of the httpOnly refresh cookie as a session
hint. The client exchanges that cookie for an access token held in memory;
access tokens are never persisted in local storage. Backend authorization still
validates every request.

When adding a public route, update `middleware.ts` and add a reachability test.
Cluster-scoped pages must preserve the cluster ID in every API request.
