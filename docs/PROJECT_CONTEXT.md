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

- **2026-07-26 — Project #7 SRE benchmark built:**
  - `benchmarks/scoring.py` — pure scoring: root-cause (AC@1), remediation,
    severity band, safety; `ScenarioSpec` (ground truth) + `aggregate`.
  - `benchmarks/sre_bench.py` — runner: fires alerts, polls resolution, fetches
    `/api/v1/incidents/{id}/transcript`, extracts `act_report`, scores across
    MTTR + 4 quality dims. Needs live platform + ACT_PHASE_ENABLED.
  - `benchmarks/README.md`; `tests/test_bench_scoring.py` (14 tests). **74 total.**
  - Reads real API fields (transcript summary + act timeline event payload).
  - NEXT candidates: grow Target_Client complexity; wire model router (#6) into
    call sites; run full benchmark on Jayanth's machine.

- **2026-07-26 — FULL VIDEO TRANSCRIPT received; accurate facts (supersede earlier inference):**
  Video = Harkirat/Super30. THREE parts, not just "7 projects":
  1. **5 class topics** (vote-to-release): memory; Firecracker/sandboxes (E2B);
     context engineering (context rot, compaction at ~700–800k of a 1M window);
     evals & RL environments (SWE-bench-from-scratch); cloud agents (Devon,
     Claude Code remote).
  2. **5 interview questions** for a Devon-like CODING agent (all map onto our SRE agent):
     Q1 architecture — you need a **sandbox per concurrent user**, scale up/down.
     Q2 **crash durability** — long tasks (30 min–2 h); resume from checkpoint via
        backed-up agent↔LLM message history; ref **Temporal**.
     Q3 context management — compaction/summarization (Manus/Claude).
     Q4 **evals** — deterministic, SWE-bench / terminal-bench.
     Q5 **observability** — Datadog/Prometheus/Grafana + LLM-obs (Neatlogs);
        agent failure traces; auto-switch infra provider on outage.
  3. **7 projects** (precise):
     #1 Terminal agent — ref codebase **"pi"** (~2k LOC TS); easy to build, HARD to
        match Claude Code on **terminal-bench**; nuance: **sub-agent orchestration**
        (pi lacks it, still competitive).
     #2 Hermes/Clawbot ("Cloudbot") — memory + integrations (WhatsApp/Telegram/
        Slack) + autonomous decisions; open source.
     #3 Slack+AI — **Buzz** (Block/Dorsey, **Rust**) + **PromptQL**; agents are
        taggable workspace members.
     #4 Superset vs T3 Code — Superset spawns CLIs in tabs; **T3 hacks into the
        agent and re-renders messages in its own UI** ("slightly better"; teaches
        agent↔LLM message flow). NOTE: our dashboard is already T3-style.
     #5 Generative courses/UI — paradigm.study; variant = AI slides + MCQ quiz.
     #6 **Model router — explicitly NOT OpenRouter.** Route by (a) task complexity,
        (b) **user's remaining credit/budget**, (c) **block personal/off-policy
        requests**. Cost control for companies.
     #7 Benchmark for a specific repo — SWE-bench/terminal-bench-style evals for ONE
        codebase (dub.sh example) + RL environments; very hard to set up.
  **Implications for what we built:**
  - `model_router.py` routes by task-type/complexity/tier only — **MISSING #6's
    budget-awareness + request-blocking**. Enhancement candidate.
  - Interview Q2 (durability): `build_multi_agent_graph` already threads a
    `checkpointer` (currently None). LangGraph checkpointer = durable resume →
    high-value, low-effort hardening that directly answers the #1 interview Q.
  - Our SRE benchmark (#7) aligns with "benchmark for a use case" spirit. ✓
  - Interview Q4/Q5 (evals + observability): benchmark done; agent-obs (thought
    traces/timeline) exists — could add LLM-obs framing.

- **2026-07-26 — Durability/checkpointing (interview Q2) wired:**
  - Fixed real bug: `checkpointer` was passed to `build_multi_agent_graph` but
    never reached `compile()` (silently dropped). Now compiled in.
  - `sre_agent/checkpointer.py`: `get_checkpointer()` (memory default; redis/
    postgres external → cross-crash durable, guarded w/ fallback), `thread_config`
    + `thread_id_from_state` (inject thread_id only when enabled).
  - Wired thread_config into ALL 6 astream sites (tasks + agent_runtime).
  - `CHECKPOINTER_ENABLED` (default false) → None checkpointer → **zero behavior
    change**; no thread_id needed. Enabled → per-incident durable resume.
  - Validated: OFF→no checkpointer; ON(memory)→InMemorySaver attached to compiled
    graph. `test_checkpointer.py` (7). **87 tests pass.**
  - Order progress: #1 router✓ #2 durability✓ → next #3 Target_Client, #4 plan doc.

- **2026-07-26 — Target_Client complexity grown (dependency chain):**
  - New `payment-service` (port 8004, `/charge`, chaos incl. `provider_down` +
    `payment_provider_up` gauge). checkout → payment via `PAYMENT_URL` (backward
    compatible) → real **downstream dependency cascade** failure class.
  - k8s: payment-service Deployment/Service in services.yaml; checkout gets
    PAYMENT_URL; start.sh builds + rolls it out.
  - Prometheus: `PaymentProviderDown` + `PaymentServiceHighErrorRate` alerts.
  - Benchmark: `payment_provider_outage` ScenarioSpec (ground truth = payment,
    not checkout — tests root-cause *attribution* across the chain).
  - Taxonomy doc updated. Validated: YAML parses (11 docs), Python compiles,
    scenarios load. 87 tests still pass. (Cluster run needed for live exercise.)
  - Order: #1✓ #2✓ #3✓ → last: #4 INTEGRATION_PLAN.md update.

- **2026-07-26 — Project #2 (Hermes → skill memory / self-improving) built:**
  - `sre_agent/skill_store.py`: incident signature (alert/service/failure_class),
    `Skill`, `InMemorySkillStore` (recurrences compound success_count),
    match/record/propose + prompt formatting. Backend-agnostic (in-memory
    default; Qdrant/DB swappable). Global `get_skill_store()`.
  - `act_phase.apply_skill_learning(state, report, store)` — propose prior skills
    + record this remediation; wired into `_act_gate_node` (adds
    proposed_skills/recorded_skill to act_report). Injectable store for tests.
  - `tests/test_skill_store.py` (10) + act_phase learning test. **97 tests pass.**
  - Validated E2E: ACT node recorded `skill-latency-inventory-service`.
  - PROJECTS status: #1✓(ACT executor) #6✓(router) #7✓(bench) #2✓(skills).
    REMAINING projects: #5 generative runbooks, #3 Slack+AI/NL-query, #4 T3 cockpit.

- **2026-07-26 — Project #5 (generative runbooks/postmortems) built:**
  - `sre_agent/runbook_generator.py`: deterministic markdown generator that
    mirrors the existing runbook YAML-frontmatter format (indexable by the
    runbooks MCP for RAG). `input_from_act(state, report)` reuses skill_store
    signature logic; `write_runbook` writes to RUNBOOKS_DIR (or repo corpus).
  - Wired into `_act_gate_node`, gated by `RUNBOOK_AUTOGEN` (default off, writes
    files). Closes the learning loop: agent's own runbooks feed future RAG.
  - `tests/test_runbook_generator.py` (8). **104 tests pass.** E2E verified:
    node wrote `RB-AUTO-high_error_rate-checkout-service.md` with valid frontmatter.
  - PROJECTS: #1✓ #2✓ #5✓ #6✓ #7✓. REMAINING: #3 Slack+AI/NL-query, #4 T3 cockpit.

- **2026-07-26 — Project #3 (Slack+AI / PromptQL NL-query) built:**
  - `sre_agent/nl_query.py`: PromptQL-style pipeline — parse intent → generate
    PromQL → **validate** (allow-listed metrics/funcs, bounded window, balanced
    syntax) → execute via injected Prometheus tool_caller. Only validated queries
    run. Plus `classify_chat_message` (query/steer/greeting) +
    `build_incident_message_payload` (bridges chat steer → existing /message
    human-checkpoint endpoint).
  - `docs/CHAT_INTEGRATION.md`: Buzz/Slack transport layer (agent-as-member) +
    security. Transport is the only deployment-specific piece; reasoning is done.
  - `tests/test_nl_query.py` (18). **122 tests pass.**
  - PROJECTS: #1✓ #2✓ #3✓ #5✓ #6✓ #7✓. REMAINING: #4 Superset/T3 cockpit (UI).

- **2026-07-26 — Project #4 (Superset/T3 → multi-incident cockpit) built:**
  - `dashboard/app/(dashboard)/cockpit/page.tsx`: parallel board of all
    investigations across clusters (polling), a **plan review/approve** panel for
    `WAITING_APPROVAL` incidents (POST /incidents/{id}/approve, admin-only), and a
    recently-resolved strip. Mirrors existing dashboard conventions (axios `api`,
    shadcn/ui, palette). Synergy: live status uses the checkpointer thread.
  - `dashboard/app/(dashboard)/layout.tsx`: Clusters/Cockpit nav + title.
  - Validated with esbuild TSX parse (syntax+JSX OK). Full type-check/build needs
    `npm ci` on Jayanth's machine (Next 16 install too slow for sandbox).
  - **ALL 7 PROJECTS DONE:** #1✓ #2✓ #3✓ #4✓ #5✓ #6✓ #7✓.

- **2026-07-26 — Completed the full "what's left" backlog (cats 2–6):**
  - Wiring gaps closed: skill loop → Planner; model router → supervisor/
    specialists (all major call sites); verification wired + dead code removed
    (new `verification.py` + generic `build_mcp_tool_caller`/metrics caller).
  - NL-query integrated (`answer_metric_question`, `handle_chat_message`).
  - Skill store persistence (`JsonSkillStore`, SKILL_STORE_PATH).
  - Interview questions built: **Q3** `context_compaction.py`, **Q5**
    `observability.py`, **Q1** `concurrency.py` (limiter + sandbox). Q2, Q4 prior.
  - Terminal-Bench adapter (`benchmarks/terminal_bench_adapter.py`).
  - ACT pipeline integration test (`test_act_integration.py`, real models).
  - **164 tests pass.** All 7 projects + all 5 interview questions now covered.
  - STILL requires Jayanth's machine: live e2e run, benchmark run, dashboard
    `npm run build`, confirm Redis/Postgres checkpointer API + TB run.

- **2026-07-26 — Depth pass (addressed "too shallow" critique):**
  - **#1 real terminal agent** `sre_agent/terminal_agent.py` — agentic run/observe
    loop, sub-agent orchestration, safety deny-list/sandbox; TB adapter now points
    at it (fixed phantom `sre_terminal_agent` reference).
  - **#5 genuinely generative** — `generate_runbook_llm`/`write_runbook_generative`
    (LLM-authored bodies, template fallback; node prefers generative) +
    `generative_course.py` (LLM course: sections/slides+quiz, parsed to typed Course).
  - **#3 real Slack transport** `sre_agent/integrations/slack_bot.py` (slack_bolt;
    process_mention/format_reply tested without Slack).
  - **181 tests pass.**
  - Honest note: #4 cockpit is an SRE-monitoring reframe of Superset/T3's
    "orchestrate coding agents" — deliberate, not a coding-agent orchestrator.

