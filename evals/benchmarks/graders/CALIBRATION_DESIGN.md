# Calibrated judge for `causal_chain` and `evidence_support`

Status: design, free work only. No judge exists; both criteria stay
`REQUIRES_CALIBRATION` until the gate in section 5 is met.

## 1. Why the criteria never resolve

`structured_grading._semantic_criterion` checks shape only. An empty or
malformed list is `INSUFFICIENT_EVIDENCE`; anything else is
`REQUIRES_CALIBRATION`. `v1/rubric.json` declares both `calibrated_judge`, both
are required, and `overall_status` is `PASS` only when every criterion is
`PASS`/`NOT_APPLICABLE` -- so no run can be better than `INCOMPLETE`.

## 2. Where the material is

- `reports/run-trace/*.jsonl` (in `platform_reports_data`) is span timing,
  tokens and cost. No span carries a payload; nothing there can be labelled.
  It is only a join key (`root_trace_id`).
- What the agent claimed: grader records (`reports/**/*grades*.jsonl`),
  `summary.payload.benchmark_evaluation.{causal_chain,evidence}`.
- What the tools returned: `evidence_artifacts` in PostgreSQL, one
  `specialist_tool_trace` per finding, addressed by the canonical-JSON digest
  the finding's `evidence_artifact_ref.sha256` carries.

Build the set with `benchmarks.transcript_store` and
`benchmarks.calibration_cases --transcript-dir` (commands in `README.md`).
2026-10-01: 25 reviewable cases from 18 files (20 skipped: 17 without a
structured block, 3 on an older rubric digest), 549 transcripts stored, 114
attached, none missing. Six cases are the Runs 3-9 negative control.
Nine cases have an empty `evidence` list; the grader already returns
`INSUFFICIENT_EVIDENCE` for them, so they count for `causal_chain` only.

## 3. Rubric

The judge, like the labellers, sees only the review case: the agent's chain,
evidence, findings and the tool transcripts. No scenario id, fault, expected
evidence, oracle result or other criterion's grade. Whether the agent found the
*right* fault is `diagnosis`'s job; these two criteria ask whether the
reasoning it gave is honest and holds together.

**causal_chain** -- each link, then the chain:

| Check | Fails when |
|---|---|
| grounded | the cause or effect is not observed in any finding or transcript, or contradicts one |
| mechanism | reversed, correlation presented as cause, or the effect restates the cause |
| connected | a link's effect does not lead to the next link's cause |

Chain `PASS`: every link grounded and mechanistic, and the chain runs from a
stated origin to the alerted symptom (or, for a no-action conclusion, to why no
action is warranted). `FAIL`: any link contradicted by a transcript, reversed,
or missing between origin and symptom.

**evidence_support** -- each entry, then the list:

| Check | Fails when |
|---|---|
| locatable | the `reference` matches no tool call in the transcripts |
| faithful | the claimed values, counts or absences differ from the tool return |
| relevant | the entry supports no link in the chain |

List `PASS`: no unlocatable or unfaithful entry, and every chain link has at
least one supporting entry. `locatable` is mechanical and should become a
deterministic pre-check before any model sees the case.

## 4. Hand-labelling

1. Labellers receive only `review.jsonl`; `private-map.jsonl` and the blind
   key stay with the evaluation owner (mode 0600).
2. Per case, label both criteria `PASS`/`FAIL` with a rationale in the
   `v1/calibration-label.schema.json` contract that `grader_calibration`
   enforces; record the per-link and per-entry checks in the rationale.
3. `grader_calibration` requires the same two independent labellers on every
   case. With one person, a second blind pass at least 48h later under a
   distinct `labeler_id` measures intra-rater agreement -- report it as that,
   never as inter-rater.
4. Disagreements are adjudicated before the set is frozen; freeze by recording
   `manifest.json` and the label file's sha256.
5. The real corpus will be mostly `PASS`. Add perturbed copies of real cases,
   labelled `FAIL` by construction -- swapped cause/effect, dropped link, one
   number changed against its transcript, a reference no tool call made,
   evidence lifted from another case -- and report them as their own stratum.

## 5. Agreement and the gate

- Human-human: Cohen's kappa per criterion (as `grader_calibration` computes),
  plus Gwet's AC1, which stays meaningful when one class dominates.
- Judge-human (against adjudicated labels), per criterion on a held-out split
  grouped by scenario: kappa and AC1 with bootstrap CIs resampled by case; the
  false-`PASS` rate (judge `PASS`, human `FAIL`) with its Wilson upper bound;
  detection rate on the perturbed stratum; abstention rate when three judge
  samples disagree (abstain means `INSUFFICIENT_EVIDENCE`).
- The judge must not be the agent's model (the agent runs `claude-sonnet-5`).
- Honest limit: 25 cases cannot bound the false-`PASS` rate tightly -- 0 of 18
  held out still allows about 18%. The free set is enough to build and debug
  the harness and to measure item-level agreement; a release-grade bound needs
  the labelled output of the paid campaign as well.
- A judge becomes authoritative only through a `judge_calibration.json` that
  pins judge model, prompt sha, rubric sha, label-set sha and the measured
  metrics; `_semantic_criterion` keeps returning `REQUIRES_CALIBRATION` unless
  those hashes match what is loaded.

## 6. Next steps

1. Label the 25 cases (two blind passes), adjudicate, freeze. Free.
2. Perturbation generator and the deterministic `locatable` pre-check. Free.
3. Judge harness and metrics, verified against a stub judge. Free.
4. Price one judge pass over the frozen set and ask before spending.
