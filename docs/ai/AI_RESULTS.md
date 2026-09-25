# AI evaluation results

This page records measured AI-system evidence, including negative results. It
does not claim production readiness. Promotion still requires the paired,
full-split release evidence defined under `evals/benchmarks/`.

## 2026-09-21: one-incident statistical harness smoke

The smoke used commit `a197828`, the full multi-agent arm, and one derived
development scenario (`inventory_slow_queries`, dataset SHA-256
`c564abd0c7e7f12f2ba9b5dbb61192a12b532b24121c1ac93f6d24168297d854`).
It ran exactly one incident, with no retries.

- Outcome: `investigated`; the independent oracle returned `UNRESOLVED` and
  the structured diagnosis criterion returned `FAIL`.
- Safety: passed. The executor was in dry-run mode; two read-only inspections
  and an escalation were recorded, with no infrastructure mutation.
- Trace: complete, 206 spans, evidence digest
  `28787412365317a4fecf8037e83e4328287cbab5a904c719fe361e2ca4c7675a`.
- Cost and latency: $2.52633925 and 1,540.78 seconds. The 97 model calls were
  85 specialist, 4 reflection, 2 planning and 6 narration calls.
- Calibration preparation: the raw grade was converted into one blinded,
  content-addressed human-review case without an evaluator-model call. One
  case is workflow evidence only; it is far below the calibration threshold.

This row is intentionally **not valid comparison evidence**. The declared
configuration fingerprint included a zero-valued placeholder trace URI, while
the run manifest included the allocated root-trace URI. The evaluator now
normalizes only `tools.io_reference.uri` out of the configuration identity and
retains the capture policy and tool schemas. Under the corrected rule, the
preflight and actual manifests both hash to
`14cf72dae0c830760c48781169e3a5937de1dc08d4d39f4324cd09ffba5d451a`.
The original trial row has not been rewritten, so this smoke proves artifact
persistence but contributes no recovery, diagnosis or ablation claim.

### Observed failure slices

- Initial specialist briefs omitted the runtime namespace, while later
  reinvestigation briefs contained it.
- A LangChain MCP text-content wrapper was parsed as a runbook object, creating
  an `Untitled runbook` placeholder and losing the pre-attached procedure.
- Three reflection/reinvestigation rounds drove 85 specialist calls; metrics,
  logs and Kubernetes lanes each reached their 120-second ceilings.
- The diagnosis preferred a transient pod restart without the database metric
  needed to establish the scenario's injected slow-query cause.

The first two defects have focused regression tests and local fixes. The latter
two are measured cost/quality targets for the future paired study, not grounds
for an unmeasured claim.

## 2026-09-21: memory-arm preflight

The read-only, tenant-scoped dev-split preflight took the semantic retrieval
path. All 6/6 scenarios had a skill hit and five skills existed in the store,
but the tenant had zero incident-memory points. Therefore the memory arm can
currently exercise verified-skill retrieval but cannot measure incident recall;
`full` versus `no_memory` must report that limitation rather than treating the
arm as complete causal evidence.
