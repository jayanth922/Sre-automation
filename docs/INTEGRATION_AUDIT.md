# Integration Audit — "properly wired" vs "bolted on"

An honest, self-critical pass over everything we built, classifying **how deeply
each feature is actually integrated into the running system** — not whether it
has tests. The goal is to find what's real vs. what's a capability sitting on a
shelf, and decide what to properly wire.

## Status legend

- **Running** — wired into the default request/data path; works when the app runs.
- **Gated-off** — wired into the path but behind a flag that defaults to off, so
  it does nothing until enabled.
- **Bolted-on** — a tested module that **nothing in the running system calls**.
  A capability, not a feature.
- **Not mounted** — (frontend) component exists but isn't rendered anywhere.
- **Optional adapter** — external-tool swap, correctly guarded; needs the
  package/keys to do anything. Fine to be optional.
- **Standalone tool** — a CLI/benchmark that's meant to be run on its own. Fine.

## Audit

| Feature | Status | What's actually wired | What "properly integrated" needs |
|---|---|---|---|
| Model router (tiers) | **Running** | `route_llm` drives reflector/planner/supervisor/specialists; on by default | ✔ — but the **budget/off-policy axes are Bolted-on**: nothing passes a `RequestContext`, so those never fire |
| Live event bus + `/ws` | **Running** | `emit_timeline_event` publishes every event; WS endpoints exist | ✔ (works when app runs) |
| Payment-service cascade | **Running** | Real K8s service; checkout→payment via `PAYMENT_URL` | ✔ (needs the cluster) |
| **ACT phase** (severity→gate→executor→verify→resolution) | **Gated-off** | Fully wired into the graph, but `ACT_PHASE_ENABLED=false` by default | Make **dry-run ACT the default** so the system actually classifies severity, proposes + verifies — the flagship shouldn't be off |
| Reflector / Planner (OODA) | **Gated-off** | Only run when ACT is on | Same flag; comes on with ACT dry-run |
| Skill memory | **Gated-off** | Runs in `act_gate`; proposes into planner | On with ACT |
| Resolution report | **Gated-off** | Built + emitted in `act_gate` | On with ACT |
| Verification | **Gated-off** | Runs on the live ACT path only | On with ACT (+ needs metrics MCP for live) |
| Checkpointer / durability | **Gated-off** | Wired to `compile()` + all astream sites; `CHECKPOINTER_ENABLED=false` | Turn on (Redis backend) so resume actually works |
| Live executor + github-exec | **Gated-off** | ACT + `EXECUTOR_LIVE` gated; servers in compose | Enable per-environment; genuinely should stay opt-in for safety |
| Generative runbooks | **Gated-off** | `RUNBOOK_AUTOGEN` + ACT | On with ACT (writes to corpus) |
| Langfuse tracing | **Gated-off** | Wired into investigation config | Set keys; fine to be optional |
| **NL-query** | **Bolted-on** | `handle_chat_message` exists; **no HTTP endpoint, not an agent tool** | Add a `/api/v1/query` endpoint AND register it as an agent tool so specialists can call it |
| **Context compaction** | **Bolted-on** | Module only; **nothing calls it** | Wire into the graph state prep / supervisor before long LLM calls |
| **Observability recorder** | **Bolted-on** | Module only; **no `track()` in any node** | Wrap the graph nodes in `track(...)`; expose a `/api/v1/agent/metrics` route |
| **Concurrency / sandbox** | **Bolted-on** | Module only; **no slot acquired** around investigations | Acquire a slot in `agent_runtime_tasks` per incident |
| **Monitor + on-call** | **Bolted-on** | `run_monitor_service` exists; **never started** | Launch it as a FastAPI startup background task; it's the whole "continuous/proactive" story |
| **War-room forwarder** | **Bolted-on** | Routing logic real; **forwarder/transport never started** | Start a Slack service that subscribes to the bus + opens rooms |
| **Code-fix sandbox** (`code_sandbox.py`) | **Removed (2026-09-04)** | Was `Bolted-on` — `run_code_fix` never called in an incident; deleted rather than wired, since `sandbox_workflow.py`'s Temporal/K8s-Job sandbox already covers this live — see `docs/ai/DECISIONS.md` "E2B sandbox backend removed" | — |
| IncidentChatPanel | **Not mounted** | Component exists; not in any page | Mount into the incident workspace |
| Cockpit | **Running-ish** | Page + nav link exist | ✔ reachable (per-incident live status needs checkpointer) |
| Frontend redesign | **Mockup** | Concept only | Build the real app (the current thread) |
| LiteLLM / Redis-PG checkpointer / ITBench | **Optional adapter** | Guarded; need pkg/keys | Fine as optional |
| Hermes | **Removed (2026-09-03)** | Was an optional `AgentRuntime` backend; fully removed after safety review — see `docs/ai/DECISIONS.md` "Hermes removal" | — |
| Terminal agent / sre_bench / toolsets | **Standalone tool** | Run on their own | Fine |

## Headline findings (the honest summary)

1. **The flagship is off by default.** The entire ACT phase — severity, policy
   gate, executor, skills, verification, resolution report, and the Reflector/
   Planner OODA loop — only runs when `ACT_PHASE_ENABLED=true`. Out of the box the
   system is a read-only advisor. For "production functionality," dry-run ACT
   should be the **default** (it's safe — no mutations), so the system actually
   does the reasoning it was built for.
2. **Seven features are pure bolt-ons** — NL-query, context compaction,
   observability, concurrency, monitor, war-room, code sandbox. Each is tested in
   isolation but **nothing running ever calls it**. These are the clearest
   "capability on a shelf" items.
3. **The whole "continuous live + two-way chat" story isn't actually running** —
   the bus + WS publish, but the monitor and the war-room service are never
   started, so nothing proactively observes or converses. The pieces exist; the
   wiring to *run* them doesn't.
4. **Frontend is a page-pile + a mockup**, not an integrated app.

## Recommended priority (proposed — to decide together)

**Tier 1 — make the core actually run (highest leverage, small changes):**
- a. Default **ACT dry-run on** → the flagship reasoning runs out of the box.
- b. Wire the **bolt-ons that have a natural home**: observability `track()` in
     nodes, concurrency slot per investigation, context compaction before long
     calls, NL-query as an endpoint + agent tool. (Real code-path integration.)
- c. Start the **monitor** and **war-room** as app startup background tasks →
     the continuous/proactive story becomes real.

**Tier 2 — the production frontend** (its data is now genuinely flowing):
- Real design system + app shell + live views wired to `/ws/*`, mount the chat
  panel, Recharts. Built as one integrated app.

**Tier 3 — depth:**
- Planner-emitted patches → code-fix sandbox in the ACT loop; RequestContext so
  the router's budget/blocking axes fire; enable durability (Redis) by default.
