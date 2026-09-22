# Structured benchmark graders

`v1/rubric.json` pins the criteria and grading method used for A04. Deterministic
criteria consume typed fields only; free-text keyword matches are not accepted.
Causal-chain and evidence-support judgments remain
`REQUIRES_CALIBRATION` until a blinded, human-labeled calibration set exists.

`v1/calibration-label.schema.json` defines that label contract. No calibration
labels are checked into the repository yet, so the evaluator must not report
judge agreement or semantic-grade accuracy. Future labels must use opaque case
IDs, the same two independent labelers for every case, and adjudication for
disagreement before any model judge can become release-authoritative.

Build the blinded review set from raw grader evidence without exposing scenario
IDs or ground truth to either labeler:

```bash
umask 077
openssl rand 32 > reports/calibration-blind.key
uv run python -m benchmarks.calibration_cases reports/sre-bench-grades.jsonl \
  --blind-key-file reports/calibration-blind.key \
  --review-output reports/calibration-review.jsonl \
  --private-mapping-output reports/calibration-private-map.jsonl \
  --manifest-output reports/calibration-manifest.json \
  --limit 20
```

The HMAC key makes selection reproducible without putting scenario identity in
the review artifact. The private map and key stay with the evaluation owner;
labelers receive only `calibration-review.jsonl`. The script rejects duplicate
outputs, digest mismatches, missing structured evaluations, and records pinned
to another rubric version or digest. Source-output hashes remain only in the
private map, so a labeler cannot join an opaque case back to the raw corpus. It
never calls a model or Langfuse, so creating the review set has no API cost.

When labels exist, measure and gate agreement with:

```bash
python benchmarks/grader_calibration.py labels.jsonl \
  --minimum-cases 20 --minimum-kappa 0.6
```

The command fails when cases are under-labeled, labels lack class variation,
or either semantic criterion misses the configured Cohen's kappa threshold.
