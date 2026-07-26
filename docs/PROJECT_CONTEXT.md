# Project Context & Working Agreement (Claude's working memory)

> This file is my persistent context for this project so I don't drift. Read it
> at the start of any session before touching the SRE Agent.

## Standing instructions from Jayanth
- **This is his FLAGSHIP resume project. Quality is not to be compromised at any
  level.** Rigor, correctness, and defensibility over speed or breadth.
- **Never hallucinate, guess, or assume.** If context is missing, ask. Verify
  external facts against current sources ("as of today").
- Be concise and direct; remove words that don't add meaning.
- Use **dynamic role-play**: adopt the most fitting expert role(s) per task and
  state them, for sharper reasoning.
- He will share resources incrementally — keep them in context, don't lose them.

## The repository (SRE_Agent_Intermediate)
Four-layer multi-agent incident-response system:
- **Target_Client** — noisy fake customer stack on K8s (gateway + checkout/
  inventory + load-gen + chaos panel); produces incidents + Prom/Loki/Grafana.
- **edge_mcp_servers** — 5 MCP servers (K8s, Prometheus, Loki, GitHub, Runbooks).
- **sre_agent** — LangGraph OODA loop (Observe→Orient→Decide→Act). Supervisor
  routes to specialists (Metrics/Logs/GitHub/Runbooks); Reflector forms a
  hypothesis (confidence); Planner builds a RemediationPlan with runbook RAG +
  Qdrant memory. Provider-agnostic (ollama/groq/gemini/nvidia).
- **backend + dashboard** — Postgres persistence/auth + Next.js operator cockpit.
- Also: `benchmarks/bench_mttr.py`, human-checkpoint interrupt system,
  `policy_engine.py`, and a **stubbed ACT phase** (execution disabled).

## The "7 projects" (from Harkirat Singh's video) — see docs/INTEGRATION_PLAN.md
1. Terminal agent (top Terminal-Bench)  2. Hermes/OpenClaw agent
3. Slack+AI (Buzz, PromptQL)  4. Superset/T3 Code  5. Generative courses
6. Model router  7. Domain benchmark

## Decisions log
- **2026-07-25:** Deep-read repo; wrote `docs/INTEGRATION_PLAN.md` mapping all 7
  onto the system as ONE platform.
- **2026-07-25:** Built project #6 (Model Router) — `sre_agent/model_router.py`
  + `tests/test_model_router.py` (12 tests pass). NOT yet wired into call sites.
