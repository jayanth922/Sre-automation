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

When labels exist, measure and gate agreement with:

```bash
python benchmarks/grader_calibration.py labels.jsonl \
  --minimum-cases 20 --minimum-kappa 0.6
```

The command fails when cases are under-labeled, labels lack class variation,
or either semantic criterion misses the configured Cohen's kappa threshold.
