# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable
— a genuinely production-grade, resume-flagship SRE agent platform, not an
"educational subset" of the tools it mirrors (`docs/COMPETITIVE_AUDIT.md`).

## Current milestone
Phase 5 (deterministic remediation pipeline) complete and **live-fire closed
end to end**: a real SLO breach is now detected, investigated, approved in
Slack, remediated with a value-changing cluster write, objectively verified,
and learned from — all in one unattended run. Focus remains **Slack-only
communication robustness** (standing rule: "Slack is the only method of all
types of communication, so it should be robust") and live-fire testing of
varied incident types (Tasks #4/#5).

## Current architecture and invariants
Two independent ACT-phase gates (`PolicyEngine.evaluate_action()` /
`policy_gate.decide()`), plus `EXECUTOR_LIVE` gating
`execute_autonomous_live()`. Slack is the sole approval/communication channel
by design. Tracing is never load-bearing: every Langfuse path degrades to "run
untraced" rather than raising. See `docs/ai/DECISIONS.md`.

Four action dispatch families in `executor.py`: `EXECUTOR_TOOL_MAP` (infra
MCP), `GITHUB_EXEC_TOOL_MAP` (code-change MCP), `NOTIFY_ONLY_ACTIONS` (reaches
a human, mutates nothing), `READ_ONLY_ACTIONS` (reaches the cluster, reads
only). Any "is this a known action?" check must consult all four;
`NON_MUTATING_ACTIONS` = notify-only ∪ read-only is what verification and
skill learning must exclude, so a page or a config dump is never graded a fix.

**Dispatch routes on capability, not on the action's name.**
`executor.live_tool_for_action()` is the single answer to "can Sentinel really
execute this?". `patch`/`config_change` resolve *from their parameters*: to
`patch_resource_limits` with a cpu/memory limit, to `patch_deployment_env`
with `parameters.env`, to nothing otherwise (ConfigMap/Helm/prose → capability
gap). Consulted at plan (`act_phase`), authorization (`mutation_gateway`) and
dispatch (`executor`).

**Env writes are guarded at the edge.** `patch_deployment_env`
read-modify-writes (a k8s strategic merge on `env` replaces the whole list),
refuses credential-named keys and `valueFrom` entries, caps key count/value
length, honours `EXECUTOR_ALLOWED_ENV_KEYS`, returns exact `prior_env`;
`build_command` redacts credential-named values.

**Only a human's Slack "acknowledge" resolves an incident.**
`compute_incident_status` never returns RESOLVED — a verified fix stops at
`PENDING_ACKNOWLEDGMENT` — and `resolved_at_for_status` stamps `resolved_at`
only for RESOLVED. Anything cross-checking "was this fixed?" must accept
`PENDING_ACKNOWLEDGMENT`, or it is asking for a status the graph cannot
produce (this is exactly how the learning gate died; see below).

**An Alertmanager group is one payload with many members.**
`InventorySlowQueries` emits one series per `query` label (7). A payload may
mix firing and resolved members, so `alerts.py` clears a condition only when
no member of the same payload still reports it firing.

**Langfuse span filtering may only drop always-leaf spans** (the SDK drops
filtered spans without re-parenting), and `mark_current_observation` flags the
`trace_run` root, not the individual tool.

## Completed or verified work
Eleven defects were found and fixed by live fire in this campaign. The ones
that shape future work:
- **The self-improving loop could only learn from failure.**
  `verified_learning.assess_learning_eligibility` demanded incident status
  RESOLVED, which no live run can present, so every verified success fell to
  `incomplete`: negative exemplars and negative runbooks only.
- **A mixed-status alert group oscillated.** A resolved member closed the
  tracking incident mid-remediation and later firing members of the *same
  payload* opened fresh ones — two orphan war rooms, two full investigations.
- Incident dedup taken under a Postgres advisory lock (and its
  rollback→commit regression), stale `resolved_at` in all three
  terminal-status writers, and an MCP content-block parse bug.
- Remediation honesty: `escalate` really pages (or reports `SKIPPED`);
  uncapable `config_change` is `blocked`, never a fabricated dry-run
  transcript; `applied X of Y [EXECUTED=…, REFUSED=…]`.
- Langfuse: eight audit gaps closed; `crud.set_org_langfuse_config` now bumps
  every cluster's `execution_context_version` (dashboard-entered keys used to
  do nothing until an API restart). `scripts/langfuse_trace_audit.py` is the
  maintained audit tool and runs *inside* the API container.

## Verification commands and latest results
- `.venv/bin/python -m pytest tests -q -p no:cacheprovider` → **1063 passed,
  3 skipped, 0 failures**.
- **Clean end-to-end live run, incident `dc1712ca`** (2026-09-14): fault
  injected at rev 10 → 7 firing series → **exactly one** incident → correct
  root cause (fault injection in rev 10), honest GitHub downgrade → Slack
  `approve fix` → `1/1 mutating action EXECUTED` (a **value-changing** env
  write, rev 11) → `Verification: RESOLVED (alert no longer firing after
  330s)` → **generative runbook written** (`RB-AUTO-latency-inventory-service`)
  → `pending_acknowledgment`, `resolved_at` NULL. `/app/data/skills.json` holds
  the positive skill: `outcome verified_success`, `verification_status
  RESOLVED`, the real `kubectl set env` command and `prior_env`.
- Edge guardrails verified live: `DATABASE_PASSWORD` refused with `[REDACTED]`
  in the recorded command, `kube-system` refused by namespace, a
  `valueFrom`-sourced key refused, prose-only `config_change` rejected
  `unsupported_action`.

## Known blockers or risks
- **Live mutation surface includes arbitrary env vars** on a deployment in an
  allow-listed namespace; the edge denylist and its tests are the only thing
  between a prompt-injected credential write and the cluster.
  `EXECUTOR_ALLOWED_ENV_KEYS` is unset in the Codespace — a real tenant should
  pin it.
- **Runbook ownership gap**: `#checkout-oncall` owns an inventory runbook, so
  the retrieved runbook can name the wrong on-call channel.
- The **Temporal worker runs image-baked (last-commit) code**; it does not run
  the ACT live path, so the drift is harmless and resolves on image rebuild.
- Open incidents: `c9e6fc3d` (remediation_in_progress), `b3510bdb`
  (awaiting_approval), `dc1712ca` (pending_acknowledgment).
- Known bug, untriaged: `checkout-service app.py:147` — `int(order_id[-1])`
  raises ValueError on non-numeric order ids.
- Alert-triggered traces cannot show `userId`; no prompt linking.
- Codespaces free tier is core-hour capped — `gh codespace stop` when idle.
- The API container is image-baked with no source mount: every change needs
  base64 → `docker cp` into `/app` → md5 → restart, one file at a time. Run
  in-container scripts as `docker exec -w /app sre-agent-api uv run python …`.
  Complex SQL must be piped into `docker exec -i sre-postgres psql` via stdin.
- `.env.local-backup-20260910` (untracked) holds live secrets and is NOT
  matched by `.gitignore`'s `.env` pattern — never commit it; use explicit
  paths in `git add`.
- `.claude/skills/langfuse/` is vendored from `github.com/langfuse/skills` and
  now tracked, so the instrumentation workflow is reproducible;
  `.claude/settings.local.json` stays untracked (global gitignore).

## Next bounded task
Tasks #4/#5: remaining software incident classes (the `checkout app.py:147`
ValueError is a ready-made one) and hardware-type incidents. One real
in-thread Slack *question* remains the last unexercised link of the follow-up
path — the probes exercised the handler, not Slack delivery.
