# Architecture diagrams

The Mermaid sources in this directory describe the current product topology,
runtime, persistence, API, authentication, dashboard, jobs, and MCP evidence
flows. Each `.mmd` source has a committed SVG with the same base name under
[`images/`](images/).

Start with:

- [System topology](images/system-topology.svg)
- [Agent runtime flow](images/agent-runtime-flow.svg)
- [Incident remediation lifecycle](images/incident-investigation-loop.svg)
- [MCP evidence sequence](images/mcp-evidence-sequence.svg)
- [Backend data model](images/backend-data-model.svg)
- [Authentication flow](images/auth-flow.svg)
- [Dashboard routing](images/dashboard-routing.svg)

The diagrams show system boundaries and important invariants rather than every
field or endpoint. Code, tests, and generated OpenAPI remain authoritative.

## Regeneration

```bash
cd apps/dashboard
npm ci
npm run generate-diagrams
```

The `generate-diagrams` script covers every committed Mermaid source. Commit
source and SVG changes together. The documentation truthfulness test verifies
that no orphaned source or generated image is left behind.

## Maintenance rules

- Do not add screenshots as architecture documentation.
- Keep target-specific examples outside the base topology.
- Remove diagrams for deleted demos instead of labeling them as current.
- Run `bash scripts/ci/check_no_static_secrets.sh` before committing generated
  assets or documentation.

See [module ownership](MODULE_OWNERS.md) for the corresponding code entry
points.
