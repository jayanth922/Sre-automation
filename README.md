# Sentinel — self-hosted, multi-agent SRE reliability console

Sentinel is an autonomous Site Reliability Engineering platform you run on **your
own Kubernetes cluster**. It watches your services, opens incidents from real
telemetry, and runs a multi-agent investigation — Observe → Orient → Decide →
Act — that correlates metrics, logs, Kubernetes state, code changes, and
runbooks into a root-cause hypothesis, then proposes severity-gated remediation
with a human in the loop. It is not a SaaS: the platform, the datastores, the
agent runtime, and the tool servers all deploy together into one namespace, and
no telemetry leaves the client's infrastructure.

Design principle throughout: **orchestrate, don't override.** Clients bring their
own LLM, their own MCP tool servers, their own runbooks (Notion or local), and
their own metric conventions — Sentinel adapts to them.

## What it does

- **Proactive detection.** A continuous monitor sweeps every connected cluster's
  per-service health and opens incidents on breach; Prometheus Alertmanager
  webhooks feed the same pipeline.
- **Multi-agent investigation (OODA).** A LangGraph supervisor coordinates
  metrics / logs / Kubernetes / GitHub / runbooks specialists over the Model
  Context Protocol, then a reflector forms a hypothesis and a planner decides a
  remediation.
- **Severity-gated remediation.** A policy gate classifies severity and
  reversibility; low-risk reversible actions can run autonomously, everything
  else requires human approval. Live actions are verified against the metrics
  afterward. Read-only by default.
- **Memory + runbooks.** Skill memory recalls what resolved similar incidents;
  runbooks come from the client's Notion database or a local corpus and feed RAG.
- **Two front doors.** A live web console and a Slack on-call bot both write to
  the same incident conversation.

## Architecture

One namespace, in-cluster service DNS, no external networking required:

- **API / agent runtime** — FastAPI + LangGraph; the control plane and the AI brain.
- **Web console** — Next.js; live incident timeline, service health, SLOs,
  analytics, runbooks, settings.
- **Postgres** — users, orgs, clusters, incidents, timeline, SLOs, audit, refresh sessions.
- **Redis** — live event bus (`/ws`), cache, graph checkpointer.
- **Qdrant** — vector store for runbook / skill memory.
- **Seven edge MCP tool servers** — the agent's hands: k8s, prometheus, loki,
  github, runbooks, executor (scale/restart), github-exec (revert PRs). The k8s
  and executor servers use in-cluster ServiceAccounts (RBAC), not a mounted kubeconfig.

## The agent (OODA loop)

`supervisor → specialists → reflector (orient) → planner (decide) → act_gate`

The full reasoning loop runs on every investigation — severity classification,
policy-gate decision, a dry-run remediation proposal, skill-memory recall/record,
a generative runbook, and a human-readable resolution report. Live cluster
mutation is governed by the policy gate + human approval (surfaced as
`EXECUTOR_LIVE`), which is a deliberate safety control, not a feature flag.

**Model routing.** Each task type is routed to a model tier (fast / balanced /
strong) — cheap models for narration/routing, strong models for
reflection/planning — with complexity bump-up, budget-aware downgrade,
off-policy blocking, and a provider fallback chain. Per-tier provider overrides
(`MODEL_ROUTER_<TIER>_PROVIDER`) let you split, e.g., reflection/planning on
Claude and fast narration on Gemini.

## Observability, security, production engineering

- **Agent observability** — layered: the live incident timeline (transparency),
  `/agent/metrics` (per-node runs/latency/errors, always on), and full Langfuse
  span tracing of every LLM/tool/chain call with tokens & cost when configured
  (self-hostable).
- **Auth / sessions** — every HTTP and WebSocket route on the runtime requires
  an authenticated principal; tenant and cluster context are derived
  server-side from that principal, never accepted as trusted request input.
  Sessions use a short-lived access token in memory + rotating refresh token
  in an httpOnly cookie, with reuse detection and server-side revocation.
- **Tenant isolation** — the MCP/tool plane is resolved per tenant+cluster
  (endpoints, credentials, namespace allowlists), not shared off one
  process-global registry; audit log entries are stamped with
  `organization_id`/`cluster_id` at write time, not inferred after the fact.
- **Mutation safety** — every live cluster write passes one mutation gateway
  immediately before the tool call fires, re-checking tenant, namespace,
  approval/action hash, cluster lock, and idempotency — not just at the API
  layer where a lock could go stale between planning and execution. Cluster
  credentials and provider API keys are encrypted at rest (envelope
  encryption, versioned keys); nothing sensitive is logged or traced in the
  clear.
- **Guardrails** — prompt-injection defense on untrusted telemetry entering
  prompts; action guardrails on the executor / github-exec tools; the policy
  gate + approval as the real safety net.
