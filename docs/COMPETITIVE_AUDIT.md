# Competitive Audit — Where This Project Stands (2026)

An honest, feature-by-feature comparison against established commercial products
and popular open-source projects. The goal is calibration, not marketing.

**Framing.** This is a *portfolio / learning flagship*, not a production product.
So the useful question isn't "does it beat HolmesGPT" (it doesn't) — it's "are the
design choices aligned with where the industry actually is, and where is each
feature a real subset vs. a toy?" Verdicts use four labels:

- **Genuinely competitive** — design and (for its scope) implementation hold up.
- **Comparable design** — the architecture matches the industry pattern; the
  implementation is a simplified subset.
- **Educational subset** — the right idea, a deliberately small version.
- **Design-only** — specified, not really implemented.

## Interface question: dashboard vs CLI vs chat

Not either/or. The market has converged on **API-first core + chat as the primary
surface + dashboard for review/approval + CLI for ops**:

- The leading OSS SRE agent, **HolmesGPT**, is used mostly by *messaging you in
  Slack* with the finding and the fix; **K8sGPT** is CLI/operator-first; commercial
  tools (**Cleric**, **incident.io**) are Slack + a web war-room for the timeline
  and approvals.
- So: keep the dashboard (incident timeline, audit trail, and the **plan
  approval** step genuinely need a UI — that's the human-in-the-loop surface), but
  treat **Slack as the primary interaction** (we built that in #3) and keep the
  **FastAPI core API-first** so dashboard/CLI/chat are all just clients. For a
  résumé, the dashboard also demonstrates full-stack breadth.

Verdict: **keep the dashboard, lead with chat, stay API-first.** Dropping the
dashboard would remove the approval/audit surface every serious tool has.

## Feature-by-feature

| Capability | Established / popular comparators | Where we stand |
|---|---|---|
| Multi-agent incident **investigation** | **HolmesGPT** (Robusta+MS, CNCF, ReAct over 30+ toolsets), **K8sGPT**, **Aurora**, Cleric/Traversal/NeuBird (commercial) | **Comparable design** — same ReAct/tool-calling pattern over metrics/logs/k8s/GitHub; far fewer integrations than HolmesGPT's 30+. |
| **Autonomous remediation** (severity-gated) | **Resolve.ai** (autonomous-first), **Shoreline**, **Robusta** automations, **Cleric** (read-only) | **Comparable design** — our severity-gated middle ground (auto low-sev, approval high-sev) is exactly the industry's "tiered autonomy"; execution is a demo. |
| **Severity engine / policy gate** | incident.io / PagerDuty severity models; **OPA/Gatekeeper** for policy | **Comparable design**, simpler. Impact×urgency + reversibility floor is sound. |
| **Executor** + guardrails | Shoreline, Robusta actions, **Event-Driven Ansible**, Rundeck | **Educational subset** — dry-run + allow-list + kubectl via MCP. 5-action library as of 2026-09-04 (restart, scale, rollback, patch_resource_limits, **recreate_pod** — single-pod delete/recreate, narrower blast radius than a full rolling restart) enforced identically at both the policy-gate reversibility classifier and the edge-side allow-list; real tools (Shoreline, Rundeck) still have far richer, cluster-object-type-spanning action libraries. |
| **Skill memory / self-improving** | **Mem0**, **Letta/MemGPT**, **Zep**, **Hermes** | **Comparable design** — a signature→skill store, persisted (`SKILL_STORE_PATH` volume) and tenant/cluster-scoped, now with real semantic recall as of 2026-09-04 (`SemanticSkillStore` in `sre_agent/skill_store.py`): the same self-hosted Qdrant instance `memory_store.py` already uses, embedding skills via the shared `fastembed` singleton, tenant-filtered `query_points`, additive to (never replacing) exact keyword match, and fallback-safe to keyword-only if Qdrant/embeddings are unreachable. Still missing Mem0/Letta/Zep's temporal graph memory (relationships between memories over time) — this is flat semantic + keyword recall, not a graph. |
| **Model router** | **LiteLLM** (per-user budgets, cost routing, 5 strategies), **RouteLLM**, **NotDiamond** (powers OpenRouter Auto Router; +39% on SRE benches), **Martian** | **Comparable design** — `MODEL_ROUTER_BACKEND=litellm` is the platform default (2026-09-04): every call now goes through the real LiteLLM client (`langchain-litellm`), which derives its model string from whichever provider/model is already resolved for that cluster/tier — no separate LiteLLM-only config needed, and it falls back to the direct provider SDK path if construction fails. Our SRE-task-tier policy sits on top, same framing as before. Still missing LiteLLM's per-user budget/cost-routing layer itself. |
| **Domain benchmark** | **ITBench** (IBM, AAAI'26, SRE/FinOps/CISO, leaderboard), **AIOpsLab** (Microsoft), **SREGym** | **Educational subset** — ours is a home-grown mini-benchmark. The credible path is to run against ITBench/AIOpsLab (note: frontier models score <50% on ITBench-AA — the bar is high). |
| **NL → verified query** | **PromptQL** (Hasura), Grafana/PromQL copilots, Text2SQL | **Educational subset** — the plan→generate→**verify** pattern is exactly right. Template set grew 2026-09-04 from 5 to 8 intents (added error **count** via `increase()` vs. rate, database-query latency, and dependency up/down status — the latter two used metrics that were already allow-listed but had no template path to reach them); still a small, fixed intent set backstopped by an LLM fallback that's re-verified against the same allow-list before it can execute. |
| **Generative runbooks / postmortems** | incident.io AI postmortems, Rootly AI, Rundeck | **Educational subset** — LLM-authored runbook + course generator; mature tools tie postmortems to the full incident record. |
| **Durability / resume** | **Temporal**, **LangGraph checkpointer** (what we use), Restate | **Comparable design** — using the standard LangGraph checkpointer is the idiomatic choice; Temporal is heavier-duty. |
| **Agent observability** | **Langfuse** (most-adopted OSS), **Arize Phoenix**, **LangSmith**, **AgentOps** | **Comparable design** — real Langfuse `CallbackHandler` (`sre_agent/tracing.py`), attached to every graph invocation via `checkpointer.py::thread_config()`, so every LLM/tool/chain span gets tokens, cost, latency, and trajectory automatically; self-hosted in `platform/docker-compose.yaml`. No Phoenix/AgentOps alternative backend, and OpenTelemetry export isn't wired, but this is the real most-adopted-OSS tool itself, not a homegrown recorder. |
| **Context compaction** | LangMem, standard context-engineering | **Educational subset** — standard keep-tail + summarize technique. |
| **Concurrency / sandbox** | **E2B**, **Firecracker**, **Modal**, **Daytona** | **Educational subset** — a slot limiter + temp dir. Real per-tenant isolation is microVM/container (Firecracker/E2B). |
| **Terminal agent** | **Claude Code**, **Codex**, **OpenHands**, **pi**; terminal-bench leaderboard | **Educational subset** — a real run/observe loop with sub-agent orchestration, but nowhere near the leaders' capability. |
| **Autonomous actor framework** | **Hermes Agent** (Nous), **OpenClaw** | **Removed (2026-09-03)** — evaluated as a pluggable `AgentRuntime` backend, then removed after a safety review found it added risk (no filesystem sandbox, undocumented toolset surface) without functional gain over the first-party `LocalTerminalRuntime` actor already in place; see `docs/ai/DECISIONS.md` "Hermes removal". |
| **Secure multi-tenant access** | HolmesGPT/Robusta relay; egress-only agents (Plural, Atlan) | **Comparable design** — built (PR #49, 2026-08-30): `sre_agent/multitenant/` mints per-tenant GitHub App installation tokens and Slack OAuth tokens instead of one shared static token, relayed edge-side via `X-Sentinel-Relay-*` headers (`relay_auth.py`/`edge_mcp_servers/relay_credentials.py`) with fallback to the old single-tenant env vars. Simpler than a full egress-only network boundary (Plural/Atlan), but no longer just specified. |

## Honest overall positioning

The system is a **coherent, well-architected educational integration** that mirrors
the real 2026 direction of the field. Two things are genuinely to its credit:

1. **The architecture matches the frontier.** HolmesGPT is a *read-only* ReAct
   investigator; the whole industry conversation is about adding a *safe autonomous
   ACT layer* on top — which is exactly what this project's severity gate + policy
   + executor is. Sitting between Cleric (always-approve) and Resolve.ai
   (autonomous-first) is a defensible, current position.
2. **The breadth is unusual for a portfolio piece** — observe → reason → route →
   act → learn → measure, as one platform, is more than most demos attempt.

Where it is clearly behind (and should say so): integration coverage (HolmesGPT's
30+ toolsets), memory sophistication (Mem0/Letta), routing (LiteLLM), real
sandboxing (Firecracker/E2B), and observability (Langfuse). Most individual
features are deliberately small versions of mature tools.

## Highest-leverage upgrades — now BUILT (as real, guarded adapters)

All five are implemented against the current SDK APIs, guarded so they fall back
to the built-in path when the optional package/keys aren't present:

1. **ITBench adapter** (`benchmarks/itbench_adapter.py`) — maps our output to the
   IBM ITBench SRE diagnosis shape for real, comparable numbers.
2. **Langfuse tracing** (`sre_agent/tracing.py`) — Langfuse v3 CallbackHandler
   wired into the investigation config; enable with `LANGFUSE_PUBLIC_KEY`.
3. **LiteLLM router backend** (`sre_agent/litellm_backend.py`) — `route_llm`
   builds via `ChatLiteLLM` (the maintained `langchain-litellm` package) when
   `MODEL_ROUTER_BACKEND=litellm`, now the platform default (2026-09-04);
   `litellm`/`langchain-litellm` are real `pyproject.toml` dependencies, not
   optional installs, and the per-tier model is derived from whatever
   provider/model the cluster already resolved. Our SRE tier policy stays on
   top.
4. **Toolset breadth** (`sre_agent/toolsets.py`) — explicit registry of the 7
   integrated MCP toolsets + HolmesGPT-style candidates to add behind the same
   interface.
5. **E2B sandbox** (`sre_agent/code_sandbox.py` → `run_code_fix`,
   `SANDBOX_BACKEND=e2b`) — microVM isolation for running LLM code fixes.

These swap toy components for the tools the industry actually uses. #1, #2 and
#3 are wired in and on by default; #4 is a reference registry; #5 (E2B) is
validated at the logic level but stays opt-in — it needs a real, paid E2B API
key, so we don't force it on by default.

## Sources

- HolmesGPT (CNCF): https://github.com/HolmesGPT/holmesgpt · K8sGPT: https://github.com/k8sgpt-ai/k8sgpt
- AI-SRE landscape: https://www.bobbytables.io/p/the-ai-sre-startup-landscape · Cleric: https://cleric.ai · Resolve.ai, Traversal, NeuBird
- Model routing: LiteLLM https://docs.litellm.ai/docs/proxy/auto_routing · RouteLLM · NotDiamond https://github.com/Not-Diamond/awesome-ai-model-routing
- Benchmarks: ITBench https://github.com/itbench-hub/ITBench · AIOpsLab (Microsoft) · SREGym
- Agent memory: Mem0 · Letta/MemGPT · Zep — https://machinelearningmastery.com/the-6-best-ai-agent-memory-frameworks-you-should-try-in-2026/
- LLM observability: Langfuse https://langfuse.com · Arize Phoenix · LangSmith
- Hermes Agent: https://hermes-agent.nousresearch.com/docs/guides/python-library
