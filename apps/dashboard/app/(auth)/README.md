# Authentication routes

- `setup/page.tsx` claims an empty installation and creates its first admin.
- `login/page.tsx` starts a rotating refresh-token session.
- `register/page.tsx` is available only when open registration is enabled.
- `accept-invite/page.tsx` creates an account from a server-owned invitation.

The access token remains in browser memory. The refresh token is an httpOnly
cookie and rotates on refresh; reuse revokes its token family. There is no
seeded account or default password.

Public-route changes must also update `apps/dashboard/middleware.ts`.