- **2026-07-25:** **Chosen FLAGSHIP centerpiece → Project #1: the Terminal/
  Executor agent that closes the OODA loop into policy-gated autonomous
  remediation, validated on a Terminal-Bench-style harness (pulls in #7).**
- **2026-07-25 — v1 scope decisions from Jayanth:**
  - **Autonomy = SEVERITY-DRIVEN.** Compute incident severity the way real
    products do (impact × urgency); LOW severity → agent executes autonomously;
    higher severity → mandatory human approval. (Not a fixed action-tier list —
    severity is the gate.)
  - **Handle the most common error classes** (broad coverage, not one scenario);
    willing to **increase Target_Client complexity** to surface more error types.
  - **Secure multi-tenant access is a first-class requirement.** Real setup:
    client infra is separate; platform must connect securely after the client
    configures their infra + GitHub/Slack details. The edge_mcp_servers layer is
    the seam — design it as a customer-installed outbound relay / connector model
    (least-privilege, no inbound access into client network).

- **2026-07-25 — Phase 0 BUILT (dry-run, zero prod risk):**
  - `sre_agent/severity_engine.py` — impact×urgency → SEV1–4, confidence round-up.
  - `sre_agent/policy_gate.py` — severity × reversibility × policy → AUTONOMOUS/
    REQUIRES_APPROVAL/BLOCKED.
  - `sre_agent/executor.py` — dry-run only; sha256-chained audit; live raises
    NotImplementedError until Phase 1.
  - `examples/act_phase_demo.py` (runs with no infra), `docs/ACT_PHASE_DESIGN.md`.
  - Tests: `tests/test_severity_engine.py`, `test_policy_gate.py`,
    `test_executor.py` — **40 tests pass total** (incl. model_router).
  - NOT yet wired into the compiled LangGraph (additive seam documented in
    ACT_PHASE_DESIGN.md §7). Next: Phase 1 sandboxed Executor MCP server.

- **2026-07-25 — ACT phase WIRED into the graph (flag-guarded):**
  - `sre_agent/act_phase.py` — pure `build_act_report(state)`: extract signals →
    severity → gate plan → dry-run executor. Duck-typed, testable.
  - `sre_agent/graph_builder.py` — new `_act_gate_node` + `_act_phase_enabled()`;
    when `ACT_PHASE_ENABLED=true`, wires `aggregate → act_gate → END` (else
    unchanged `aggregate → END`). Node is non-fatal, emits an "act" timeline
    event when incident_id present.
  - `tests/test_act_phase.py` (6 tests). **46 dependency-light tests pass.**
  - graph_builder.py passes `py_compile`; full graph run still needs the app
    stack (langchain/langgraph/sqlalchemy) which isn't in the sandbox.
  - NOTE: the Planner/Reflector OODA nodes are still not in the compiled graph,
    so `remediation_plan` is only present when that path runs; act_gate cleanly
    no-ops ("ACT skipped") otherwise. Wiring the Planner into the live graph is
    the next graph change.

- **2026-07-25 — Planner/Reflector WIRED into the live graph (flag-guarded):**
  - `graph_builder.py`: when `ACT_PHASE_ENABLED=true`, `_route_supervisor`
    diverts the terminal step to `reflector`; graph becomes
    `supervisor → reflector → planner → aggregate → act_gate → END`. Default
    (flag off) is unchanged: `aggregate → END`, no OODA nodes.
  - Reflector's deeper-investigation loop is collapsed to one forward pass in v1.
  - **Validated in real LangGraph** (installed deps in sandbox): ACT-on graph
    compiles with reflector/planner/act_gate + correct edges; ACT-off graph has
    none of them. Invoked the real `_act_gate_node` with real pydantic
    RemediationPlan/AlertContext + real policy_engine: SEV4 inventory→AUTONOMOUS
    (dry-run kubectl), SEV2 checkout→REQUIRES_APPROVAL. 46 unit tests pass.
  - **True end-to-end (alert→resolved) needs the full stack** (live LLM provider,
    5 MCP servers, Postgres) — not runnable in this sandbox. On Jayanth's machine:
    `uv sync` → `docker compose up` (platform + edge + Target_Client) →
    set `ACT_PHASE_ENABLED=true` → fire an alert (e.g. `uv run python
    benchmarks/bench_mttr.py` or the Alertmanager webhook).
  - Sandbox deps installed for validation: langgraph, langchain-core/-groq/
    -ollama, sqlalchemy, asyncpg, psycopg2-binary, pydantic[email], passlib,
    python-jose, bcrypt, redis, socksio.

- **2026-07-26 — Phase 1 START: Executor MCP server (edge write-tools) built:**
  - `edge_mcp_servers/mcp_servers/executor_real/` — FastMCP server (port 4005):
    `restart_deployment`, `scale_deployment`, `patch_resource_limits`,
    `rollback_deployment`, `executor_health`. Dry-run by default (k8s
    server-side `dryRun=All`); `guardrails.py` = defense-in-depth (action +
    namespace allow-list, scale-to-0 floor, operator-owned env vars).
    Dockerfile installs kubectl (for rollback). README documents least-priv RBAC.
  - `docker-compose.yaml` → new `mcp-executor` service; `.env.example` → adds
    `MCP_EXECUTOR_URI` + `ACT_PHASE_ENABLED`.
  - `tests/test_executor_guardrails.py` (8 tests). **54 tests pass total.**
  - NEXT: wire agent-side `executor.py` live path (dry_run=False) to CALL this
    MCP server via MCP client, replacing the NotImplementedError. Then benchmark.
  - Git: committing each step now (4 retro commits + this one).

- **2026-07-26 — Phase 1 COMPLETE: agent-side live executor path wired:**
  - `executor.py`: `Executor.aexecute(dry_run=False, tool_caller=...)` calls the
    executor MCP server; `EXECUTOR_TOOL_MAP` (restart/scale/rollback/patch);
    `build_executor_tool_caller(MCP_EXECUTOR_URI)` builds the MCP client. Sync
    `execute` stays dry-run only.
  - `act_phase.execute_autonomous_live(...)` applies only AUTONOMOUS actions.
  - `graph_builder._act_gate_node`: when `EXECUTOR_LIVE=true` AND whole plan is
    AUTONOMOUS → live apply; failures non-fatal (captured as live_error).
  - THREE safety levels: ACT_PHASE_ENABLED (reason) → dry-run preview →
    EXECUTOR_LIVE (apply, autonomous-only).
  - Validated: dry-run path unchanged; live-with-no-server degrades gracefully.
    **60 unit tests pass** (added live aexecute + execute_autonomous_live tests).
  - NEXT candidates: (a) SRE benchmark (#7) scoring MTTR/root-cause/remediation
    across chaos scenarios; (b) grow Target_Client complexity; (c) wire model
    router (#6) into call sites.

## Housekeeping
- Delete the accidental duplicate nested folder:
  `SRE_Agent_Intermediate/SRE_Agent_Intermediate/`.
