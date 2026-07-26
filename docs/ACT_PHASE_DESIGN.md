# ACT Phase — Severity-Driven Autonomous Remediation (Design)

This document specifies the flagship capability of the SRE Agent: closing the
OODA loop from **DECIDE** (a proposed remediation plan) into **ACT** (executing
it) under severity-driven autonomy and hard safety guarantees. It covers the
severity model, the autonomy gate, the secure multi-tenant access architecture,
the failure taxonomy, and how it wires into the existing LangGraph runtime.

Everything described here is grounded in current (2026) industry practice; see
Sources at the end.

---

## 1. Principle

> The agent may remediate **autonomously** only when an incident is **low
> severity** *and* the action is **reversible**. Everything else routes to a
> human at the existing checkpoint. Uncertainty always resolves toward *more*
> caution.

This is the pattern the industry converged on in 2026: LLM reasoning bounded by
policy, tiered by severity, with reversible/audited actions and a human in the
loop for anything consequential.

---

## 2. Severity model (`sre_agent/severity_engine.py`)

Severity is computed automatically as **impact × urgency**, the ITIL / PagerDuty
/ incident.io standard — using signals the investigation already collects, so no
human triage is needed to classify.

**Impact** (0–1): error-rate magnitude, SLO breach, user-facing, revenue-
impacting, blast radius (affected services). **Urgency** (0–1): SLO burn rate
(anchored at the 14.4× fast-burn multiple), saturation, error-rate slope, and
whether it is still escalating.

Each score is bucketed high/medium/low and mapped through an impact×urgency
matrix to **SEV1 (critical) … SEV4 (low)**. Two safety rules are built in:

- **Round up when unsure.** If the Reflector's hypothesis confidence is below
  threshold (default 0.5), severity is escalated one level. Missing a real SEV1
  is far costlier than briefly over-mobilizing.
