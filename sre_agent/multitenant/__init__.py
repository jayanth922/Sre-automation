"""Multi-tenant secure access (Phase 4).

Replaces single-shared-secret integrations (one static GITHUB_TOKEN, one
static SLACK_BOT_TOKEN, one static MCP_SERVICE_TOKEN-authenticated identity
for every edge MCP call) with per-tenant issued credentials:

- ``github_app``: GitHub App installation tokens, short-lived and minted
  per ``Cluster.github_app_installation_id`` instead of a stored long-lived PAT.
- ``slack_oauth``: Slack "Add to Slack" OAuth, one bot token per Organization
  instead of one global env-var token for the whole deployment.
- ``relay_auth``: relays a cluster's own resolved credentials to
  ``edge_mcp_servers`` alongside the existing tenant-identity headers, so one
  control plane can act on behalf of many distinct Cluster rows instead of
  assuming exactly one tenant per deployment.
"""
