# Auth Routes

This route group contains the public login and registration pages. It sits outside the protected dashboard shell so unauthenticated users can create an account or sign in before they are redirected into the operator workspace.

## Pages

- [login/page.tsx](login/page.tsx) posts credentials to `/auth/token`, stores the bearer token through the auth context, and redirects into the app.
- [register/page.tsx](register/page.tsx) creates a new account and organization through the auth API.

## Behavior

- These pages are intentionally lightweight and do not use the protected dashboard chrome.
- The login form expects the seeded admin credentials from the backend seed flow unless you have created your own user.
- The auth context syncs the token into a cookie so the middleware can allow access to protected routes.
- Once the token is present, the app should transition into the protected dashboard shell without forcing a manual reload.

## Design Notes

The auth pages are intentionally minimal because their job is to establish trust and session state, not to explain the product. Their visual treatment is much simpler than the incident workspace because the user only needs a quick path into the system.

## Extension Notes

If you add another public auth screen, keep it in this route group so it remains accessible before login. If a future auth page needs dashboard chrome or cluster context, it probably belongs in the protected route group instead.

## Related Docs

- [../README.md](../README.md)
- [../../lib/README.md](../../lib/README.md)
- [../../../backend/README.md](../../../backend/README.md)