- **Autonomy band.** `is_low_severity()` returns true for SEV3–SEV4 by default
  (`AUTONOMY_MAX_SEVERITY`, env-tunable per tenant — a payment processor's SEV2
  may be another team's SEV1).

All anchors and thresholds are environment variables, so the model tunes per
customer without code changes.

## 3. Autonomy gate (`sre_agent/policy_gate.py`)

For each action the gate returns **AUTONOMOUS**, **REQUIRES_APPROVAL**, or
**BLOCKED**, combining three checks, most-restrictive-wins:

1. **Hard policy** — delegated to the existing `policy_engine.evaluate_action`
   (e.g. never scale-to-0 in prod). A block here is final.
2. **Severity gate** — autonomy only offered for low-severity incidents.
3. **Reversibility floor** — the defensibility keystone:
   - *Reversible* (restart, rollback, revert_commit, escalate): autonomous if
     low severity.
   - *Risky* (scale-up, config_change, patch): autonomous only if low severity
     **and** the action carries a concrete `rollback_plan`.
   - *Irreversible* (scale-to-0, destructive): **never** autonomous, regardless
     of severity.

A whole plan is autonomous only if *every* action is (`decide_plan`).

## 4. Executor (`sre_agent/executor.py`) — Phase 0: dry-run

The Executor translates a cleared action into the concrete `kubectl`/`gh`
command it would run, and in Phase 0 **stops there**, returning the command plus
a **tamper-evident (sha256-chained) audit record**. Calling with
`dry_run=False` raises `NotImplementedError` — a deliberate honesty guarantee
that nothing can mutate real infrastructure until Phase 1 wires the sandboxed
Executor MCP server with least-privilege RBAC.

Run `python examples/act_phase_demo.py` to see the full path over five scenarios
with zero cluster access.

---

## 5. Secure multi-tenant access architecture

Real customers run their own infrastructure; the platform must reach it without
ever holding inbound access into their network. The 2026 converged pattern:

**Egress-only edge relay.** The customer installs a lightweight relay *inside
their own cluster* that opens an **outbound** connection to the SaaS control
plane. No inbound ports, no network peering, no shared credentials; default-deny
ingress and egress. The platform reverse-proxies MCP tool calls back down that
tunnel. This repo is already shaped for it: `edge_mcp_servers` is described as
"the bridge between the SaaS control plane and the customer-target," and the
backend `Cluster` model already stores per-tenant connection details + a token.
The production evolution is simply to **relocate the edge relay into the
customer's boundary and have it dial home**.

**Least-privilege integrations.**
- **GitHub → a GitHub App** (fine-grained, org-installed, permissions scoped to
  exactly what the agent needs, survives employee departure) — not the demo's
  `GITHUB_TOKEN` PAT.
- **Slack → an OAuth app** with scoped bot tokens granted at install.

**Onboarding flow (a real product surface).** Customer signs up → registers a
cluster (receives a token) → `helm install` the edge relay → installs the GitHub
App → authorizes the Slack app. Their secrets stay in *their* boundary; the
control plane only ever holds scoped, revocable tokens.

```
   Customer boundary (their VPC / cluster)          SaaS control plane
   ┌───────────────────────────────────┐            ┌──────────────────┐
   │  workloads → Prometheus/Loki/K8s   │            │  sre_agent       │
   │            ▲                       │  outbound  │  backend/dash    │
   │   edge relay (MCP servers) ────────┼──tunnel──▶ │  (never dials in)│
   │   GitHub App · Slack OAuth         │            └──────────────────┘
   └───────────────────────────────────┘
```

---

## 6. Failure taxonomy (broad coverage)

v1 targets the recurring, autonomously-remediable Kubernetes failure classes,
each with a mapped remediation and default reversibility:

| Failure class | Typical remediation | Reversibility |
|---|---|---|
| CrashLoopBackOff (bad deploy) | rollback / revert_commit | reversible |
| OOMKilled | patch memory limit | risky (needs rollback plan) |
| ImagePullBackOff | patch image / rollback | risky |
| HPA thrashing | patch HPA bounds | risky |
| High error rate | rollback or restart | reversible |
| Config drift | config_change (re-apply) | risky |
| Resource saturation | scale up | risky |
| Dependency failure | escalate / restart dependent | reversible |

To surface more of these, `Target_Client` is slated to grow (database, cache,
a checkout→payment→inventory→DB dependency chain) so failures cascade
realistically. This taxonomy doubles as the scenario matrix for the benchmark
(project #7).

---

## 7. LangGraph integration seam

New OODA flow (additive; existing supervisor/specialist flow is untouched until
the ACT nodes are enabled by flag):

```
swarm → reflector → planner → [severity → policy_gate] → executor → verify → aggregate
                                          │
                                          └─ REQUIRES_APPROVAL / BLOCKED → human checkpoint (existing)
```

- `severity_engine.classify_severity(signals)` runs after the Planner, using the
  Reflector's confidence and the metrics already in state.
- `policy_gate.decide_plan(...)` gates the `RemediationPlan`.
- AUTONOMOUS → Executor (dry-run in Phase 0); otherwise the existing
  `pending_human_messages` checkpoint mechanism handles approval.
- After execution, the **existing verification step** re-queries Prometheus to
  confirm the fix and stores the resolution in Qdrant memory.

---

## 8. Phase roadmap

- **Phase 0 (built):** Severity Engine, Policy Gate, dry-run Executor, audit
  trail, demo, and this design. Zero production risk, fully demoable. 40 unit
  tests passing.
- **Phase 1:** Sandboxed Executor MCP server; enable real Tier-1 (reversible,
  low-sev) execution against `Target_Client` only, with abort window.
- **Phase 2:** Tier-2 with mandatory notification + rollback verification;
  persist audit records to `AuditLog`.
- **Phase 3:** Egress-only edge relay + GitHub App / Slack OAuth onboarding;
  Terminal-Bench adapter and the SRE scenario benchmark for a citable score.

---

## 9. Sources

- PagerDuty — incident severity classification: https://www.pagerduty.com/resources/incident-management-response/learn/incident-severity-classification/
- incident.io — incident management best practices 2026: https://incident.io/blog/incident-management-best-practices-2026
- Autonomous K8s remediation + tiered guardrails: https://edixos.com/en/blog/ai-sre-agents-autonomous-operations/
- Plural — egress-only secure agent: https://www.plural.sh/blog/secure-agent-kubernetes-connection/
- Atlan — secure agent (no inbound): https://docs.atlan.com/secure-agent
- GitHub Apps vs OAuth (least privilege): https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/differences-between-github-apps-and-oauth-apps
- Terminal-Bench 2.1 / Harbor custom agents: https://www.tbench.ai/docs/agent-introduction
