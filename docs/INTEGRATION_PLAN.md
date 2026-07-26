# Integrating Harkirat's "7 Projects" into the SRE Agent — One Unified Platform

**Author's note:** This plan maps the seven projects from Harkirat Singh's video
*"How I Interview AI Engineers + 7 Projects That'd Get You Hired"* onto the
existing `SRE_Agent_Intermediate` codebase. The thesis is simple: you do **not**
need to build seven disconnected demos. Six of the seven are already latent in
this repository's architecture, and building them *here* turns each one from a
throwaway portfolio piece into a real feature of a coherent, autonomous SRE
platform. That single integrated system is a stronger interview artifact than
seven small ones, because it demonstrates system design, not just breadth.

Every external reference below was verified against current (2026) sources; see
the Sources section at the end. Nothing in the mapping is assumed — where a
project's fit is a judgment call, that is stated explicitly.

> **Build status (2026-07-26).** Implemented so far: the Model Router (#6, all
> three axes), the full ACT phase (severity engine + policy gate + dry-run
> executor + Executor MCP server + live path), the SRE domain benchmark (#7),
> durable checkpointing, and a payment-service dependency chain in Target_Client.
> The video's *second half* (five standard interview questions) is mapped in the
> new section below — several answers are already latent in this codebase.

---

## 1. The system you already have

`SRE_Agent_Intermediate` is a four-layer, multi-agent incident-response system:

- **Target_Client** — a deliberately noisy fake customer stack on Kubernetes
  (gateway + checkout/inventory services + load generator + chaos panel) that
  *produces* incidents and Prometheus/Loki/Grafana telemetry.
- **edge_mcp_servers** — five MCP servers (K8s, Prometheus, Loki, GitHub,
  Runbooks) that expose live evidence to the agent as tools.
- **sre_agent** — a LangGraph runtime implementing an **OODA loop**
  (Observe → Orient → Decide → Act): a Supervisor plans and routes to
  specialist agents (Metrics, Logs, GitHub, Runbooks); a Reflector forms a
  hypothesis with a confidence score; a Planner produces a remediation plan
  using runbook RAG and Qdrant incident-memory. Provider-agnostic across
  ollama / groq / gemini / nvidia.
- **backend + dashboard** — Postgres persistence and auth (orgs, users,
  clusters, incidents, timeline events, jobs, SLOs, audit logs) and a Next.js
  operator cockpit that renders the multi-agent conversation live.

There is also a `benchmarks/` folder (`bench_mttr.py`) and a human-in-the-loop
"checkpoint" interrupt mechanism already wired into the Supervisor.

That existing surface is exactly what makes the seven projects plug in cleanly.

---

## 2. The mapping at a glance

| # | Project (from the video) | What it actually is | Where it plugs into this repo | New artifact | Effort |
|---|--------------------------|---------------------|-------------------------------|--------------|--------|
| 6 | **Model router** | Route each task to the best/cheapest suitable model | `sre_agent/llm_utils.py` + new `model_router.py`; called by supervisor/reflector/planner/narrator | New module + policy | **Low** |
| 7 | **Benchmark for a use case** | A rigorous eval for one narrow domain (video's example: TS repos) | `benchmarks/` — extend `bench_mttr.py` into an SRE-agent benchmark driven by the chaos panel | New benchmark suite | **Low–Med** |
| 1 | **Terminal agent** (top Terminal-Bench) | A CLI agent that operates a terminal to finish end-to-end tasks | The stubbed **ACT** phase: a new "Executor" MCP server / specialist that runs `kubectl`/shell remediation | New MCP server + graph node | **Med–High** |
| 2 | **Hermes / OpenClaw agent** | Adopt a dominant open-source agent framework (self-improving, skill-saving) | Alternative runtime for the Executor; its "save every workflow as a reusable skill" maps to Qdrant incident-memory | Runtime adapter | **Med** |
| 3 | **Slack + AI** (Buzz, PromptQL) | A chat-native agent + reliable natural-language → data querying | Feed the existing human-checkpoint queue from Slack/Buzz; add a PromptQL-style NL→PromQL/LogQL tool | Connector + new tool | **Med** |
| 5 | **Generative courses** | AI that generates a course/curriculum on any topic | Auto-generate runbooks / postmortems / on-call training from resolved incidents | New generator + runbook writeback | **Med** |
| 4 | **Superset / T3 Code** | A GUI that orchestrates an army of CLI coding agents across isolated worktrees | The dashboard as a multi-incident **cockpit**: parallel investigations + a plan-review UI | Dashboard feature | **Med–High** |

Recommended build order is by dependency and value-per-effort: **6 → 7 → 1 → 2
→ 3 → 5 → 4**. The router (6) improves every other piece, and the benchmark (7)
gives you a scoreboard to prove that each subsequent addition actually helps.

---

## 3. Project-by-project integration detail

### #6 — Model Router → task-aware model selection *(built)*

**What it is.** Harkirat is explicit that this is **not** OpenRouter. It's a
router smart enough to pick a model on three axes: (a) **task complexity**
(Fable vs GPT-5.5 vs Opus 4.8…), (b) **the caller's remaining credit/budget**,
and (c) **policy** — blocking off-policy requests (e.g. a dev using the
company's Claude Code for a personal project). The goal is company cost control.

**Why it fits.** The repo already abstracts providers behind
`create_llm_with_error_handling(provider)` and `create_llm_with_fallback(...)`,
but it uses a single global `LLM_PROVIDER` for *every* call. Yet the workload is
heterogeneous: supervisor routing and narration are cheap, high-frequency calls;
the Reflector's hypothesis and the Planner's remediation plan are the
high-stakes reasoning calls. Routing the cheap calls to a fast tier and the
hard calls to a strong tier cuts cost and latency without hurting quality.

**Integration points.**
- New `sre_agent/model_router.py`: a pure-logic `select_model(task_type,
  complexity)` returning a `RoutingDecision`, plus `route_llm(...)` that
  delegates to the existing `create_llm_with_error_handling`.
- Call sites: `graph_builder.py` `_reflector_node` and `_planner_node` are wired
  to `route_llm` (→ STRONG). Supervisor/specialists remain candidates.
- **All three axes are implemented:** `RequestContext(remaining_budget,
  off_policy)` — low budget downgrades the tier, an exhausted budget or an
  off-policy request raises `ModelRouterBlocked`.
- Fully env-configurable and backward compatible: disabled ⇒ current behavior.

**This is implemented in this commit** — see `sre_agent/model_router.py` and
`tests/test_model_router.py`.

### #7 — Domain Benchmark → an SRE-agent benchmark

**What it is.** The video's example is "a benchmark for TypeScript open-source
repos." The transferable skill is building a rigorous eval for one narrow
domain.

**Why it fits.** `benchmarks/bench_mttr.py` already fires synthetic Alertmanager
webhooks and measures MTTR against published baselines. The `Target_Client`
chaos panel can inject *known* faults, which gives you ground truth. That is
90% of a benchmark harness already.

**Integration points.** Extend into a suite that scores, per injected scenario:
MTTR, **root-cause accuracy** (did the Reflector's hypothesis name the right
service/commit?), **remediation-plan correctness**, and false-positive rate,
across `pass^k` runs — plus a leaderboard across model-router configurations.
This is what proves projects #1, #2 and #6 actually improve the system.

### #1 — Terminal Agent → the ACT phase executor

**What it is.** An agent that drives a real terminal to complete tasks, scored
on Terminal-Bench (tbench.ai). "Project #1: Build a Terminal Agent (Claude Code
/ Codex)" per the video.

**Why it fits.** The OODA loop in this repo currently stops at DECIDE — the
Planner emits a plan but the ACT phase (`PolicyGate → Executor`) is stubbed and
"automatic execution is disabled." A terminal agent is exactly the missing
"hands": given an approved `RemediationPlan`, it executes `kubectl rollout undo`,
pod restarts, scaling, etc., then the existing verification step re-queries
Prometheus to confirm the fix. `create_revert_pr` / `revert_pr` already exist on
the GitHub agent, so code-level remediation is half-built too.

**Integration points.** A new `edge_mcp_servers/mcp_servers/executor/` MCP server
exposing sandboxed shell/kubectl tools; a new `executor` graph node gated by
`policy_engine.py`; Terminal-Bench-style task cases to validate it safely before
it ever touches the live cluster.

### #2 — Hermes / OpenClaw → open-source agent runtime + skill memory

**What it is.** Adopt one of 2026's dominant open-source agent frameworks
(Nous Research **Hermes Agent** or **OpenClaw**). Hermes's headline feature is
that it *saves every workflow it learns as a reusable skill*.

**Why it fits.** That "compounding skills" property is a near-exact match for the
repo's Qdrant incident-memory (`memory_store.py`, `recall_similar_incidents` /
`store_incident_memory`), which already stores resolved incidents and injects
similar ones into the Planner. Wrapping the Executor (#1) in Hermes/OpenClaw
turns successful remediations into named, replayable skills.

**Integration points.** A runtime adapter behind the Executor node; persist
learned skills alongside incident memory so the Planner can propose "apply skill
X" for a recurring incident class.

### #3 — Slack + AI → chat-native steering + reliable NL→data

**What it is.** Two references. **Buzz** (Block/Jack Dorsey) is an open-source,
Nostr-based Slack/GitHub rival built for human+AI collaboration with a signed,
hash-chained audit trail. **PromptQL** (Hasura) is a plan-execute-verify natural
-language→data agent that avoids NL-to-SQL hallucination.

**Why it fits.** The repo already has a human-checkpoint interrupt queue
(`load_pending_human_events`, `pending_human_messages`) and an `AuditLog` model.
A Slack/Buzz connector lets on-call engineers receive incident alerts and steer
the investigation ("focus on logs", "pause") straight from chat — feeding the
exact mechanism that already exists. PromptQL's pattern maps to a new tool that
turns "show checkout error rate for the last hour" into a *verified* PromQL/LogQL
query, and Buzz's signed-event log maps onto `AuditLog`/`AuditEvent`.

**Integration points.** A chat connector service that POSTs to the incident
follow-up endpoint; a `nl_query` MCP tool (or specialist) implementing
plan→generate-query→execute→verify against Prometheus/Loki.

### #5 — Generative Courses → auto-generated runbooks & postmortems

**What it is.** AI that generates a personalized course/curriculum on any topic
(paradigm.study).

**Why it fits.** The repo has a Runbooks MCP server over markdown plus RAG in the
Planner. The same generative technique, pointed at a *resolved* incident,
produces the teaching artifact SRE teams actually want: an auto-drafted runbook,
a postmortem, and an on-call training module for that incident class — which then
feeds back into the Runbooks corpus and improves future RAG. A learning loop, not
a standalone course tool.

**Integration points.** A post-resolution `narrative.py`-style generator that
writes markdown into the runbooks directory and links it on the incident
timeline; optionally a "Learn" tab in the dashboard.

### #4 — Superset / T3 Code → the multi-incident cockpit

**What it is.** Open-source desktop GUIs (Superset `superset-sh/superset`, T3
Code by Theo) that orchestrate an "army" of CLI coding agents across isolated
git worktrees, with review, terminal, and parallelism.

**Why it fits.** This repo *already* ships the operator dashboard that renders one
supervisor orchestrating specialists. The Superset/T3 pattern generalizes it to
(a) run and monitor *multiple* incident investigations in parallel with
isolation, and (b) provide a first-class **review/approval UI** for remediation
plans — the `plan_pending_approval` flow already exists in the backend but
deserves a proper cockpit. This is the front-end capstone that showcases
everything underneath.

**Integration points.** Dashboard views for a parallel investigation board and a
plan-diff/approve panel; backend job/queue plumbing (the `Job` model already
exists) for concurrent runs.

---

## 4. Why this is one project, not seven

Read top to bottom, the integrations compose into a single narrative: the system
**observes** (Target_Client + edge MCP), **reasons** (LangGraph OODA), **routes
each thought to the right model** (#6), **acts** through a terminal executor
(#1) built on an **open-source, skill-learning agent runtime** (#2), is
**steerable from chat with reliable data queries** (#3), **turns every resolved
incident into training material** (#5), is **measured by a domain benchmark**
(#7), and is **driven from a multi-incident cockpit** (#4). Each project is a
feature of the same platform, sharing the same state, memory, and audit trail.

---

## 4b. Interview-question hardening (the video's second half)

Beyond the 7 projects, the video walks through five "standard interview
questions" for a Devon-like agent company. They map directly onto this SRE agent,
and answering them in-code is high-value resume signal:

| # | Interview question | How it maps here | Status |
|---|--------------------|------------------|--------|
| Q1 | Architecture: sandbox per user, scale up/down | Executor runs in sandboxed pods; edge relay is per-tenant (egress-only) | Designed (ACT_PHASE_DESIGN §5) |
| Q2 | **Crash durability** — resume a long task from a checkpoint (Temporal) | LangGraph checkpointer wired per incident_id; `CHECKPOINTER_ENABLED` | **Built** |
| Q3 | Context management — compaction/summarization | Follow-up flow reloads incident context; compaction is a candidate | Partial |
| Q4 | **Evals** — deterministic, SWE-bench/terminal-bench style | SRE domain benchmark scoring MTTR + root-cause/remediation/severity/safety | **Built (#7)** |
| Q5 | **Observability** — agent failure traces, auto-switch provider | `thought_traces` + incident timeline; model-router fallback chain switches providers | Partial |

The standout is **Q2**: the video emphasizes crash durability most, and it is now
implemented — a restarted process resumes the same investigation from its last
checkpoint. Q1's sandboxing/scaling and Q5's provider auto-switch are also
partially realized (sandboxed executor + edge relay; router fallback chain).

## 5. Sources

- Terminal-Bench leaderboard — https://www.tbench.ai/leaderboard/terminal-bench/2.1
- Terminal-Bench repo — https://github.com/harbor-framework/terminal-bench
- Block Buzz repo — https://github.com/block/buzz ; coverage — https://decrypt.co/374026/jack-dorseys-block-launches-buzz-a-nostr-based-slack-and-github-rival-for-ai-agents
- Hasura PromptQL — https://hasura.io/promptql ; MCP server — https://github.com/hasura/promptql-mcp
- Hermes Agent vs OpenClaw — https://decrypt.co/364211/what-is-hermes-open-source-ai-agent-openclaw-competitor
- T3 Code — https://betterstack.com/community/guides/ai/t3-code/ ; Superset — https://github.com/superset-sh/superset
- Generative courses (paradigm.study) — referenced in the video; concept: AI-generated personalized curricula
