# Versioned API

`api/v1` contains Sentinel's authenticated product routes. The FastAPI app in
`sre_agent.agent_runtime` mounts them alongside the backend `/auth` routes.

Shared authentication, tenant ownership, and cluster ownership dependencies
belong in `v1/auth_deps.py` and `v1/ownership.py`. Route handlers must derive
organization scope from the authenticated principal rather than accepting it as
trusted request data.

The Next.js console reaches these routes through same-origin rewrites. Update
backend schemas, route responses, dashboard types, and wiring tests together
when changing a contract.

See [the route overview](../../../docs/architecture/images/api-routes.svg).
