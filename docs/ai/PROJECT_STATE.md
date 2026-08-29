# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
A01–A10 remain open as PRs #3–#12. Runtime PRs: R02 #13, R03 #14, R08 #15.
R04 per-cluster LLM authorization is implemented on `codex/r04-per-cluster-models`.

## Current architecture and invariants
- Cluster LLM provider/model/base_url/key resolve at run start, are allowlist-
  authorized (`ALLOWED_LLM_PROVIDERS`, `ALLOWED_LLM_MODELS`, `LLM_RUN_BUDGET`),
  fingerprint runtime caches, and appear exactly in run metadata manifests.
- Cluster-pinned model/endpoint/key bypass LiteLLM and cross-provider fallback so
  two tenants cannot share process-global brain settings.

## Completed or verified work
- R04: authorized resolve, router pin, create/update validation, concurrent
  two-cluster cache isolation tests.

## Active problem
A-stack and earlier runtime PRs still unmerged. Shared multi-replica admission
and truthful heartbeats (R07) remain open.

## Relevant files
- `sre_agent/cluster_context.py`, `sre_agent/execution_context.py`,
  `sre_agent/model_router.py`, `tests/test_cluster_llm.py`

## Verification commands and latest results
- `pytest tests/test_cluster_llm.py tests/test_execution_context.py`: 14 passed

## Known blockers or risks
- Operator must configure allowlists in production; empty model allowlist permits
  any model id on an allowed provider.

## Next bounded task
Open R04 PR, then R07 truthful heartbeats or Redis-backed shared admission.
