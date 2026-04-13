# Local Runbooks Corpus

This folder contains the markdown runbooks indexed by the local runbooks MCP server. Each file is a standalone operational playbook that the agent can search when it needs remediation guidance or incident-specific context.

## Naming Convention

The files use a stable identifier prefix so they can be referenced and sorted consistently:

- `RB-001-*` for checkout-service scenarios.
- `RB-002-*` for api-gateway scenarios.
- `RB-003-*` for inventory-service scenarios.
- `RB-006-*` and beyond for broader dependency or platform issues.

## How The Server Uses Them

The runbooks MCP server loads these files, parses any frontmatter, and builds a searchable index. The agent then uses the server to retrieve operational guidance when the evidence points toward a known failure pattern.

This is the strongest link between the demo and real operating practice: the assistant can point to a concrete runbook path instead of returning a generic answer.

## Writing Guidance

- Keep the title, service, and incident type obvious from the filename and frontmatter.
- Include actionable remediation steps instead of generic advice.
- Prefer concise, specific evidence and expected signals so the agent can match a runbook to the live incident.
- Write the steps in the order an operator would actually execute them.

## Related Docs

- [../README.md](../README.md)
- [../../README.md](../../README.md)