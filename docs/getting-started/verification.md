# Local setup and verification

## Prerequisites

- Docker with Compose
- Python 3.12 or newer and `uv`
- Node 20 or newer
- `kubectl`, Helm, and Terraform only for their respective deployment checks

Anthropic is the supported model provider. A key is not required to boot the
platform, run deterministic tests, or inspect the console, but it is required
for a live model-backed investigation.

## Start the local stack

```bash
bash scripts/dev/start.sh
```

The script creates `.env` and `services/edge_mcp_servers/.env` from their
examples when absent, generates internal secrets that are still blank or
placeholders, and synchronizes the MCP service token between both stacks.

Open:

- <http://localhost:3002> for the console
- <http://localhost:8080/docs> for the API schema

The first user claims the empty installation and becomes the initial admin.
There is no seeded account or default password.

Stop the stack with:

```bash
bash scripts/dev/stop.sh
```

## Configure a target cluster

Use the console to configure each cluster's namespace, Anthropic model/key,
Prometheus and Loki endpoints, GitHub connection, Notion runbook database,
Slack connection, and policy settings. These values are tenant- and
cluster-scoped; avoid process-global credentials for multi-tenant operation.

The edge stack binds MCP endpoints to loopback and requires the shared bearer
token. Mutating tools also require explicit namespace allowlists. Empty
allowlists fail closed.

## Python verification

```bash
uv sync --frozen --extra dev --extra temporal --extra anthropic
bash scripts/ci/check_python_quality.sh
bash scripts/ci/check_no_static_secrets.sh
uv run pytest -q
```

For a faster structural check:

```bash
bash scripts/dev/quickstart_smoke.sh
```

## Frontend verification

```bash
cd apps/dashboard
npm ci
npm run lint
npx tsc --noEmit
npm run build
```

## Deployment verification

Run the checks for the tools installed on your machine:

```bash
bash scripts/ci/check_helm_rbac.sh
bash scripts/ci/check_helm_ws.sh
bash scripts/ci/check_helm_production.sh
bash scripts/ci/check_kustomize.sh
bash scripts/ci/check_terraform.sh
```

The CI workflow additionally builds the API, dashboard, and every edge MCP
image.

## Evaluation verification

The following checks are deterministic and do not make model calls:

```bash
uv run python -m benchmarks.make_release_fixtures --check
uv run python evals/benchmarks/release_gate.py matrix \
  --matrix evals/benchmarks/release/v1/ci-matrix.json \
  --output reports/release-matrix.json
uv run pytest -q tests/test_release_gate.py tests/test_release_evidence.py
```

Do not run `sre_bench.py`, an ablation campaign, or another live model-backed
benchmark without explicit budget authorization. The existing smoke result is
documented in [AI_RESULTS.md](../ai/AI_RESULTS.md) and is not a comparable
model-quality row.

## Optional live MCP smoke

With the edge services configured and running:

```bash
uv run python scripts/smoke/mcp_servers.py
```

This smoke may invoke model-backed specialist agents. Treat it as a paid live
operation, not as part of the default local verification path.
