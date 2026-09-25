# Project state

## Project objective
Make Sentinel a truthful, tenant-isolated, reproducible, and cost-conscious
SRE agent. Deterministic policy and durable state—not model prose—control
writes, approvals, status transitions, and operator-facing claims.

## Current milestone
The repository-layout and documentation cleanup is complete on the isolated
`codex/repository-layout` worktree. The first frontend quality slice is also
complete: lint is clean, and incident/approval status rendering now matches the
durable backend enums. Root, incident, service, and first-cluster routing have
been audited; settings/team wiring and a local operator-flow smoke remain.

## Current architecture and invariants
- Application packages live under `src/`, the web application under `apps/`,
  edge MCP services under `services/`, evaluations under `evals/`, deployment
  assets under `infra/`, and the Meridian reference integration under
  `examples/meridian/`.
- `mutation_gateway.authorize_and_execute()` is the sole fresh-incident,
  tenant, policy, idempotency, and audit boundary. Successful actions are not
  replayed after process death; a cleared incident cannot restart remediation.
- Specialist reads and model turns are bounded; verification remains
  deterministic. Approval cannot override a hard block.
- Benchmark evidence is content-addressed to scenario, code, configuration,
  rubric, model, and trace artifacts. Missing or inconsistent evidence blocks
  a release claim.
- Recovery, structured quality, safety, and complete cost traces govern
  release. Diagnosis-only evidence cannot authorize rollout.

## Completed or verified work
- The v2 benchmark has 22 scenarios, independent recovery oracles, structured
  grading, adversarial cases, confidence evaluation, paired statistics, four
  ablation arms, and a content-addressed release gate. Dataset v3 adds three
  split-specific `missing_data` scenarios.
- Deterministic calibration and one-row statistical-smoke builders validate
  source/rubric provenance without paid model calls or holdout leakage.
- One authorized smoke exposed missing namespace context, MCP wrapper parsing,
  repeated reinvestigation, specialist timeouts, and a configuration-identity
  defect. Focused fixes are present; the original row remains immutable and
  non-comparable.
- Specialist execution is structurally cost-bounded to six model turns per
  specialist, one reinvestigation round, a recursion backstop, and a 120-second
  timeout. Limits are clamped, configurable, observable, and fingerprinted.
- Slack incident threads remain the sole conversation surface; the unused,
  unbounded `/api/v1/chat` route was removed.
- The repository now has an OSS-style layout, MIT license, contribution and
  security policies, a documentation index, current onboarding/operations
  docs, and regenerated architecture diagrams. Historical design/audit docs
  are explicitly archived. Eleven orphaned demo screenshots, obsolete
  handoffs/plans, and the credential-bearing NVIDIA session note were removed.
- Frontend lint was reduced from 29 errors and 4 warnings to zero without rule
  suppression. Polling and timestamps initialize safely. Five unmounted
  duplicate components—including two obsolete dashboard-chat surfaces—were
  removed and pinned absent by reachability tests.
- All nine durable incident states have explicit operator labels and tones.
  Remediation-gate status casing now matches the backend enum, so pending gates
  and Slack approval instructions cannot silently disappear. The incident row
  preserves the approval cue when graph status is temporarily unavailable.
- Root and cluster loading now distinguish API failure from a genuinely empty
  tenant, preventing an outage from presenting false cluster onboarding or a
  false missing-cluster redirect.

## Active problem
The frontend compiles cleanly. No local API/dashboard process is listening, so
an authenticated operator smoke is pending; starting shared Docker services
from this worktree could interfere with the active Claude checkout.

## Relevant files
- Frontend: `apps/dashboard/`.
- Runtime: `src/sre_agent/`; persistence/API models: `src/backend/`.
- Evaluation: `evals/benchmarks/`; public evidence:
  `docs/ai/AI_RESULTS.md`.
- Local and production deployment: `infra/`; edge services:
  `services/edge_mcp_servers/`.
- Layout and documentation contracts: `tests/test_docs_truthfulness.py`,
  `tests/test_module_reachability.py`, and `tests/test_service_topology.py`.

## Verification commands and latest results
- `uv run pytest -q` → **2122 passed, 7 skipped** (2026-09-24).
- `bash scripts/dev/quickstart_smoke.sh` → passed (secret scan, compile,
  documentation contracts, Helm RBAC, and WebSocket defaults).
- Release fixtures reproduce with `python -m benchmarks.make_release_fixtures
  --check`; all shell files pass `bash -n`; Docker Compose config is valid.
- Dashboard `npm run lint`, `npx tsc --noEmit`, and `npm run build` pass.
- Latest frontend wiring contract slice → **52 passed** plus Ruff clean.
- `git diff --check` passes; all Mermaid sources have regenerated SVG peers.

## Known blockers or risks
- No paid benchmark campaign is authorized. Existing smoke evidence is not a
  quality or ablation claim; semantic criteria still need two independent
  blinded labelers, adjudication, and measured agreement.
- Dataset v3 needs the Meridian image rebuilt and deployed; measured tenant
  memory lacks a provenance-pinned incident corpus for a valid memory ablation.
- Rotate the Anthropic key, Slack token, and NVIDIA credential previously
  exposed in local output or deleted documentation. Removing a file does not
  remove secrets from Git history; rewrite published history if applicable.
- Never stage `.agents/`, `.env.local-backup-20260910`, or `.env.bak-*`; never
  use `git add -A`.

## Next bounded task
Audit settings/team form fields and error states against their response schemas.
Then run a local authenticated smoke when isolated services and test credentials
are available. Keep paid benchmarks paused until explicit authorization.