- **2026-07-26 — #2 completed properly: Hermes actually used (not just skill memory).**
  - Q "why not Hermes instead of LangGraph?" → LangGraph stays the ORCHESTRATOR
    (auditable, gated, human-in-loop = SRE requirement); Hermes = autonomous ACTOR.
    Replacing the engine ≠ integrating; it'd be a rewrite + downgrade for safety.
  - Researched + verified Hermes's real Python API (docs): `pip install
    git+…/hermes-agent`, `from run_agent import AIAgent`, `run_conversation`/`chat`,
    `disabled_toolsets`/`skip_memory`/`max_iterations`.
  - `sre_agent/actor_runtime.py`: `AgentRuntime` interface + `LocalTerminalRuntime`
    (default) + `HermesRuntime` (real, guarded import). `get_agent_runtime` via
    `AGENT_RUNTIME=local|hermes`. terminal_agent CLI uses it. `hermes` extra in
    pyproject. 6 tests (local runs; hermes raises clean install error here).
  - Note: #4 cockpit intentionally dropped as out-of-scope (user agreed).

- **2026-07-26 — Closed the code-change remediation gap (the video's composite).**
  - Found: github MCP is READ-only; write tools (create_revert_pr etc.) were in
    agent_config.yaml but implemented nowhere; executor SKIPPED revert_commit. So
    "LLM-suggested code change executed" was NOT happening.
  - Built `edge_mcp_servers/mcp_servers/github_exec/` (WRITE MCP, port 4006):
    create_revert_pr / comment_on_pr, dry-run default + guardrails (revert/comment
    only, repo allow-list). Compose service + MCP_GITHUB_EXEC_URI.
  - Executor: `GITHUB_EXEC_TOOL_MAP` + `_github_args`; `aexecute` now routes
    infra→executor MCP, code-change→github-exec MCP (via `github_caller`);
    `build_github_exec_tool_caller`. `execute_autonomous_live` + node pass the
    github caller. So a bad-deploy fix (revert PR) executes under the same
    severity/policy gate, alongside infra/runbook steps, then verification confirms.
  - Now the full composite the video describes IS done: code change + runbook-
    informed plan + executed + system-state verified. **195 tests pass.**

- **2026-07-26 — Code-fix sandbox + resolution report (user request):**
  - `code_sandbox.py`: `apply_and_test(repo, patch/patch_files, test_command)` —
    copies/clones repo into an isolated workspace, applies the LLM's change, runs
    the repo's tests, reports TESTED_PASS/FAIL. Answers "can we apply+test LLM
    code in a sandbox" = yes (mechanism proven with a toy git repo; true isolation
    = Firecracker/E2B, per audit). Posture: sandbox-test + RECOMMEND, don't auto-merge.
  - `resolution_report.py`: `build_resolution_report(state, act_report,
    verification, code_fix)` → detailed markdown (issue, root cause, actions +
    verification, sandbox-tested suggested code fix for manual apply, next steps).
  - Wired into ACT node: builds the report, attaches to act_report, and emits an
    `assistant_message` timeline event → shows up in the SAME incident conversation
    (dashboard + Slack). Verified E2E (node emits resolution_report). 
  - Note: auto-running the sandbox during ACT needs the plan to carry a patch +
    repo + test_command (planner/schema enhancement); mechanism + report support ready.
  - **203 tests pass.**

- **2026-07-26 — Live-streaming slice #1 (of the continuous-live-chat design):**
  - `sre_agent/live_events.py`: event-bus backbone — `InMemoryEventBus` (default,
    tested) + `RedisEventBus` (prod, guarded) + `get_event_bus()`; channels
    `incident:{id}` and `insights`; `publish_incident_event`/`publish_insight`.
  - `agent_runtime.py`: WebSocket endpoints `/ws/incidents/{id}` and `/ws/insights`
    (push, not poll).
  - `incident_timeline.emit_timeline_event` now publishes every event to the bus
    (best-effort) → the incident conversation streams live.
  - `dashboard/lib/useLiveStream.ts`: React hook (WS + auto-reconnect) to consume it.
  - `LIVE_BUS_BACKEND` env. 8 bus tests; **211 total**. esbuild-validated TS.
  - Design roadmap (docs): #1 live stream ✓ → #2 per-incident Slack thread + inbound
    routing to the human-checkpoint queue (two-way chat) → #3 always-on monitor +
    on-call routing → #4 dashboard chat panel. All reuse this bus.

- **2026-07-26 — Live-chat slice #2: two-way Slack war room.**
  - `sre_agent/war_room.py` (framework-agnostic, tested): `WarRoomRegistry`
    (incident↔thread), `format_event_for_slack`, `forward_events` (bus→thread,
    outbound), `route_thread_reply` (inbound: query→answer, steer→checkpoint
    queue+ack, else ignore). Poster/steer_sink injected.
  - `integrations/slack_bot.py`: `build_war_room_app` (thread-reply handler),
    `open_war_room`, `post_incident_message` steer sink (POST /message). Guarded.
  - Canonical runner (`sre_agent.incident_runner.run_incident_investigation`)
    publishes an "opened" lifecycle event to the `incidents` control channel →
    Slack service opens the war room. The quarantined `agent_runtime_tasks`
    module only forwards to that entry point.
  - The loop: agent streams into the thread; on-call replies route back into the
    supervisor's human-checkpoint queue → agent responds in-thread. 6 tests; **217**.
  - Roadmap: #1 stream ✓ #2 war room ✓ → #3 monitor+on-call routing, #4 dash chat panel.

- **2026-07-26 — Live-chat slice #3: always-on monitor + on-call routing.**
  - `sre_agent/monitor.py`: `evaluate_health(signals)` → OK/DEGRADED/CRITICAL +
    flags (env thresholds); `MonitorState` (transition-based dedup); `run_monitor`
    (fetch→evaluate→publish insight→flag on transition, injected fetch/on_flag);
    `build_alert_payload` (flag → Alertmanager webhook, reuses incident-creation
    path); `default_on_flag`/`run_monitor_service` (guarded infra).
  - `sre_agent/oncall.py`: `current_oncall` rotation (pure), `resolve_oncall`
    (env rotation / PagerDuty guarded), `format_slack_mention`.
  - Flow: monitor continuously observes → streams live health to `insights` bus →
    on transition into degraded/critical, opens an incident (webhook) + pages
    on-call. 16 tests; **228 total**.
  - Roadmap: #1 stream ✓ #2 war room ✓ #3 monitor+on-call ✓ → #4 dashboard chat panel.

- **2026-07-26 — Live-chat slice #4: dashboard chat panel (design COMPLETE).**
  - `dashboard/components/dashboard/IncidentChatPanel.tsx`: mirrors the war-room
    conversation via `useLiveStream` (WebSocket) and posts operator replies to
    `/incidents/{id}/message` — same human-checkpoint queue as Slack. So Slack +
    dashboard are two symmetric views of one conversation.
  - esbuild-validated TSX. Mount: drop `<IncidentChatPanel incidentId={id} />`
    into the incident workspace page (not auto-mounted to avoid a blind edit of
    that large page; full `npm run build` verifies on Jayanth's machine).
  - **Live-chat design DONE: #1 WS stream ✓ #2 Slack war room ✓ #3 monitor+on-call ✓
    #4 dashboard chat ✓.** 228 Python tests; frontend esbuild-validated.

- **2026-07-26 — Competitive-audit upgrades BUILT (all 5, guarded adapters):**
  APIs verified via web before coding (Langfuse v3, LiteLLM Router/ChatLiteLLM,
  E2B Sandbox, ITBench).
  - #2 `tracing.py` (Langfuse v3 CallbackHandler → investigation config).
  - #3 `litellm_backend.py` (route_llm → ChatLiteLLM when MODEL_ROUTER_BACKEND=litellm).
  - #5 `code_sandbox.run_code_fix` + `apply_and_test_e2b` (SANDBOX_BACKEND=e2b microVM).
  - #1 `benchmarks/itbench_adapter.py` (map output → ITBench diagnosis shape).
  - #4 `sre_agent/toolsets.py` (registry: 7 integrated + HolmesGPT-style candidates).
  - All guarded/fallback; validated at logic level (live needs the pkg+keys).
  - **243+ tests pass** (added tracing/litellm/e2b/itbench/toolsets tests).
  - COMPETITIVE_AUDIT.md updated: upgrades marked BUILT.

## Housekeeping
- Delete the accidental duplicate nested folder:
  `SRE_Agent_Intermediate/SRE_Agent_Intermediate/`.
