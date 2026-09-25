"""Multi-tenant secure access (Phase 4).

Replaces single-shared-secret integrations (one static SLACK_BOT_TOKEN, one
static MCP_SERVICE_TOKEN-authenticated identity for every edge MCP call)
with per-tenant issued credentials:

- ``slack_oauth``: a manually-pasted, verified Slack bot token per
  Organization instead of one global env-var token for the whole deployment.
- ``relay_auth``: relays a cluster's own resolved credentials (GitHub PAT,
  k8s token, Notion key) to ``edge_mcp_servers`` alongside the existing
  tenant-identity headers, so one control plane can act on behalf of many
  distinct Cluster rows instead of assuming exactly one tenant per
  deployment.
"""
