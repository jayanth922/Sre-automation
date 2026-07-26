# Run It On Your Machine — Checklist

Everything needed to stand the system up and exercise each capability, with the
exact flags that turn each feature on. Work top to bottom the first time.

---

## 0. Prerequisites

- [ ] Docker Desktop with **Kubernetes enabled** (the demo runs in the local K8s node).
- [ ] `uv` + Python 3.12 (`pyproject.toml` targets 3.12).
- [ ] Node 20+ (for the dashboard).
- [ ] A working `~/.kube/config` (the K8s + executor MCP servers mount it).
- [ ] An LLM provider: local **Ollama**, or an API key for **groq / gemini / nvidia**.
- [ ] `git bash`/WSL if on Windows (the start scripts are bash).

Housekeeping: the accidental nested `SRE_Agent_Intermediate/` folder was already
removed; if it reappears from a fresh clone, delete it.

---

## 1. Environment

```bash
cp .env.example .env
cp edge_mcp_servers/.env.example edge_mcp_servers/.env
```

Edit the root `.env` — the values that matter:

- [ ] `SECRET_KEY` — any strong random string.
- [ ] `LLM_PROVIDER` = `ollama` | `groq` | `gemini` | `nvidia`, plus its key
      (`GROQ_API_KEY` / `GOOGLE_API_KEY` / `NVIDIA_API_KEY`, or `OLLAMA_BASE_URL`).
- [ ] `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB`.
- [ ] MCP URIs (`MCP_K8S_URI` … `MCP_EXECUTOR_URI`) — defaults are correct for the
      local compose setup.
- [ ] `PROMETHEUS_URL` / `LOKI_URL` — defaults point at the target's stack.

Edit `edge_mcp_servers/.env`:

- [ ] `GITHUB_TOKEN` + `GITHUB_REPO` (for the GitHub MCP / code-change agent).
- [ ] `PROMETHEUS_URL` / `LOKI_URL` for the target observability stack.

---

## 2. Feature flags (all default OFF/safe)

Set these in the root `.env`. Progression: leave everything off for a pure
advisor run, then enable one tier at a time.

| Flag | Default | What it turns on |
|------|---------|------------------|
| `ACT_PHASE_ENABLED` | `false` | Severity gate + Planner/Reflector in the graph + **dry-run** executor (the act_report). No cluster mutation. |
| `EXECUTOR_LIVE` | `false` | Real Tier-1 remediation via the executor MCP (only when the whole plan is autonomous). Needs `mcp-executor` running. |
| `CHECKPOINTER_ENABLED` | `false` | Durable, resumable investigations. |
| `CHECKPOINTER_BACKEND` | `memory` | `redis` / `postgres` for true cross-crash durability. |
| `RUNBOOK_AUTOGEN` | `false` | Auto-write a runbook per resolved incident into the corpus. |
| `SKILL_STORE_PATH` | (unset) | Path to persist learned skills as JSON across restarts. |
| `MODEL_ROUTER_ENABLED` | `true` | Task-aware model routing (already on). |
| `AUTONOMY_MAX_SEVERITY` | `3` | Severities ≥ this (SEV3/SEV4) may auto-remediate. |
| `MAX_CONCURRENT_INVESTIGATIONS` | `5` | Concurrency cap for parallel investigations. |

Optional tuning: `MODEL_ROUTER_LOW_BUDGET_THRESHOLD`,
`MODEL_ROUTER_STRONG_PROVIDER`, `VERIFY_ERROR_THRESHOLD`,
`VERIFICATION_WAIT_SECONDS`, `CONTEXT_MAX_TOKENS`, `POLICY_RESTART_RISK_THRESHOLD`.

Edge executor guardrails (in `edge_mcp_servers/docker-compose.yaml` or its env):
`EXECUTOR_ALLOWED_NAMESPACES=demo-app`, `EXECUTOR_MIN_REPLICAS=1`.

---

## 3. Start everything

```bash
uv sync                 # python env
./main_start.sh         # Target_Client → platform → edge MCP servers
```

Or step-by-step if you want to isolate a layer:

```bash
./Target_Client/start.sh                       # workload + monitoring in K8s
cd platform && docker compose up -d --build     # Postgres/Redis/Qdrant/API/dashboard
cd edge_mcp_servers && docker compose up -d --build  # the 6 MCP servers
```

Dashboard (production build or dev):

```bash
cd dashboard && npm ci && npm run build && npm run start   # or: npm run dev
```

---

## 4. Verify it's up

