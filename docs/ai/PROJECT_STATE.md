# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable
— a genuinely production-grade, resume-flagship SRE agent platform, not an
"educational subset" of the tools it mirrors (`docs/COMPETITIVE_AUDIT.md`).

## Current milestone
Phase 5 — deterministic remediation pipeline (Temporal-orchestrated, two
manual approval gates per issue, per-issue isolated chat/PR, concurrent-
incident correlation) — **complete and live-fire validated end-to-end as of
2026-09-03**, including a real GitHub PR write against
`jayanth922/meridian-shop` (test-18, PR #1, closed) and the bounded
retry-loop/close-incident flow (test-11). Full design in
`docs/ai/PHASE5_DETERMINISTIC_PIPELINE_PLAN.md`; implementation history and
every bug found/fixed along the way is in git log and
`docs/ai/DECISIONS.md` — not restated here.

Also done since: Hermes actor backend fully removed (single backend now,
`LocalTerminalRuntime` — see DECISIONS.md "Hermes removal"), execution-trace
dashboard timeline, and a UI/UX pass (error states, loading states, dead CSS,
accessibility labels — responsive/mobile layout deliberately deferred).

Also done, 2026-09-04 (resource-optimization pass, unrelated to Phase 5
logic): Temporal (`sre_agent/temporal_client.py`) now supports both a local
dev-server (`temporalio/temporal:latest server start-dev`, opt-in
`COMPOSE_PROFILES=local-temporal`) and Temporal Cloud
(`TEMPORAL_HOST`/`TEMPORAL_API_KEY`, TLS auto-enabled with an API key);
self-hosted Langfuse (clickhouse/minio/langfuse-web/-worker) deleted
entirely from `docker-compose.yaml`, `.env.example`, and the Helm chart —
Langfuse Cloud (free tier) is now the only tracing backend. Commit
`4804da5`, pushed to `origin/master`. Same day: `sre_agent/code_sandbox.py`
(an opt-in E2B microVM backend added earlier the same day) removed entirely
— zero production callers, pure duplication of `sandbox_workflow.py`'s
K8s-Job-based sandbox, which is the sole code-fix sandbox mechanism now. See
`docs/ai/DECISIONS.md` "E2B sandbox backend removed". Small frontend fix same
window: `dashboard/app/(dashboard)/clusters/[id]/layout.tsx` moved its
missing-cluster redirect out of render into `useEffect` (commit `cafa5c1`).

Also done, 2026-09-08: resumed `cuddly-winner-659v67gv695hrxjw` (was
`Shutdown`, not reaped — `gh codespace ssh` auto-starts a shutdown
codespace). `cd edge_mcp_servers && docker compose --progress=quiet up -d
--build` — all 8 MCP servers (k8s/prometheus/loki/github/runbooks/executor/
github-exec/sandbox) came up healthy. Separately, `platform/`'s
`sre-dashboard` container had exited; running `docker compose up -d
dashboard` from inside `platform/` (no `--env-file`) recreated `sre-agent-api`
with blank Postgres creds because **the root `.env` lives at
`/workspaces/Sre-automation/.env`, not `platform/.env`**, and plain `docker
compose` in `platform/` doesn't see it — broke auth
(`InvalidPasswordError`). Fixed with `docker compose --env-file ../.env up -d
--force-recreate sre-agent-api`; confirmed healthy. **Always pass `--env-file
../.env` (or run from repo root) for any `platform/` compose command run
directly instead of via `main_start.sh`.** Dashboard confirmed reachable at
`https://cuddly-winner-659v67gv695hrxjw-3002.app.github.dev`. Full
account-creation→incident→resolved manual e2e test still not run this
session.

Also done, 2026-09-05/07 (`.devcontainer/devcontainer.json` fixed for
GitHub Codespaces): Codespace creation was failing 5+ times in a row with
"failed to start SSH server" even though the API reported `state:
Available` — root cause was the base `ubuntu-24.04` devcontainer image
needing the `sshd` feature explicitly (GitHub's own error message named the
fix); added `ghcr.io/devcontainers/features/sshd:1` (commit `098569a`).
Along the way also stripped `kubectl-helm-minikube`/`kind`/`node` (not
needed just to run `main_start.sh`, which is pure `docker compose`) and
switched `uv` from a devcontainer feature — which was silently failing and
falling back to an unusable Alpine "recovery container" — to installing it
via its own install script in `postCreateCommand` (commit `36a0ff4`), then
restored the `python:3.12` feature (commit `6b533b2`) after finding
`platform/start.sh` needs `python3` on the host to generate
`SECRET_KEY`/`CREDENTIAL_ENCRYPTION_KEY`/etc. on first boot. Verified
working end-to-end on a fresh codespace: SSH connects immediately, and
`cp .env.example .env && ./main_start.sh` builds and brings up the full
`platform/` stack (postgres/redis/qdrant/sre-agent-api/sre-dashboard, all
healthy, dashboard on `:3002`). The `edge_mcp_servers` half of
`main_start.sh` (7 MCP server images) was mid-build, not yet confirmed
working, when the codespace was stopped on request — not a known-bad state,
just unverified. Codespace `cuddly-winner-659v67gv695hrxjw` (repo
`jayanth922/Sre-automation`, branch `master`) is stopped, not deleted.

## Current architecture and invariants
Two independent ACT-phase gates (`PolicyEngine.evaluate_action()` /
`policy_gate.decide()`), plus `EXECUTOR_LIVE` env var gating
`execute_autonomous_live()`. `EXECUTOR_TOOL_MAP` (`executor.py:38-44`) routes
`action_type` → live MCP tool name. See `docs/ai/DECISIONS.md` for the full
rationale log (Task #16 root causes, cloud-dev-env-via-rsync convention).

## Completed or verified work
Pre-Phase-5: Task #16 live-fire validation; model tiering/prompt caching +
cross-provider routing; Temporal code-fix verification; runbook RAG/NL-query
(PR #53); ad hoc Slack chat memory (PR #54). Phase 5 A–F + cutover, retry
loop, real PR-write path, execution-trace view, Hermes removal, UI/UX pass:
all done, live-fire validated, merged to `origin/master` — see git log for
commit-by-commit detail.

## Active problem
Phase 5 is done. `edge_mcp_servers` (all 8) and `platform/` (dashboard +
sre-agent-api + postgres/redis/qdrant) are now both confirmed healthy on
`cuddly-winner-659v67gv695hrxjw` as of 2026-09-08 — see note above. Three
items remain: the pending manual end-to-end frontend test (account creation
→ cluster connect → incident → resolved/closed, still not run), and two
deferred-until-Phase-5-done items per standing instruction
(`decision-production-grade-upgrade` memory): the **responsive/mobile layout
pass** and the **AIOpsLab domain benchmark** (neither started).

## Relevant files
- `sre_agent/incident_remediation_workflow.py` — the two-gate Temporal
  workflow, retry loop, close-incident handoff.
- `sre_agent/actor_runtime.py` — deterministic actor (`LocalTerminalRuntime`),
  sole backend.
- `sre_agent/graph_builder.py::_act_gate_node` — deterministic-pipeline
  detection/deferral.
- `sre_agent/service_topology.py`, `sre_agent/incident_correlation.py` —
  correlation-gate adjacency (Phase A).
- `edge_mcp_servers/mcp_servers/sandbox_real/` — sandbox-verify MCP server.
- `edge_mcp_servers/mcp_servers/github_exec/server.py` — `create_fix_pr`/
  `create_revert_pr` (real PR-write path).
- `sre_agent/approval_flow.py`, `sre_agent/api/v1/remediation_gates.py`,
  `backend/models.py::RemediationGateApproval` — gate persistence/API.
- `dashboard/app/(dashboard)/clusters/[id]/incidents/[incidentId]/page.tsx`
  — gate approval panel + execution-trace timeline.
- `sre_agent/war_room.py`, `sre_agent/integrations/slack_bot.py` — Slack
  gate-decision commands.
- `docs/ai/DECISIONS.md` — durable technical decisions; check before
  re-deriving root causes already documented there.
- `sre_agent/temporal_client.py` — Temporal bootstrap (local dev-server vs
  Cloud, `TEMPORAL_ENABLED`/`TEMPORAL_API_KEY`).

## Verification commands and latest results
`.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` → 870
passed, 3 skipped (2026-09-10, uncommitted per-org Langfuse work). `npx tsc
--noEmit -p dashboard/` clean. Re-run: `pytest`, `ruff check .`, `mypy .` —
see `docs/ai/DECISIONS.md`/git log if a specific historical count is needed.

## Known blockers or risks
- GitHub Codespaces free tier is capped on core-hours — stop the active
  codespace when idle (`gh codespace stop -c <name>`). `gh codespace list`
  is the source of truth for which one(s) currently exist/are billing; a
  stopped codespace has previously disappeared/404'd within ~1 day
  (observed with `glowing-lamp-p9qj7jp756cjq5`), cause unconfirmed — don't
  assume a stopped codespace is still resumable without checking
  `gh codespace list` first.
- Approval requests (`ApprovalRequest` and `RemediationGateApproval`) expire
  ~30 min (`APPROVAL_TTL_MINUTES`) — see resolve→refire recipe below if
  re-testing live execution during that run.

Also done, 2026-09-08 (later same session): stood up the `kind-meridian`
target cluster inside the codespace per the plan recorded in commit
`fb3a593` — installed `kubectl` v1.31.0 + `kind` v0.24.0 on the codespace
(neither was present after the devcontainer strip-down), `kind create
cluster --name meridian --config k8s/kind-config.yaml` (from
`github.com/jayanth922/meridian-shop`, cloned to `/tmp/meridian-shop` on the
codespace — not part of this repo), then `./start.sh`, which auto-detects
the kind context, builds+loads the 6 Meridian images, deploys the app +
Prometheus/Loki/Grafana/Alertmanager into namespace `meridian`, patches
their Services to NodePort, and bridges the kind node onto the platform's
`sre-platform-network` Docker network (+ CoreDNS restart) so
`sre-agent-api` resolves from pods. All 11/11 pods came up ready. Verified
`host.docker.internal:9090`/`:3100` (the platform's existing defaults —
`Settings` Prometheus/Loki fields can stay **blank**) are reachable from
`mcp-prometheus`/`mcp-loki`. Created the `meridian-alertmanager-secret`
(cluster token from Sentinel's Clusters→Connect) and restarted
`deployment/alertmanager`; confirmed it can reach `sre-agent-api:8080/ping`
by name. One gotcha hit: `kind create cluster` first failed with
`permission denied` on `~/.kube/config.lock` — `~/.kube` was root-owned;
fixed with `sudo chown -R vscode:vscode ~/.kube`. Note: `kind`/`kubectl`
and the `meridian-shop` clone live only on the codespace VM's disk /
`/tmp` — neither survives a codespace rebuild, only a stop/resume (this
codespace's `/tmp/meridian-shop` and installed binaries are not committed
anywhere and would need to be redone from this note if the codespace is
ever rebuilt rather than resumed).

Also done, 2026-09-08 (same session, after the kind-meridian e2e prep):
Settings-page cleanup, prompted by real usage — connecting `kind-meridian`
surfaced that Connections showed Prometheus/Loki as OK while Endpoints
showed blank fields with no hint of the platform-default URL in effect.
Fixed at the time by returning non-secret `effective_prometheus_url`/
`effective_loki_url` and showing "Using the platform default: <url>" —
**superseded later this same session, see the "no silent defaults" entry
below; that hint and both response fields no longer exist.** Then, since
the platform authenticates to GitHub with a repo URL + PAT only, removed
the GitHub App
multi-tenant install flow entirely (it was never wired to any UI besides
the one button just deleted): `sre_agent/multitenant/github_app.py` and
`tests/test_multitenant_github_app.py` deleted; `relay_auth.py` now reads
`context.credentials.get("github_token")` directly (no more App-token
minting attempt); `execution_context.py` no longer threads
`github_app_installation_id` into the credentials dict;
`api/v1/multitenant.py` no longer has the `/clusters/{id}/github-app/*`
routes (Slack OAuth routes untouched); `backend/schemas.py`/`crud.py` no
longer reference the field; `.env.example`'s `GITHUB_APP_*` vars removed;
`dashboard/lib/console.ts` type trimmed. **Left as-is, deliberately:**
`backend/models.py:173`'s `github_app_installation_id` DB column and its
original Alembic migration — dropping a live Postgres column needs a new
migration and is a more consequential, harder-to-reverse change than
deleting unused Python, so it was intentionally not bundled into this
cleanup; the column is now fully unused dead storage. All edits synced to
`cuddly-winner-659v67gv695hrxjw` via `gh codespace cp --expand` +
`docker cp` + `docker restart sre-agent-api` (image-baked container, not
volume-mounted); confirmed clean restart (`Application startup complete`,
`/ping` healthy from both the API and the dashboard container). Nothing
this session has been committed to git yet — pending user request.

Also done, 2026-09-08 (fresh-start reset, same session): per user request to
wipe all data and start clean, `TRUNCATE ... RESTART IDENTITY CASCADE` on all
15 app tables in `sre-postgres` (excluding `alembic_version`) + Redis
`FLUSHALL` + `sre-agent-api` restart. Before reconnecting `kind-meridian`,
found it was NOT actually quiet at baseline: 4 alerts firing from a real
standing bug (`payment-service`'s `/charge` did `int(amount) % int(order_id)`
— `order_id` is a string like `ord-8vyr7mz3`, so every charge raised
`ValueError`, cascading into `checkout-service` error-rate alerts) plus a
real `inventory-service` in-memory leak. User chose to patch both and reset
pods rather than leave them or paper over with a fault-toggle. Fixed the bug
(`loyalty_points = int(amount) % 100`), rebuilt+`kind load`ed the image,
`kubectl rollout restart deployment payment-service inventory-service -n
meridian`; **committed and pushed to `origin/master` of
`jayanth922/meridian-shop`** as commit `de76893` (pushed from the local
machine via a patch export, since the codespace's own git credential helper
is scoped only to this repo, not `meridian-shop`). Confirmed via Prometheus
polling that alerts fully clear within ~5-7 min post-fix (longer than the
"~2-5 min" figure below — the `rate(...[5m])` window in these specific
queries keeps pre-fix errors counted until they age out of that window,
*then* the 2 min `for:` timer runs). Created a fresh org+admin user
(`POST /auth/register`) and a fresh `kind-meridian` Cluster row
(`POST /api/v1/clusters`, id `bcbd9577-...`, namespace `meridian`), then
re-wired `meridian-alertmanager-secret`'s `cluster-token` key to the new
cluster's token and restarted `deployment/alertmanager`; verified the new
token authenticates by curling `sre-agent-api:8080/ping` from inside the
alertmanager pod with it.

**Found, not yet fixed:** `edge_mcp_servers/.env`'s single-tenant fallback
`GITHUB_TOKEN`/`GITHUB_REPO` are literal unfilled placeholders (`ghp_...`,
`your-org/your-repo`), and the new `kind-meridian` Cluster row has no
per-cluster `github_token`/`github_repo` set either (per-cluster values take
priority over the env fallback in `relay_auth.py`). Net effect: GitHub-based
remediation (revert PRs, code-change fixes) has no working credential right
now. This blocks the user's explicitly next-requested test ("software side
faults first, like code changes in github") until a real PAT for
`jayanth922/meridian-shop` is supplied and wired in — via
`PATCH /api/v1/clusters/{id}` (`github_token`, `github_repo` fields) or the
Settings page, once a PAT is provided.

Also done, 2026-09-08 (later same session): fixed the "Reconnect Slack"
button in both `clusters/[id]/settings/page.tsx` and `.../team/page.tsx`
surfacing a hardcoded generic error instead of the real backend detail
(`e.response?.data?.detail`); added `k8s_api_server`/`k8s_token` Settings
fields (schema/CRUD already supported them, UI didn't expose them). Then,
per explicit user instruction ("there shouldn't be any defaults anywhere
... just the configuration/fallback scope, not dry_run or retries"),
removed every silent config-fallback found in that scope: `IncidentCreate.
severity`/`SLOCreate.window_days`/`InvitationCreate.role`+
`expires_in_hours` are now required (no Pydantic defaults);
`sre_agent/metrics_profile.py` rewritten — `DEFAULTS` replaced with
`REQUIRED_KEYS` + `EXAMPLES` (placeholder text only), raises
`MetricsProfileNotConfigured` instead of silently merging guessed metric
names; `services.py` no longer falls back to `os.getenv("PROMETHEUS_URL"/
"LOKI_URL")`, wraps `mp.resolve()` and returns 503 with the missing-fields
list instead; dead global `sre_agent/api/v1/metrics.py` (`os.getenv`-only,
zero frontend/test callers) deleted along with its `agent_runtime.py`
router mount; `slack_oauth.resolve_slack_bot_token` no longer falls back to
`os.getenv("SLACK_BOT_TOKEN")` — an org's Slack connection must be set via
OAuth or the manual-token Settings field. Backfilled the `kind-meridian`
Cluster row (`bcbd9577-...`) with explicit `prometheus_url`/`loki_url`
(`http://host.docker.internal:9090`/`:3100`, confirmed reachable from
`sre-agent-api`) and a `metrics_config` matching its real Prometheus rules
(read from the live `prometheus-config` ConfigMap) so it keeps working
under the new no-fallback code — `cpu_query`/`mem_query` reused the old
platform-default query text since no cAdvisor/kubelet scrape target is
configured for this demo cluster (pre-existing gap, unchanged). Ran the
full `pytest` suite (856 passed, 3 skipped) — one static router-auth
allowlist (`tests/test_route_auth_coverage.py`) still listed the deleted
`metrics.py`, fixed. Three architecturally-similar patterns were flagged to
the user as out-of-scope-pending-confirmation; resolution below.

**Item 1 (confirmed "leave as-is"):** `edge_mcp_servers/mcp_servers/*_real/
server.py` single-tenant env-var fallbacks. Investigated
`relay_credentials.py`: only 6 fields relay per-request
(`github_token`/`repo`, `k8s_api_server`/`token`, `notion_*`) — no
Prometheus/Loki relay headers exist, so `prometheus_real`/`loki_real`'s env
vars are their *sole* config mechanism, not a fallback alongside another
path. `k8s_real`/`github_real` have a genuine, deliberately-documented
dual-mode: relay credentials for multi-tenant SaaS vs. static
`KUBECONFIG`/in-cluster ServiceAccount for self-hosted single-tenant. User
chose to leave this layer untouched — closed, no code changed.

**Item 2 (confirmed, fixed 2026-09-08 later same session):** the platform's
`LLM_PROVIDER="anthropic"` bootstrap default, but *only* at the per-cluster
resolution layer — `sre_agent/cluster_context.py::resolve_llm()` now treats
`provider` exactly like `model`/`base_url`/`api_key` already did: env
fallback applies *only* when `cluster is None` (local dev / self-hosted,
mirrors the item-1 precedent); a bound cluster with no `llm_provider` gets
`None`, and `authorize_llm()` fails closed with a clear
`UnauthorizedLLMConfigError` ("No LLM provider configured for this
cluster...") instead of silently picking anthropic. Removed the matching
redundant fallback in `agent_runtime.py::_build_runtime` (now
`require_supported_provider(context.llm_provider)` directly) and in the
OODA-state metadata assembly (`runtime.context.llm_provider`, no `or
os.getenv(...)`). `backend/crud.py::create_cluster`/`update_cluster` now
only call `resolve_authorized_llm` when an LLM override is actually being
set (guarded on `llm_provider` truthy) so plain cluster creation, and
clearing LLM fields back to "unconfigured," don't get rejected — validation
happens at investigation-start (`ExecutionContext.from_cluster`), same
posture as `MetricsProfileNotConfigured`. Settings page copy updated
(`"Platform default"` → `"Not configured"`, explains investigations refuse
until a provider is set). **Deliberately left alone** (self-hosted/local-dev/
CLI-only `LLM_PROVIDER` reads, not per-cluster silent substitution):
`provider_config.py`'s `DEFAULT_PROVIDER`/`validate_startup_config()` process
boot default, `agent_runtime.py`'s "no cluster_id" local-mode branch + `
__main__` CLI arg default, `graph_builder.py`/`model_router.py`/
`output_formatter.py`/`run_manifest.py`/`llm_utils.py`/`config.py`'s
downstream `os.getenv("LLM_PROVIDER", "anthropic")` reads (dead code for any
cluster-scoped run now that the value is guaranteed resolved before
metadata is built; still the correct default for the local/self-hosted
"no cluster at all" path). Added tests in `tests/test_cluster_llm.py`
(no-default-for-bound-cluster, env-fallback-only-with-no-cluster,
unconfigured-provider-raises) and fixed one pre-existing test
(`tests/test_namespace_scope.py::test_from_cluster_fails_closed_without_
namespace`) whose fixture cluster had `llm_provider=None` and now needs an
explicit provider to isolate the namespace check it's actually testing.
Full suite: 860 passed, 3 skipped. Hot-deployed
(`cluster_context.py`/`crud.py`/`agent_runtime.py`) to
`cuddly-winner-659v67gv695hrxjw`; `sre-agent-api` restarted clean/healthy.

**Item 3 (confirmed, fixed 2026-09-08 later same session):** the real bug was
`war_room_service.py::maybe_open_war_room` — called per-incident (with a
`cluster_id` available at the call site) but reading `os.getenv(
"SLACK_BOT_TOKEN")` directly, ignoring per-org routing entirely. Fixed:
signature is now `maybe_open_war_room(incident_id, cluster_id, summary)`; it
resolves the incident's cluster → organization →
`multitenant/slack_oauth.resolve_slack_bot_token(org)`, with no env fallback
(mirrors `integrations/jira.py::maybe_create_jira_issue`'s per-cluster
pattern). Call site in `agent_runtime.py` (`_run_graph_impl`) updated to pass
`str(cluster_id)`. `integrations/slack_bot.py`'s standalone socket-mode bot
(`run_slack_bot()`) was deliberately **left unchanged**: Slack Bolt's
socket-mode `AsyncApp` binds one bot token per running process, so an
operator-set `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN` env pair is structurally a
single-tenant-only feature, not a per-org routing bug — same precedent as
item 1. Added `test_maybe_open_war_room_uses_org_token_not_env` and
`test_maybe_open_war_room_noops_without_org_token` to
`tests/test_war_room_service.py`. Full suite: 862 passed, 3 skipped.
Hot-deployed (`war_room_service.py`/`agent_runtime.py`) to
`cuddly-winner-659v67gv695hrxjw`; `sre-agent-api` restarted clean/healthy.
Committed and pushed to `origin/master`.

Also done, 2026-09-10 (local dev environment session): brought up local
Temporal (`platform/docker-compose.yaml`'s `local-temporal` profile) —
crash-looped at first with a misleading `"unable to open database file: out
of memory (14)"`; real cause was the fresh `platform_temporal_data` named
volume being root-owned while the container runs as uid 1000, fixed via a
throwaway root `alpine` container doing `chown -R 1000:1000`. `.env`'s
`TEMPORAL_ENABLED` flipped to `"true"`.

Then implemented **per-organization Langfuse tracing** end-to-end (each org
sets its own Langfuse project keys, mirroring `Organization.slack_bot_token`
— no fallback to any operator-wide default once an org exists, so an
unconfigured org just runs untraced rather than risking cross-tenant trace
mixing): `backend/models.py` (`Organization.langfuse_public_key`/
`_secret_key`/`_host`, new migration `d4e5f6a7b8c9`, applied), `backend/
schemas.py` (`LangfuseConfigSet`, `OrgResponse` exposes public key/host only),
`backend/crud.py::set_org_langfuse_config`, `POST /organization/langfuse`
(`sre_agent/api/v1/members.py`, admin-only). Credential threading: `sre_agent/
tracing.py::get_langfuse_callback`/`tracing_callbacks` take an optional
`org_langfuse` dict and use the Langfuse SDK's per-`public_key` client
registry (`Langfuse(public_key=...)` + `CallbackHandler(public_key=...)`) so
two orgs' traces never share a client; `checkpointer.py::thread_config` and
`execution_context.py::ExecutionContext.org_langfuse_credentials()`
(`None` only for the true no-org local/CLI runtime) carry it through to
every graph-invocation call site — all 5 in `agent_runtime.py` plus the one
in `mission_control.py`'s approval-resume path (`get_agent_runtime` now
takes an explicit `organization` row, fetched by callers before their DB
session closes, avoiding a `DetachedInstanceError` on lazy-relationship
access). Frontend: new "Langfuse" section in `clusters/[id]/team/page.tsx`
(public/secret key + optional host, admin-only, mirrors the existing Slack
section) and `Org` type in `dashboard/lib/console.ts` extended. Verified:
full `pytest` suite green (870 passed, 3 skipped — includes new tests in
`test_tracing.py`/`test_execution_context.py` and a bumped Alembic-head
assertion in `test_canonical_models.py`), `tsc --noEmit` clean, `ruff check`
clean on every new/touched line (pre-existing unrelated lint debt in
`agent_runtime.py`/`mission_control.py` left as-is). `sre-agent-api` and
`sre-dashboard` images rebuilt and confirmed healthy locally with the new
`/organization/langfuse` route live in the OpenAPI schema. Not yet done:
syncing any of this to the Codespace, and no manual click-through of the new
UI section in a browser (no browser tool available this session).

Also done, 2026-09-11 (Codespace session): closed the Temporal-worker gap and
proved it end-to-end. Added a `temporal-worker` service to `platform/
docker-compose.yaml` (runs `sre_agent.sandbox_worker`, same "local-temporal"
profile as `temporal`) — pushed, pulled onto the Codespace, and permanently
enabled there (`TEMPORAL_ENABLED="true"`, `COMPOSE_PROFILES="local-temporal"`
now set in the Codespace's `.env`, not just local). Fixed the same
`platform_temporal_data`-volume-root-ownership crash-loop documented above,
now confirmed to recur across environments — same fix
(`chown -R 1000:1000`). Found and fixed a second, previously-undocumented gap
blocking any real sandbox run: `MCP_SANDBOX_URI` was entirely absent from
`.env`/`.env.example` (added, port 4007/sse, alongside the other `MCP_*_URI`
vars), and `edge_mcp_servers/docker-compose.yaml`'s `mcp-sandbox` published
on `127.0.0.1:4007:3000` — unreachable via `host.docker.internal` from the
platform stack's separate Docker network (confirmed by raw TCP connect: a
0.0.0.0-bound port like `redis`'s was reachable, a 127.0.0.1-bound one was
refused). Rebound to `4007:3000` (fix scoped to `mcp-sandbox` only; the other
edge MCP services share the same 127.0.0.1 restriction and are a known,
unaddressed risk — see below). Also created the missing `sentinel-sandbox` k8s
namespace and set `SANDBOX_ALLOWED_IMAGES=busybox:1.36` on the Codespace.
Verified: started a real `CodeFixVerificationWorkflow` (workflow id
`e2e-smoke-fa67fc26`) against the seeded `kind-meridian` cluster and a
synthetic test `Incident` row — reached `COMPLETED` with verdict `RESOLVED`,
and `kubectl get events -n sentinel-sandbox` showed real `busybox:1.36` Jobs
created, run (baseline hit `BackoffLimitExceeded` as designed, candidate
`Completed`), and torn down by `cleanup_activity`. Temporal task-queue
pollers confirmed live throughout.

Then checked whether the same `127.0.0.1`-binding bug hit the other 7 edge
MCP services (`mcp-k8s`/`mcp-prometheus`/`mcp-loki`/`mcp-github`/
`mcp-runbooks`/`mcp-executor`/`mcp-github-exec`) — it did, and severely: raw
TCP connect from `sre-agent-api` to all 7 was refused, and
`docker logs sre-agent-api` showed `Failed to load MCP tools` on every
recent graph invocation, silently degrading to `mcp_tools = []` (0 MCP
tools, only the local `get_current_time` tool) — the agent had been running
with no k8s/metrics/logs/github/runbooks/executor access at all on the
Codespace. Rebound all 7 to `<port>:3000` (no `127.0.0.1:` prefix), same as
`mcp-sandbox`. Re-verified: all 8 MCP ports now reachable via
`host.docker.internal` from `sre-agent-api`. Not yet re-verified with a real
graph invocation (no alert was firing at check time to trigger one
naturally) — the fix is confirmed at the network layer, not yet observed
producing a nonzero MCP-tool-count log line.

## Next bounded task
If continuing the Langfuse work: manually click through the new "Langfuse"
section on `clusters/[id]/team` in a browser (save keys, confirm the
"connected" badge and `org.langfuse_public_key` round-trip), then decide
with the user whether/how to sync to the Codespace.

Otherwise, longer-standing: wire a real GitHub PAT for
`jayanth922/meridian-shop` into the `kind-meridian` Cluster row (see "Found,
not yet fixed" above) — needed before any software-side fault test. Per
standing sign-off policy, do not ask the user to paste the PAT into chat;
have them enter it directly via the dashboard Settings UI outside the
conversation. If dropping the now-dead `github_app_installation_id` column
is ever wanted, it needs a new Alembic migration (not yet written).

## Resolve→refire recipe (for re-testing checkout-service fault, on the
Codespace's `kind-meridian` cluster)
1. `kubectl set env deployment/checkout-service -n meridian ERROR_RATE=0`
2. Poll `kubectl exec -n meridian deploy/prometheus -- wget -qO-
   http://localhost:9090/api/v1/alerts` until no `alertname` (~2-5 min).
3. Confirm incident status flips to `resolved` in Postgres.
4. `kubectl set env deployment/checkout-service -n meridian ERROR_RATE=0.6`
   to fire a genuinely new incident (dedup matches by title on non-resolved
   incidents only). Planner's proposed actions are non-deterministic across
   identical fault runs.
