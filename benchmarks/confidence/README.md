# Confidence calibration

A06 treats confidence as a task-specific empirical probability, not as an LLM
authorization signal. Benchmark runs preserve diagnosis and remediation
self-reports only when an exact structured outcome is available. The v1 record
schema pins each pair to scenario, dataset, candidate configuration, and A05
pair ID.

Build reliability evidence with:

```bash
python benchmarks/confidence_eval.py reports/sre-bench-confidence.jsonl \
  --task remediation \
  --config-fingerprint "$BENCH_CONFIG_FINGERPRINT" \
  --report-output reports/remediation-reliability.json \
  --artifact-output config/remediation-confidence-v1.json \
  --artifact-version remediation-v1
```

The report includes reliability bins, Brier score, log loss, ECE, MCE, and
optional drift against a reference JSONL. Artifact bins use equal-frequency
grouping, Laplace smoothing, and adjacent-violator pooling so calibrated
probability is monotonic.

An autonomy threshold is emitted only when its observed outcomes have the
configured minimum support and Wilson lower confidence bound. A valid
remediation artifact can be configured with
`REMEDIATION_CONFIDENCE_CALIBRATION_PATH`. Runtime also requires
`SENTINEL_CONFIG_FINGERPRINT` to exactly match the artifact's A01 configuration.
Missing, invalid, mismatched, diagnosis-only, or under-supported artifacts fail
closed: mutations require approval. Notify-only escalation remains non-mutating.

No calibration artifact or reference dataset is committed here. Both must come
from real, content-addressed benchmark outcomes; generated or synthetic evidence
must not be used to enable production autonomy.
