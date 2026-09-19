# Ablation — does each component earn its complexity?

Sentinel's pitch against read-only diagnostic tools rests on three claims:

1. a supervisor-routed split of specialists diagnoses better than one agent;
2. a reflector between investigation and planning makes conclusions more
   reliable;
3. learned memory improves outcomes on incidents the system has seen before.

Each is an empirical claim and none is self-evident. This directory is how
they get measured. Nothing here is part of a production run.

## The arms

`sre_agent/ablation.py` defines four measurement configurations, selected with
`SENTINEL_ABLATION_ARM`:

| Arm | Removed | What it isolates |
| --- | --- | --- |
| `full` | nothing | The control. Today's shipped architecture. |
| `single_agent` | the specialist split | One ReAct loop holding the union of every specialist's read-only tools. |
| `no_reflector` | the ORIENT stage | The planner reads the specialists' findings directly, unsynthesised. |
| `no_memory` | incident recall and verified skills | Both directions: no retrieval, no writes. |

Each arm removes exactly one component. Two removals at once produce a number
nobody can attribute.

Three properties are worth stating explicitly, because each is a way an
ablation study usually goes wrong:

* **Unset is not an arm.** With `SENTINEL_ABLATION_ARM` absent the process is
  production: full architecture, learning live, and — asserted in
  `tests/test_ablation.py` — a graph identical to the one that ships. Setting
  it to `full` is a *different* state: the control arm of an experiment.
* **A typo fails closed.** `SENTINEL_ABLATION_ARM=no_reflectr` raises
  `AblationError` at startup. Degrading to the control would have produced a
  comparison of the full system against itself, reported truthfully and
  uselessly as "no difference".
* **Learned-memory writes are frozen in every arm, the control included.**
  Arms run sequentially against one cluster. If the control wrote what it
  learned, the arm that ran next would inherit a corpus the control never
  had, and the comparison would measure run order.

### What each arm deliberately keeps

`single_agent` keeps the reflector, the bounded re-investigation loop, and
`aggregate` — the report writer. Removing the report writer would change what
a "report" is and make quality scores incomparable. It drops `infra_prescan`,
which is a second agent, and whose tools the single investigator already holds.

`no_reflector` keeps DECIDE and ACT untouched. Because the reflector was the
only path by which specialist findings reached the planner, the planner now
receives those findings raw, wrapped with the same untrusted-content boundary
the reflector used. Skipping ORIENT removes reasoning, never the injection
boundary — an arm that also dropped the boundary would be measuring a
strawman.

`no_memory` removes incident recall and verified skills only. Static runbooks
stay: they are authored knowledge, not learned, and removing them too would
make the difference unattributable.

## Before spending the budget: can `no_memory` observe anything?

Learned-memory writes are frozen for **every** arm during an experiment, so
whatever `full` retrieves has to pre-exist the run. If the corpus matches
nothing on the split, `full` and `no_memory` make the same lookups, get the
same nothing, and behave identically. That reports `NOT_DEMONSTRATED` — which
is also how the considered finding "learned memory does not earn its
complexity" reports. The artifact cannot tell the two apart, so measure it
first:

**Run it inside the agent container.** `SemanticSkillStore` degrades to
keyword-only recall when `qdrant-client` is missing, Qdrant is unreachable, or
the embedding model will not load — each time with a log line and no error.
The agent image ships without `qdrant-client`; an operator host with the dev
extras does not. On the same corpus and the same six dev scenarios that
difference gave **6/6 coverage on the host and 3/6 in the container**.
`--expect-retrieval-path` turns that into an error instead of a plausible
number.

`benchmarks/` is not in the agent image, so copy it in for the run and take it
out again — this is a measurement, not a deployment:

```bash
docker cp benchmarks sre-agent-api:/app/benchmarks
docker exec sre-agent-api python /app/benchmarks/ablation_coverage.py \
  --split dev --organization-id "$ORG" --cluster-id "$CLUSTER" \
  --qdrant-url http://qdrant:6333 \
  --expect-retrieval-path keyword_only \
  --output /tmp/ablation-memory-coverage.json
docker cp sre-agent-api:/tmp/ablation-memory-coverage.json reports/
docker exec sre-agent-api rm -rf /app/benchmarks
```

The preflight is read-only. `SemanticSkillStore.__init__` backfills the Qdrant
skill index, which would have the preflight reporting on a corpus it had just
written; `open_store_read_only()` suppresses that for the duration of
construction and fails loudly if the method it suppresses is ever renamed.
Verified on the host path with the real five-skill corpus: semantic recall
active, zero Qdrant writes.

