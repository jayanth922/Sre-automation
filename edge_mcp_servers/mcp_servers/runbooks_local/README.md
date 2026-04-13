# Local Runbooks MCP Server

This service exposes repository-backed operational runbooks over MCP. Instead of reading from an external knowledge base, it indexes the markdown files stored alongside the repository so the agent can search the local runbook corpus.

## Responsibilities

- Load markdown runbooks from the `runbooks/` directory.
- Parse frontmatter when present.
- Build a searchable index, with optional semantic embeddings when the embedding dependency is available.
- Serve runbook search and retrieval tools to the platform.

## Why It Exists

This server gives the agent a deterministic, file-backed runbook source for the demo environment. It makes the operational knowledge visible in git and easy to review, while still giving the agent a structured way to query it.

That is useful because the platform can quote a specific runbook rather than inventing a remediation suggestion from scratch.

## Configuration

- `RUNBOOKS_DIR` overrides the runbooks location.
- `RUNBOOKS_EMBEDDING_MODEL` selects the embedding model when embeddings are available.
- `RUNBOOKS_INDEX_LIMIT` caps the number of indexed files.
- The compose stack publishes the service on host port `4004`.

## Operational Notes

- The server builds an index from markdown files in the runbook corpus.
- If embeddings are unavailable, the server still works with text-based search behavior.
- The local runbooks folder is meant to stay in sync with the operational scenarios the demo stack can actually produce.

## Related Docs

- [../README.md](../README.md)
- [../../README.md](../../README.md)