- [ ] Dashboard: http://localhost:3002
- [ ] Agent API + docs: http://localhost:8080/docs
- [ ] Target gateway: http://localhost:8000
- [ ] Chaos panel: http://localhost:8888
- [ ] MCP servers: 4000 (k8s) 4001 (prom) 4002 (loki) 4003 (github) 4004 (runbooks) **4005 (executor)**
- [ ] Prometheus 9090 · Grafana 3001 · Loki 3100 · Alertmanager 9093
- [ ] `curl http://localhost:4005/` (executor MCP) and check `executor_health` reports your allowed namespaces.

Log in to the dashboard with the seeded admin (from `SEED_ADMIN_EMAIL` /
`SEED_ADMIN_PASSWORD`, defaults `admin@example.com` / `admin`).

---

## 5. Trigger an incident and watch the flow

Pick one:

- [ ] **Chaos panel** (`:8888`) — inject error/latency/`provider_down`; Prometheus
      rules fire → Alertmanager → the agent investigates.
- [ ] **Benchmark driver** (fires synthetic alerts): see §6.
- [ ] **Manual webhook** — POST an Alertmanager payload to
      `POST /api/v1/alerts/webhook` with the cluster token.

Then watch:

- [ ] **Cockpit**: http://localhost:3002/cockpit — every investigation in parallel;
      approve any plan in `WAITING_APPROVAL`.
- [ ] Open an incident to read the live transcript (plan → specialists → reflector
      → planner → summary → **act_report** when ACT is on).

---

## 6. Exercise each capability

- [ ] **Advisor (default):** flags off → investigation ends at a summary/recommendation.
- [ ] **ACT dry-run:** `ACT_PHASE_ENABLED=true`, restart the API → the timeline gets
      an `act_report` (severity, decision, the `kubectl` it *would* run). Zero mutation.
- [ ] **ACT live:** also set `EXECUTOR_LIVE=true` and ensure `mcp-executor` is up →
      low-severity reversible fixes apply to `demo-app` only, then verify re-checks
      the metric. Higher severity routes to the cockpit for approval.
- [ ] **Durability:** `CHECKPOINTER_ENABLED=true`, `CHECKPOINTER_BACKEND=redis` (or
      postgres) → start a long investigation, kill the agent container mid-run,
      restart it, confirm it resumes the same incident.
- [ ] **Skill memory:** trigger the same incident class twice → the 2nd plan cites
      the learned skill (`recorded_skill`/`proposed_skills` in the act_report).
      Add `SKILL_STORE_PATH=/data/skills.json` to persist across restarts.
- [ ] **Auto-runbooks:** `RUNBOOK_AUTOGEN=true` → after an incident, look for
      `RB-AUTO-*.md` in the runbooks corpus.
- [ ] **NL query:** in a Python shell against a running metrics MCP:
      `uv run python -c "import asyncio; from sre_agent.nl_query import answer_metric_question as a; print(asyncio.run(a('checkout error rate last hour')).plan.promql)"`
- [ ] **Benchmark:**
      ```bash
      ACT_PHASE_ENABLED=true uv run python benchmarks/sre_bench.py   # MTTR + quality
      uv run python benchmarks/bench_mttr.py                          # MTTR only
      ```

---

## 7. Run the tests

```bash
uv run pytest -q            # the 164 unit/integration tests added for the flagship
uv run pytest tests/test_act_integration.py -v   # end-to-end ACT pipeline
```

(The three original repo tests — `test_timeline_crud`, `test_supervisor_follow_up`,
`test_mission_control_follow_up` — need a live DB and are separate.)

---

## 8. Optional: Terminal-Bench score

```bash
pip install terminal-bench
tb run --agent-import-path benchmarks.terminal_bench_adapter:SRETerminalAgent
```

Confirm the `AbstractInstalledAgent` import path + method signatures against your
installed `terminal-bench` version first (the adapter is written to the documented
interface and guarded, but the API can shift between versions).

---

## 9. Things to confirm on first real run (honest caveats)

- [ ] **Dashboard `npm run build`** — I validated the cockpit TSX by parser, not a
      full Next 16 build.
- [ ] **Redis/Postgres checkpointer** — the backend savers use the standard
      `from_conn_string` API guarded with a memory fallback; confirm against your
      installed `langgraph-checkpoint-redis` / `-postgres` version.
- [ ] **Live execution scope** — the executor guardrail allow-list is `demo-app`
      only; keep it that way until you trust it.
- [ ] **Terminal-Bench** interface (see §8).

---

## 10. Teardown

```bash
./main_Stop.sh
```

---

### Suggested first pass

1. Flags all off → start → fire one chaos incident → watch the advisor investigate.
2. `ACT_PHASE_ENABLED=true` → repeat → read the dry-run act_report.
3. `EXECUTOR_LIVE=true` → repeat a low-severity case → watch it self-remediate + verify.
4. `CHECKPOINTER_ENABLED=true` → run the durability kill/restart test.
5. `ACT_PHASE_ENABLED=true uv run python benchmarks/sre_bench.py` → capture the numbers.
