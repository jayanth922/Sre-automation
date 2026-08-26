# Paired statistical evaluation

`statistical_eval.py` compares two configurations over the same scenario/trial
pairs. Raw JSONL trials conform to `v1/trial.schema.json`; each record pins its
dataset, scenario version, candidate configuration fingerprint, independent
oracle result, structured-grader status, safety result, latency, and evidence
artifact paths.

Use the same experiment and pair seed for both candidates. The configuration
fingerprint must be the canonical SHA-256 of A01's `provenance`, `models`,
`tools`, and `runtime` sections; `configuration_fingerprint()` deliberately
excludes per-trial input and trace fields:

```bash
# Baseline run
BENCH_EXPERIMENT_ID=exp-2026-08-26 \
BENCH_CANDIDATE_ID=baseline \
BENCH_CONFIG_FINGERPRINT=<64-char-manifest-sha> \
BENCH_PAIR_SEED=seed-41 \
BENCH_RUNS_PER_SCENARIO=20 \
uv run python benchmarks/sre_bench.py

# Candidate run: same experiment/seed, different candidate/fingerprint
BENCH_EXPERIMENT_ID=exp-2026-08-26 \
BENCH_CANDIDATE_ID=candidate \
BENCH_CONFIG_FINGERPRINT=<different-64-char-manifest-sha> \
BENCH_PAIR_SEED=seed-41 \
BENCH_RUNS_PER_SCENARIO=20 \
uv run python benchmarks/sre_bench.py

python benchmarks/statistical_eval.py reports/sre-bench-trials.jsonl \
  --baseline baseline --candidate candidate \
  --output reports/sre-bench-comparison.json
```

The report preserves raw artifact hashes, sample counts, scenario slices,
failure categories, Wilson intervals, paired bootstrap intervals, effect sizes,
pass@k/pass^k, MTTR/latency/cost distributions, and policy parameters.
Promotion fails when pairs are missing, uncertainty is too wide, recovery is
not non-inferior, a high-risk slice regresses, safety fails, or A04 structured
grades are incomplete. Binary promotion gates use conservative differences of
Wilson intervals so all-tie samples do not produce a falsely zero-width
bootstrap interval.

Cost remains `null` until A08 provides trace-complete cost accounting; coverage
is reported rather than replacing missing cost with zero.
