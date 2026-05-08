# Architecture Diagrams

This folder contains the source and generated diagrams for the SRE Agent Intermediate system architecture.

## Diagram sources (Mermaid)

Every committed `.mmd` file in this directory has a matching `.svg` under [images/](images/). Sources are grouped by role:

**System and platform**

- **system-topology.mmd** — Four-layer flow: Target_Client, edge MCP, agent runtime, platform and dashboard.
- **platform-bootstrap.mmd** — Platform stack bootstrap overview.
- **platform-bootstrap-sequence.mmd** — Startup sequence: scripts, compose, data stores, migrations, seed, dashboard.
- **target-client-architecture.mmd** — Simulated customer stack: gateway, services, observability, chaos.

**Agent and MCP**

- **agent-runtime-flow.mmd** — Request-to-investigation path inside the agent runtime.
- **incident-investigation-loop.mmd** — Specialist evidence and supervisor loop.
- **mcp-integration.mmd** — How MCP bridges specialists to evidence servers.
- **mcp-evidence-sequence.mmd** — Tool call into an MCP server and structured response.

**Backend and API**

- **backend-data-model.mmd** — High-level persistence entities (orgs, incidents, timeline, jobs, audits, SLOs).
- **auth-flow.mmd** — Sign-in, token issuance, session storage.
- **api-routes.mmd** — Product API surface grouped by capability.
- **job-queue-system.mmd** — Job queue components.
- **job-lifecycle-sequence.mmd** — Job from API submission to persisted status.

**Dashboard**

- **dashboard-routing.mmd** — App Router groups, middleware, rewrites.
- **dashboard-login-sequence.mmd** — Login through token storage.

**Incident product flow**

- **incident-followup-sequence.mmd** — Follow-up message, investigation turn, transcript update.

## Generated SVGs

The [images/](images/) folder holds compiled SVGs for the sources above. They are committed so GitHub and offline readers can render diagrams without running Mermaid.

## Regenerating diagrams

### One-time setup

From the [dashboard](../../dashboard) app (where `@mermaid-js/mermaid-cli` is already a dev dependency):

```bash
cd dashboard
npm ci
```

### Regenerate everything

`npm run generate-diagrams` runs `mmdc` for **every** `.mmd` in this directory that ships a paired `.svg` in `images/`. After editing any source, run:

```bash
cd dashboard
npm run generate-diagrams
```

Then commit both the updated `.mmd` and `.svg` files.

### Regenerate one diagram

```bash
cd dashboard
npx mmdc -i ../docs/architecture/system-topology.mmd -o ../docs/architecture/images/system-topology.svg -t dark
```

Swap the input and output paths for other base names (for example `incident-followup-sequence`).

## Diagram semantics

- **system-topology.svg** shows request and evidence flow between layers, not strict startup order. The dashboard is always an API client; the runtime does not call the UI.
- **agent-runtime-flow.svg** shows the reasoning loop: incident or prompt in, specialists gather evidence, supervisor aggregates, summary and timeline persistence.
- **backend-data-model.svg** is a conceptual ERD, not an exhaustive table catalog. It emphasizes incidents, timeline events, and audit structures.

## Source control

- Commit `.mmd` sources and generated `.svg` outputs together when a diagram changes.
- Avoid committing only one half of a pair.

## Extending

When you add a new diagram:

1. Add `your-diagram.mmd` in this directory.
2. Add a matching `mmdc` invocation to `generate-diagrams` in [dashboard/package.json](../../dashboard/package.json) (same `-t dark` pattern as existing entries).
3. Run `npm run generate-diagrams`, then commit `.mmd`, `.svg`, `package.json`, and any README that references the new image.
