# Release evaluation gate

A09 combines the authoritative A05 statistical report, A07 adversarial report,
and A08 trace artifacts into one content-addressed promotion decision. The v1
policy fixes the minimum paired sample, recovery and quality non-inferiority
margins, maximum cost and latency regression ratios, zero-tolerance safety
limits, and rollout constraints.

The gate recomputes both reports from the raw artifacts. A bundle's
`statistical_report` and `adversarial_report` are treated as claims about its
records, and a claim that disagrees with the records it summarises is itself a
reason to block. See `evals/benchmarks/release_evidence.py`.

## CI contract matrix

`v1/ci-matrix.json` pins the policy and every bundle by SHA-256. Its fixtures
prove that a safe candidate promotes while deliberately regressive prompt,
model, and tool configurations block. These fixtures test the gate; they are
not production release evidence.

Each fixture's regression lives in its records, and the bundle's reports are
whatever `statistical_eval.compare_candidates` and `adversarial_eval.evaluate`
return for them. Nothing here is hand-written, so nothing here can claim a
verdict its evidence does not support. Regenerate after any change:

```bash
uv run python -m benchmarks.make_release_fixtures
uv run python -m benchmarks.make_release_fixtures --check   # what CI asserts
```

```bash
uv run python evals/benchmarks/release_gate.py matrix \
  --matrix evals/benchmarks/release/v1/ci-matrix.json \
  --output reports/release-matrix.json
```

CI preserves the matrix report as an artifact. Changing a pinned fixture or
policy without updating its descriptor fails closed.

## Candidate evidence

There is no candidate bundle in the repository. There was one, and every line
of its evidence read `{"fixture": "paired-trials-v1", "record": 1}`; it was
deleted rather than regenerated, because a synthetic bundle asserting measured
results about this repository's real prompts, models, and tools is worse than
no bundle at all. A protected-path PR therefore fails closed on "protected
change lacks release evidence" until a real evaluation run produces one, which
is the honest answer.

Changes to protected prompt, model-routing, or tool-contract paths require
`evals/benchmarks/release/candidate/bundle.json` and its referenced raw artifacts,
produced by an actual paired evaluation against the candidate configuration.
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
uv run python evals/benchmarks/release_gate.py digest \
  --policy evals/benchmarks/release/v1/policy.json \
  --repo-root . \
  --output reports/release-source-digest.json
```

Evaluate the bundle directly before opening or updating the PR:

```bash
uv run python evals/benchmarks/release_gate.py evaluate \
  --policy evals/benchmarks/release/v1/policy.json \
  --bundle evals/benchmarks/release/candidate/bundle.json \
  --output reports/release-decision.json
```

The promotion report pins the policy, bundle, paired trials, adversarial
observations, and root traces. Missing evidence, stale source digests, changed
rollout triggers, incomplete grades/traces, or threshold regressions block.
