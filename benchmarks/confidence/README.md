# Confidence calibration

A06 treats confidence as a task-specific empirical probability, not as an LLM
authorization signal. Benchmark runs preserve diagnosis and remediation
self-reports only when an exact structured outcome is available. The record
schema pins each pair to scenario, dataset, candidate configuration, A05 pair
ID — and, since schema v2, to how the observation was obtained.

Build reliability evidence with:

```bash
python benchmarks/confidence_eval.py reports/sre-bench-confidence.jsonl \
  --task remediation \
  --config-fingerprint "$BENCH_CONFIG_FINGERPRINT" \
  --report-output reports/remediation-reliability.json \
  --artifact-output config/remediation-confidence-v1.json \
  --artifact-version remediation-v1 \
  --false-autonomy-cost 20 \
  --abstention-cost 1
```

The report includes reliability bins, Brier score, log loss, ECE, MCE, and
optional drift against a reference JSONL. Artifact bins use equal-frequency
grouping, Laplace smoothing, and adjacent-violator pooling so calibrated
probability is monotonic.

## The threshold is chosen, not asserted

Every distinct calibrated probability is a candidate operating point, and the
artifact records all of them as a `threshold_curve`. Each point carries the
evidence behind it: how many actions would have been autonomous, how many
abstained, the true and false autonomy counts, the observed success rate, its
Wilson lower bound, coverage, expected cost per action, and whether it clears
the support and Wilson floors.

Cost is what picks among them. `--false-autonomy-cost` is what one wrong
autonomous action costs; `--abstention-cost` is what one human approval round
trip costs. Only the ratio matters. The recorded selection rule is:

> lowest expected cost per action among operating points whose autonomous
> population meets `minimum_threshold_support` and whose Wilson lower bound
> meets `required_wilson_lower`; ties resolve to the higher threshold

The floors survive as a safety constraint — an operating point that cannot be
shown to work is never eligible however cheap it looks — but they no longer
*select* the threshold. Raising `--false-autonomy-cost` buys less autonomy at a
stricter threshold; lowering it buys more. `always_abstain_cost`,
`always_autonomous_cost` and `selected_cost` are recorded side by side so the
threshold can be compared against both degenerate policies, and
`autonomy_beats_abstention` is false when the cheapest eligible point still
costs more than sending everything to a human.

## Only live evidence can grant autonomy

Each record declares an `evidence_source` of `live_benchmark`, `replay`, or
`synthetic`. `benchmarks/sre_bench.py` is the only sanctioned producer of
`live_benchmark` records: a real agent against a real injected fault.

A corpus that is not entirely `live_benchmark` still earns a full reliability
picture and a full threshold curve, but its artifact carries
`autonomy_threshold: null` and an `autonomy_blocked_reason` saying why. This is
a load-time contract, not documentation: `load_calibration_artifact` recomputes
the curve and the three costs from the bins and the cost model, re-derives the
selected point from the recorded rule, and rejects any artifact whose threshold
did not come from an all-`live_benchmark` corpus — so a hand-edited and
re-digested artifact fails to load.

A valid remediation artifact can be configured with
`REMEDIATION_CONFIDENCE_CALIBRATION_PATH`. Runtime also requires
`SENTINEL_CONFIG_FINGERPRINT` to exactly match the artifact's A01 configuration.
Missing, invalid, mismatched, diagnosis-only, under-supported, and
evidence-blocked artifacts all fail closed: mutations require approval, and
`ActReport.autonomy_blocked_reason` says which of those it was. Notify-only
escalation remains non-mutating.

No calibration artifact or reference dataset is committed here. Both must come
from real, content-addressed benchmark outcomes.

## Schemas

- `v1/` — the original record and artifact schemas, retained so existing
  evidence stays readable.
- `v2/` — current. Records require `evidence_source`; artifacts require the cost
  model, threshold curve, per-policy costs, evidence sources, blocked reason,
  and selection rule.
