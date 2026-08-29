# Scenario datasets

`v1/` is a content-addressed benchmark dataset. `dataset.json` pins the SHA-256
of every split, and the loader rejects any unrecorded edit.

Each scenario declares:

- scenario and dataset versions;
- taxonomy and risk class;
- provenance;
- alert input and fault-adapter contract;
- expected evidence;
- allowed and forbidden action types;
- expected severity bands; and
- an independent recovery probe.

## Splits

- `train`: visible examples for development.
- `dev`: the default live benchmark split.
- `holdout`: frozen evaluation cases. The loader requires explicit local access
  and refuses to expose this split when `CI` is set.

Run another split with `BENCH_DATASET_SPLIT=train`. Local holdout evaluation
also requires `BENCH_ALLOW_HOLDOUT=true`. Do not use holdout results to tune
prompts, models, tools, thresholds, or scenario logic.

Never edit a frozen holdout file in place. Create a new dataset version,
recompute all split digests, and retain the old version for reproducibility.

## Current boundary

Version 1 migrates the four evidence-backed reference-client scenarios that
already existed in the live benchmark. The `meridian_admin_config_v1` adapter
executes their typed `/admin/config` inject/cleanup contracts, verifies applied
values, and restores the healthy baseline. Clean, noisy, multi-fault, capacity,
security, partial-outage, and no-action cases remain future dataset additions
and must be backed by runnable fixtures rather than invented labels.
