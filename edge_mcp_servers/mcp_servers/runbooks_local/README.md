# Runbooks

Local Markdown source of truth for operational runbooks.

The runbooks MCP server builds a semantic search index over these Markdown files so the agent can retrieve relevant procedures from runbooks directly.

## Required frontmatter

Each runbook should use YAML frontmatter with at least:

- `title`
- `runbook_id`
- `service`
- `incident_type`
- `severity`
- `status`
- `owner_team`
- `primary_owner`
- `tags`
- `last_reviewed`
- `version`
- `source_of_truth`
- `escalation_channel`
- `related_systems`
- `alert_name`
- `impacted_environment`
- `service_tier`

## Content rules

- Keep the section order consistent.
- Use numbered steps for the resolution procedure.
- Keep commands explicit when known.
- Use placeholders only when the exact environment detail is still unknown.
- Keep the incident-response intent visible; do not describe hidden failure injectors or simulation-only tooling in these files.