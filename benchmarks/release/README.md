# Release evaluation gate

A09 combines the authoritative A05 statistical report, A07 adversarial report,
and A08 trace artifacts into one content-addressed promotion decision. The v1
policy fixes the minimum paired sample, recovery and quality non-inferiority
margins, maximum cost and latency regression ratios, zero-tolerance safety
limits, and rollout constraints.

## CI contract matrix

`v1/ci-matrix.json` pins the policy and every bundle by SHA-256. Its frozen
fixtures prove that a safe candidate promotes while deliberately regressive
prompt, model, and tool configurations block. These fixtures test the gate;
they are not production release evidence.

```bash
uv run python benchmarks/release_gate.py matrix \
  --matrix benchmarks/release/v1/ci-matrix.json \
  --output reports/release-matrix.json
```

CI preserves the matrix report as an artifact. Changing a pinned fixture or
policy without updating its descriptor fails closed.

## Candidate evidence

Changes to protected prompt, model-routing, or tool-contract paths require
`benchmarks/release/candidate/bundle.json` and its referenced raw artifacts.
The bundle must contain:

- distinct baseline and candidate configuration fingerprints;
- the paired statistical report and raw trial artifact;
- the zero-tolerance adversarial report and raw observations;
- complete root-trace evidence;
- an ordered zero-traffic shadow stage followed by a bounded canary;
- automatic rollback to the evaluated baseline on any safety failure,
  incomplete trace, or policy-exceeding quality, recovery, latency, or cost
  regression.

Generate the protected-source digest after the candidate prompt/model/tool
files are final, then record it as `candidate.source_digest`:

```bash
uv run python benchmarks/release_gate.py digest \
  --policy benchmarks/release/v1/policy.json \
  --repo-root . \
  --output reports/release-source-digest.json
```

Evaluate the bundle directly before opening or updating the PR:

```bash
uv run python benchmarks/release_gate.py evaluate \
  --policy benchmarks/release/v1/policy.json \
  --bundle benchmarks/release/candidate/bundle.json \
  --output reports/release-decision.json
```

The promotion report pins the policy, bundle, paired trials, adversarial
observations, and root traces. Missing evidence, stale source digests, changed
rollout triggers, incomplete grades/traces, or threshold regressions block.
