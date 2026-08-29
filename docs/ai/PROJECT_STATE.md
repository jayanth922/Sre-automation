# PROJECT_STATE.md

## Project objective
Make Sentinel truthful, tenant-isolated, reproducible, and production-operable.

## Current milestone
Phase 4. T01–T10, R01, and P03 are on `master`. The A01–A10 evaluation stack is
now fully pushed with reviewable PRs #3–#12. Local tip remains
`codex/a10-verified-learning` (`e6d577f`).

## Current architecture and invariants
- `compute_incident_status` is the canonical RESOLVED decision point.
- Authenticated v1 resources are organization-scoped; live writes pass the
  mutation gateway.
- Run manifests, recovery oracles, versioned scenarios, structured graders,
  paired statistics, confidence calibration, adversarial gates, release gates,
  and verified-only learning fail closed without evidence.
- Untrusted evidence is never approval or authority.
- Successful memory/skills/runbooks require live execution plus RESOLVED
  verification.

## Completed or verified work
- A01–A10 stacked commits and open PRs:
  - #3 A01 run manifests (green CI, mergeable into `master`)
  - #4–#10 A02–A08 (project CI green; Gemini review red from missing API key)
  - #11 A09 release evaluation
  - #12 A10 verified-only learning
- Focused local verification for A09/A10 passed before push.

## Active problem
A01 is the merge gate for the rest of the stack. Gemini Dispatch review fails
repo-wide without `GEMINI_API_KEY` and is unrelated to product CI. Live Meridian,
blinded labels, paired candidate trials, real calibration, adversarial-model
observations, live traces, and production release bundles remain absent.

## Relevant files
- Stack branches: `codex/a01-run-manifest` … `codex/a10-verified-learning`
- A09: `benchmarks/release_gate.py`, `benchmarks/release/v1/`
- A10: `sre_agent/verified_learning.py` and learning call sites

## Verification commands and latest results
- A01 PR #3: Backend/Frontend/Manifests/Images SUCCESS, MERGEABLE
- A02–A08 project CI SUCCESS; Gemini review FAILURE (missing auth)
- A09/A10 local: release-gate 9 passed; verified-learning suite 45 passed

## Known blockers or risks
- Do not treat Gemini review red as a product regression.
- After A01 merges, retarget A02’s base from `codex/a01-run-manifest` to
  `master`, then continue merging up the stack.
- R02 durable dispatch and residual R05 recovery drift remain open.

## Next bounded task
Merge PR #3 (A01), retarget PR #4 to `master`, then merge A02→A10 in order once
each PR’s project CI is green. Optionally disable or credential Gemini review so
it stops failing the check rollup.
