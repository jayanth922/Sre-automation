# Protected Dashboard Shell

This route group contains the authenticated dashboard experience. It defines the shared shell, page-title logic, account menu, and the full-screen treatment used by the incident workspace.

## What It Owns

- The top-level header and account menu.
- The page title derived from the current pathname.
- The scroll and overflow behavior for normal pages versus the incident workspace.
- The cluster-scoped route subtree under `clusters/`.

## Layout Behavior

The layout applies a full-height background and a sticky header so the operator shell stays visible while content changes underneath it. That is important for the dashboard because the incident workspace and the cluster list both benefit from persistent navigation while the body changes.

For the incident dashboard, the main content area switches to a tighter overflow model so the transcript and details panes can use the full viewport. That keeps the screen focused on the conversation rather than on the surrounding shell.

## Child Routes

- `page.tsx` is the shell entry for the protected dashboard.
- `clusters/[id]/page.tsx` redirects to the cluster incident list.
- `clusters/[id]/incidents/page.tsx` shows the incident table.
- `clusters/[id]/incidents/[incidentId]/` opens the incident conversation view.
- `clusters/[id]/audit/page.tsx` shows the audit trail.

## Dashboard Shell Responsibilities

The shell decides the visible page title from the route, surfaces the account menu, and ensures the layout remains visually consistent across most protected pages. That means most feature pages should not recreate their own app chrome.

## Extension Notes

If a future page needs a different layout contract, decide whether it should still live in this route group. If it is a protected page that uses the same operator shell, it probably belongs here. If it needs a very different viewport behavior, it may need its own route-group boundary.

## Related Docs

- [../README.md](../README.md)
- [clusters/README.md](clusters/README.md)