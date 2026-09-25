# GitHub MCP Server

This service exposes repository intelligence over MCP using the PyGithub client. It lets the agent inspect commits and pull requests and correlate code changes with incidents.

## Responsibilities

- Connect to GitHub using `GITHUB_TOKEN`.
- Open the repository identified by `GITHUB_REPO`.
- Serve commit and pull-request tools through FastMCP.
- Provide code-change context when the investigation points toward a deploy or regression.

## Why It Exists

The incident workflow needs code-change context when a regression is suspected. This server gives the agent a direct way to inspect recent code history without hard-coding GitHub calls into the platform runtime.

It is especially useful when the supervisor decides that an incident may be caused by a recent release, because the GitHub evidence can be correlated with metrics and logs in the same turn.

## Configuration

- `GITHUB_TOKEN` is required.
- `GITHUB_REPO` must be in `owner/repo` form.
- `HTTP_PORT` defaults to `3000`.
- `HOST` defaults to `0.0.0.0`.

## Operational Notes

- The server initializes the GitHub client on startup.
- If the token or repository is missing, the service may start but the tools will fail until the credentials are set.
- The tools exposed here are meant to support incident correlation, not full repository management.

## Related Docs

- [../README.md](../README.md)
- [../../README.md](../../README.md)