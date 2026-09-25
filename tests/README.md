# Test suite

The suite covers API tenancy, authentication, durable jobs, process-death
recovery, remediation policy, MCP guardrails, specialist bounds, evaluation
contracts, release evidence, documentation truthfulness, and frontend/backend
wiring.

Run everything from the repository root:

```bash
uv run pytest -q
```

Integration tests that require their explicit marker live under
`tests/integration/`:

```bash
uv run pytest -q tests/integration -m integration
```

Prefer the smallest relevant file while developing, then run the complete
suite before submission. Live model or infrastructure smoke tests are not part
of the default pytest run; opt-in commands live under `scripts/smoke/`.
