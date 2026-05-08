# Dashboard `lib`

Small, app-wide modules that are not React components.

## Contents

- [auth-context.tsx](auth-context.tsx) — `AuthProvider`, session restore, JWT handling, and the `api` helper used by protected pages and feature components. Keeps the bearer token aligned between the HTTP-only cookie (for `middleware.ts`) and `localStorage` (for client requests).
- [utils.ts](utils.ts) — shared helpers (for example Tailwind `cn` class merging).

## How this fits the app

`middleware.ts` only sees cookies; React needs live token state for API calls. The auth context bridges those two. Feature components under `components/dashboard/` typically consume the context rather than parsing tokens themselves.

## Related Docs

- [../README.md](../README.md)
- [../app/README.md](../app/README.md)