- **Durability** — investigations run as Postgres lease-backed durable jobs
  (heartbeat renewal, bounded retry, cancellation, dead-letter queueing), and
  graph state is checkpointed per incident against the same durable backend
  so a human-approval pause or an API restart resumes exactly once instead of
  re-running or silently dropping the incident.
- **Release safety** — changes to prompts, model routing, or tool contracts
  are gated in CI by a content-addressed evidence bundle (paired statistical
  comparison, zero-tolerance adversarial suite, full trace accounting) keyed
  to the exact source digest of what changed; a change without matching
  evidence fails closed instead of shipping unverified.

## Deploy (same-machine Kubernetes)

Build the images once, then pick one:

```bash
# Plain manifests
./deploy/k8s/install.sh

# Helm
helm install sentinel deploy/helm/sentinel -n sentinel --create-namespace \
  --set secrets.secretKey=$(openssl rand -hex 32) \
  --set secrets.postgresPassword=$(openssl rand -hex 16) \
  --set secrets.credentialEncryptionKey=$(python -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())') \
  --set secrets.mcpServiceToken=$(openssl rand -hex 32)

# Terraform (Helm release only — BYO cluster + pre-created Secret; see deploy/terraform/README.md)
kubectl apply -f deploy/terraform/secret.example.yaml   # edit first
cd deploy/terraform && terraform init && terraform apply
```

Then port-forward the console (`:3002`) and the API (`:8080`) and open the
console to **register** — the first sign-up creates the organization and becomes
its admin (manage teammates and roles under Team). Connect a cluster in Settings (its Prometheus/Loki endpoints, metric
conventions, GitHub repo, Notion runbook database) and point Alertmanager at
`POST /api/v1/alerts/webhook` with the cluster token.

## Configure (bring your own)

- **LLM** — `anthropic` (Claude) or `gemini`, per cluster or per model-router
  tier (`MODEL_ROUTER_<TIER>_PROVIDER`). Provider support is deliberately
  narrow rather than a generic LiteLLM passthrough: both providers give
  reliable tool/function-calling structured output, which the agent's
  specialist and planner nodes depend on. Startup fails closed with a
  migration message if a legacy provider (`groq`, `ollama`, `nvidia`,
  `openai`, `openai_compatible`) is still configured.
- **MCP tools** — register your own servers via `MCP_SERVERS_JSON`, merged with
  the built-ins.
- **Runbooks** — a cluster's Notion database, or the local markdown corpus.
- **Slack** — set the bot + app tokens to mirror incidents into your on-call
  channel and let engineers steer via `@mention`.
- **Datastores** — deploy the bundled Postgres/Redis/Qdrant, or bring your own
  (`*.deploy=false` + external endpoints).

## Testing & benchmarks

- **CI** (`.github/workflows/ci.yml`), gated behind a `quality-gate` aggregator
  that has to see every job pass:
  - `python-quality` — Ruff, Mypy, compileall, secret scanning.
  - `backend-tests` — `pytest` against a real Postgres service, plus the docs
    truthfulness suite (`tests/test_docs_truthfulness.py`).
  - `release-evaluation` — runs `release_gate.py impact` over the PR's diff;
    a protected prompt/model/tool change without a matching evidence bundle
    fails the build.
  - `migrations` — Alembic upgrade/downgrade round-trip on a clean database.
  - `eval-smoke` — fast subset of the agent evaluation suite.
  - `frontend` — dashboard type-check and lint.
  - `manifests` / `terraform` — Helm and Terraform validation against
    production defaults.
  - `images-platform` / `images-edge` — container builds for the runtime and
    each MCP tool server (matrixed).
- **Quickstart smoke** (no live cluster): `bash scripts/quickstart_smoke.sh`
  runs secret scanning, compileall, and the docs truthfulness tests as a fast
  local pre-flight before pushing.
- **Benchmarks** (`benchmarks/`): `sre_bench.py` / `bench_mttr.py` fire scenarios
  at a live platform. Credentials come from env (`BENCH_ADMIN_*`,
  `BENCH_CLUSTER_*`) or runtime bootstrap (`BENCH_BOOTSTRAP=1`) via
  `benchmarks/fixtures.py` — no static cluster tokens are shipped.
  Scoring is pure-function and unit-tested (`tests/test_bench_scoring.py`).
  `release_gate.py` (used by CI above) evaluates the same kind of evidence
  bundle offline via its `evaluate`/`impact`/`matrix` subcommands.

## Tech stack

Python 3.12 · FastAPI · LangGraph / LangChain · Model Context Protocol ·
Postgres · Redis · Qdrant · Next.js 16 / React 19 · Docker · Kubernetes ·
Helm · Terraform · Langfuse.
