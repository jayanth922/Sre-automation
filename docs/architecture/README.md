# Architecture Diagrams

This folder contains the source and generated diagrams for the SRE Agent Intermediate system architecture.

## Diagram Files

### Sources (Mermaid)

- **system-topology.mmd** — The 4-layer system flow: Target_Client generates incidents, edge MCP servers expose evidence, sre_agent runtime reasons over evidence, and the platform persists state and serves the dashboard UI.
- **agent-runtime-flow.mmd** — The request-to-investigation flow inside the sre_agent runtime: from user prompt through specialist evidence gathering, supervisor aggregation, summary generation, and timeline persistence.
- **backend-data-model.mmd** — The entity-relationship overview of the backend persistence layer: organizations, users, clusters, incidents, timeline events, jobs, audit trails, and SLOs.

### Sequence Diagrams (Mermaid)

- **dashboard-login-sequence.mmd** — The operator sign-in flow: dashboard form submission, backend credential verification, token issuance, and browser session storage.
- **incident-followup-sequence.mmd** — The incident follow-up loop: dashboard message, API persistence, new investigation turn, MCP evidence gathering, and timeline update.
- **mcp-evidence-sequence.mmd** — The tool-call path from the runtime into an MCP server and back with structured evidence payloads.
- **platform-bootstrap-sequence.mmd** — The platform startup path: scripts, compose, database and cache services, backend migrations and seed, and dashboard readiness.
- **job-lifecycle-sequence.mmd** — The queued-job path from API submission through consumer execution and persisted status updates.

### Generated (SVG)

The images folder contains the compiled SVG outputs of the source diagrams above. These SVGs are committed to the repository so they render directly in GitHub-style markdown preview without requiring build steps on read.

## Regenerating Diagrams

When you update a `.mmd` source file, regenerate the corresponding SVG:

### One-time setup

From the dashboard folder:

```bash
cd dashboard
npm install --save-dev @mermaid-js/mermaid-cli
```

### Regenerate all diagrams at once

```bash
cd dashboard
npm run generate-diagrams
```

### Regenerate a single diagram

```bash
cd dashboard
npx mmdc -i ../docs/architecture/system-topology.mmd -o ../docs/architecture/images/system-topology.svg -t dark
```

Repeat for `agent-runtime-flow.mmd`, `backend-data-model.mmd`, and the sequence diagram sources listed above.

## Diagram Semantics

- **system-topology.svg** shows request flow and evidence flow between layers, not startup dependency order. The dashboard is always a client of the API; the runtime does not call back into the UI.
- **agent-runtime-flow.svg** shows the reasoning loop: incident or alert enters, specialists gather evidence in parallel, supervisor routes and aggregates, summary is generated, and timeline events are persisted.
- **backend-data-model.svg** shows high-level entity relationships; it is not a full table catalog. It emphasizes the incident, timeline event, and audit-trail structures that the agent and dashboard rely on most.

## Source Control

- Commit the `.mmd` files directly to git.
- Commit the generated `.svg` files so they are always available in GitHub markdown preview without running tools.
- When you update a diagram, regenerate its SVG and commit both the source and the output together.

## Extending

If you add a new major subsystem diagram (e.g., dashboard routing tree, scheduler flow, MCP server interaction pattern):

1. Create a new `.mmd` file in this directory.
2. Add a generation command to [../dashboard/package.json](../dashboard/package.json) with the same pattern as the existing ones.
3. Generate the output and commit both files.
4. Link the new diagram in the relevant README where it adds clarity.