Pass the artifact to the comparison as `--memory-coverage`. Without it, or
with a blind or partial one, `no_memory` carries an `insufficient_evidence`
entry and cannot report a clean null result.

## Running an experiment

Every arm is an ordinary `sre_bench.py` run. The arms must share the
experiment and the pairing, and must differ in identity and fingerprint:

| Variable | Across arms |
| --- | --- |
| `BENCH_EXPERIMENT_ID` | **same** |
| `BENCH_PAIR_SEED` | **same** |
| `BENCH_DATASET_VERSION` / `BENCH_DATASET_SPLIT` | **same** |
| `BENCH_TRIAL_RESULTS_PATH` | **same** (all arms append to one artifact) |
| `BENCH_CANDIDATE_ID` | **distinct** |
| `BENCH_CONFIG_FINGERPRINT` | **distinct** |

```bash
export BENCH_EXPERIMENT_ID=ablation-2026-09
export BENCH_PAIR_SEED=ablation-seed-1
export BENCH_TRIAL_RESULTS_PATH=reports/ablation-trials.jsonl

for arm in full single_agent no_reflector no_memory; do
  SENTINEL_ABLATION_ARM="$arm" \
  BENCH_CANDIDATE_ID="$arm" \
  BENCH_CONFIG_FINGERPRINT="$(...)" \
    uv run python benchmarks/sre_bench.py
done
```

Run the arms back to back against the same cluster, same models, same
revision. A `git pull` between two arms invalidates the experiment, and the
harness will say so rather than average over it.

### Capturing each arm's manifest

`BENCH_CONFIG_FINGERPRINT` is operator-declared: `sre_bench.py` writes whatever
string it is handed onto every trial. Nothing in the trial artifact proves the
run actually used the configuration that string claims, so two runs of the
*same* arm can be handed in under different fingerprints and compared happily.

The manifest closes that. Pull it for any job in each arm's run:

```bash
curl -sH "Authorization: Bearer $TOKEN" \
  "$BENCH_BASE_URL/api/v1/clusters/$BENCH_CLUSTER_ID/jobs/$JOB_ID/manifest" \
  > reports/manifest-$arm.json
```

The endpoint's row wrapper is fine as-is; the harness unwraps `manifest`. The
arm in force is recorded in the manifest's `runtime` section, which is one of
the four sections the A01 configuration fingerprint hashes — so the arm is
*part of* the fingerprint, and a manifest cannot claim an arm it did not run.

## Comparing

```bash
uv run python benchmarks/ablation_eval.py reports/ablation-trials.jsonl \
  --full-id full --full-manifest reports/manifest-full.json \
  --arm single_agent=single_agent=reports/manifest-single_agent.json \
  --arm no_reflector=no_reflector=reports/manifest-no_reflector.json \
  --arm no_memory=no_memory=reports/manifest-no_memory.json \
  --memory-coverage reports/ablation-memory-coverage.json \
  --output reports/ablation.json
```

Before computing anything, the harness requires of every arm — control
included — that the manifest name the arm being claimed, hash to the
fingerprint recorded on that arm's trials, carry `ablation_experiment: true`,
carry `learned_memory_writes: false`, and share a `code_sha` with the control.
Any of these failing is an error, not a warning: a mislabelled arm produces no
report rather than a plausible number.

### Reading the verdicts

The arm is the *baseline* and the full stack is the *candidate*, so every
paired delta reads "full minus arm". Per arm:

| Verdict | Meaning |
| --- | --- |
| `DEMONSTRATED` | The lower bound of the paired quality delta is above zero. The component earns its complexity. |
| `NOT_DEMONSTRATED` | The interval contains zero. **Not a pass.** |
| `REFUTED` | The upper bound is below zero — removing the component improved outcomes. Exit code 2. |

This is deliberately a different question from `statistical_eval.py`'s. That
one is a release gate asking "is the candidate non-inferior?", a bar a
component that does nothing clears easily. The full `compare_candidates`
report is embedded per arm under `paired_report`, including its
`release_decision`, but that decision is not the ablation verdict.

`NOT_DEMONSTRATED` carries an `insufficient_evidence` list. If it is
non-empty — too few pairs, intervals wider than the CI ceiling, no cost
evidence — the run could not have detected an effect if one existed, and the
result is ignorance rather than a null. Report it that way.

Each arm also reports `cost_of_complexity`: the paired cost and latency the
full stack spends over that arm. A component that is `NOT_DEMONSTRATED` *and*
costs significantly more is called out by name in `notes`. That combination is
the finding this harness exists to be able to state.
