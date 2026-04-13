# sre_agent API

This folder contains the versioned HTTP surface exposed by the agent runtime. It is the product API layer that the dashboard uses to read and manipulate clusters, incidents, jobs, SLOs, metrics, alerts, and mission-control data.

The important distinction is that this is not the reasoning engine itself. This layer is the contract exposed to the rest of the product while the actual agent execution lives in the runtime modules next to it.

## Purpose

The API namespace exists so the control plane can keep its product-facing routes organized without mixing them into the lower-level runtime internals. That makes it possible to evolve the graph, the prompt logic, and the data flow separately from the public HTTP contract.

## Versioning Model

The `v1` directory is the current contract. When you need a breaking change, add a new version directory instead of mutating the current routes in place. That keeps the dashboard and backend integration stable while allowing the next API revision to be introduced deliberately.

## Contents

- [v1/](v1/) contains the concrete route implementations.
- `auth_deps.py` centralizes authentication and organization lookup dependencies used by the route modules.

## How The Routes Are Used

The dashboard reaches these routes through Next.js rewrites, which keeps the browser on one origin and avoids cross-origin complexity. The backend auth flow supplies the bearer token, and the API dependency helpers convert that token into the current user and organization context.

These routes are the bridge between the operator UI and the data model. If the dashboard shows a cluster, incident transcript, job queue, or SLO status, it is reading from this API layer.

## What To Change Here

Update this layer when you are changing:

- The fields returned by list or detail endpoints.
- The auth/organization context required by the routes.
- The shape of incident transcript, job, metric, or SLO responses.
- The versioned contract consumed by the dashboard.

## Practical Notes

- Keep auth and org resolution in [auth_deps.py](auth_deps.py) rather than duplicating token parsing in every route.
- Keep response payloads aligned with the backend schemas so the UI does not have to perform guesswork.
- Document any new route family in [v1/README.md](v1/README.md) so the public contract stays discoverable.

## Related Docs

- [v1/README.md](v1/README.md)
- [../../dashboard/README.md](../../dashboard/README.md)
- [../../backend/README.md](../../backend/README.md)