# Notion Runbooks MCP Server

This service exposes each tenant's Notion-hosted operational runbooks over MCP. There is no local runbook corpus — Notion is the sole source of truth, per team.

## Responsibilities

- Query the cluster's Notion runbook database (title + optional properties: service, incident type, severity, tags, owner team, alert name, environment, escalation channel).
- Fetch a page's block content and render it to Markdown on demand.
- Serve runbook search and retrieval tools to the platform, matching the tool names/signatures the agent already calls (`search_runbooks`, `get_runbook_content`, `get_incident_playbook`, `get_troubleshooting_guide`, `get_escalation_procedures`, `get_common_resolutions`).

## Why It Exists

Production runbooks live wherever an SRE team already keeps them — for most teams, Notion. This server lets the agent search and quote that same Notion content instead of maintaining a separate, easily-stale local copy.

## Configuration

- Per-cluster: `notion_api_key` / `notion_database_id`, relayed per request from the control plane (`X-Sentinel-Relay-Notion-Key` / `X-Sentinel-Relay-Notion-Database` — see `sre_agent/multitenant/relay_auth.py`).
- Self-hosted/single-tenant fallback: `NOTION_API_KEY` / `NOTION_DATABASE_ID` env vars on this process, used only when no credential is relayed for the in-flight connection.
- The compose stack publishes the service on host port `4004`.

## Operational Notes

- No schema is assumed on the Notion database beyond "there is a title property" — other fields are read from same-named properties when present.
- A short-lived (20s), bounded per-credential cache avoids re-querying the whole database on every tool call within one investigation.
- Without Notion credentials (relayed or static), runbook tools return an empty/error result rather than falling back to any local file source.

## Related Docs

- [../README.md](../README.md)
- [../../README.md](../../README.md)
