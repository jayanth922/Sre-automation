# Auth Routes

This route group contains the public login and registration pages. It sits outside the protected dashboard shell so unauthenticated users can create an account or sign in before they are redirected into the operator workspace.

## Pages

- [login/page.tsx](login/page.tsx) posts credentials to `/auth/token`, stores the bearer token through the auth context, and redirects into the app.
- [setup/page.tsx](setup/page.tsx) claims an unclaimed installation, creating its first admin and organization. It closes permanently once a user exists.
- [register/page.tsx](register/page.tsx) creates a new account and organization. It refuses once the installation is claimed, because joining an existing organization goes through an invitation instead.
- [accept-invite/page.tsx](accept-invite/page.tsx) redeems an invitation token into an account. The email address and role come from the server-side invitation record, so the page only collects a password.

## Behavior

- These pages are intentionally lightweight and do not use the protected dashboard chrome.
- There is no seed account. The first admin comes from the claim page; everyone after that arrives by invitation.
- The auth context syncs the token into a cookie so the middleware can allow access to protected routes.
- Once the token is present, the app should transition into the protected dashboard shell without forcing a manual reload.

## Design Notes

The auth pages are intentionally minimal because their job is to establish trust and session state, not to explain the product. Their visual treatment is much simpler than the incident workspace because the user only needs a quick path into the system.

## Extension Notes

If you add another public auth screen, keep it in this route group **and add its path to `isPublicPath` in `middleware.ts`** — otherwise the middleware redirects every unauthenticated visitor to `/login` before the page renders. If a future auth page needs dashboard chrome or cluster context, it probably belongs in the protected route group instead.

## Related Docs

- [../README.md](../README.md)
- [../../lib/README.md](../../lib/README.md)
- [../../../backend/README.md](../../../backend/README.md)