#!/usr/bin/env python3
"""Ablation harness: does each architectural component earn its complexity?

Sentinel's comparison against read-only tools rests on three claims — that a
supervisor-routed specialist split diagnoses better than one agent, that the
reflector makes conclusions more reliable, and that learned memory improves
outcomes on recurring incidents. Each claim is an empirical question, and this
module is what answers it. An architecture diagram is not evidence.

The measurement is a paired comparison against `sre_agent.ablation` arms: run
the same scenarios, same model, same dataset, once per arm, then ask whether
the full stack beats the arm by more than the noise. `statistical_eval` already
does the paired statistics; what this adds is the question and the provenance.

The question is different from the release gate's. `statistical_eval` asks
"is the candidate non-inferior?" — a bar a component that does nothing clears
easily. Here the full stack is the candidate and the arm is the baseline, and
a component earns its place only when the lower bound of the paired delta is
strictly above zero. "We could not measure a difference" is reported as
exactly that, never as a pass.

The provenance closes a hole the release gate leaves open. `BENCH_CONFIG_
FINGERPRINT` is operator-declared, so two runs of the *same* arm can be handed
in under different fingerprints and compared happily — a comparison of the
full system against itself, reported as a null result. Every arm therefore has
to present the run manifest it actually ran under: this harness recomputes
`configuration_fingerprint()` from it, requires the answer to match the
fingerprint on that arm's trials, and requires the manifest's `runtime` section
to name the arm being claimed. A mislabelled arm fails to load rather than
producing a plausible number.

Usage:

    python benchmarks/ablation_eval.py reports/sre-bench-trials.jsonl \\
        --full-id full-v1 --full-manifest reports/manifest-full.json \\
        --arm single_agent=single-v1=reports/manifest-single.json \\
        --arm no_reflector=noreflect-v1=reports/manifest-noreflect.json \\
        --arm no_memory=nomemory-v1=reports/manifest-nomemory.json \\
        --output reports/ablation.json

Manifests come from `GET /api/v1/clusters/{cluster_id}/jobs/{job_id}/manifest`
for any job in that arm's run; the endpoint's row wrapper is accepted as-is.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.statistical_eval import (  # noqa: E402
    ArtifactEvidence,
    StatisticalEvalError,
    TrialRecord,
    compare_candidates,
    configuration_fingerprint,
    load_trials,
)
from sre_agent.ablation import ARMS, FULL_ARM  # noqa: E402

SCHEMA_VERSION = 2

#: The lower bound of the paired delta must clear this before a component is
#: credited. Zero, not a tolerance: an effect whose interval includes "no
#: effect" has not been demonstrated, however suggestive the point estimate.
SUPERIORITY_FLOOR = 0.0

DEMONSTRATED = "DEMONSTRATED"
NOT_DEMONSTRATED = "NOT_DEMONSTRATED"
REFUTED = "REFUTED"


class AblationEvalError(ValueError):
    """The ablation evidence cannot support a claim either way."""


def load_manifest(path: Path) -> dict[str, Any]:
    """Read a run manifest, accepting the API's row wrapper around it."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AblationEvalError(f"cannot read run manifest {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AblationEvalError(
            f"run manifest {path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise AblationEvalError(f"run manifest {path} must be an object")
    # `GET .../jobs/{job_id}/manifest` returns the stored row; the manifest
    # itself is one field of it.
    inner = payload.get("manifest")
    manifest = inner if isinstance(inner, dict) else payload
    if inner is not None and payload.get("comparable") is False:
        # The runtime already decided this run cannot be compared to anything.
        # Re-deciding that here from the same manifest would be theatre.
        reasons = payload.get("non_comparable_reasons") or ["unspecified"]
        raise AblationEvalError(
            f"run manifest {path} is marked non-comparable by the runtime: "
            f"{'; '.join(str(reason) for reason in reasons)}"
        )
    if not isinstance(manifest.get("runtime"), dict):
        raise AblationEvalError(
            f"run manifest {path} has no runtime section; it cannot attest an arm"
        )
    return manifest


def load_coverage(path: Path) -> dict[str, Any]:
    """Read a corpus-coverage artifact from benchmarks/ablation_coverage.py.

    This is not a run manifest and carries no `runtime` section; routing it
    through `load_manifest` made `--memory-coverage` unusable from the CLI.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AblationEvalError(
            f"cannot read coverage artifact {path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise AblationEvalError(
            f"coverage artifact {path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise AblationEvalError(f"coverage artifact {path} must be an object")
    return payload


def attest_arm(
    manifest: dict[str, Any],
    *,
    expected_arm: str,
    fingerprint: str,
    source: str,
) -> dict[str, Any]:
    """Bind a declared arm to the manifest the run actually used.

    Four things have to hold, and each corresponds to a way the comparison
    could otherwise report a confident number about nothing:

    * the manifest names the arm being claimed — otherwise the label is prose;
    * the manifest hashes to the fingerprint on that arm's trials — otherwise
      the trials came from some other configuration;
    * the experiment flag is set, on the control too — a control run with
      learning live is not a control;
    * learned-memory writes are frozen — an arm that wrote during the
      experiment changed the corpus the next arm inherited, so the comparison
      measures run order.
    """
    runtime = manifest["runtime"]
    declared = runtime.get("ablation_arm")
    if declared != expected_arm:
        raise AblationEvalError(
            f"{source}: manifest declares ablation_arm={declared!r}, "
            f"but this comparison labels it {expected_arm!r}"
        )
    try:
        recomputed = configuration_fingerprint(manifest)
    except StatisticalEvalError as exc:
        raise AblationEvalError(f"{source}: {exc}") from exc
    if recomputed != fingerprint:
        raise AblationEvalError(
            f"{source}: manifest fingerprint {recomputed} does not match the "
            f"fingerprint {fingerprint} recorded on the arm's trials; the "
            "trials were produced by a different configuration"
        )
    if runtime.get("ablation_experiment") is not True:
        raise AblationEvalError(
            f"{source}: manifest was not recorded under an ablation experiment "
            "(SENTINEL_ABLATION_ARM unset); learning was live and the run is "
            "not a valid arm, control included"
        )
    if runtime.get("learned_memory_writes") is not False:
        raise AblationEvalError(
            f"{source}: learned-memory writes were not frozen; a later arm "
            "inherited a corpus this one created, so the comparison would "
            "measure run order rather than architecture"
        )
    return {
        "arm": expected_arm,
        "config_fingerprint": recomputed,
        "removed_components": list(ARMS[expected_arm].removed),
        "code_sha": (manifest.get("provenance") or {}).get("code_sha"),
        "graph_sha256": (
            ((manifest.get("provenance") or {}).get("graph") or {}).get("sha256")
        ),
    }


def _single_fingerprint(trials: tuple[TrialRecord, ...], candidate_id: str) -> str:
    values = {
        trial.config_fingerprint
        for trial in trials
        if trial.candidate_id == candidate_id
    }
    if not values:
        raise AblationEvalError(f"no trials recorded for candidate {candidate_id!r}")
    if len(values) != 1:
        raise AblationEvalError(
            f"candidate {candidate_id!r} has {len(values)} configuration "
            "fingerprints; one arm is one configuration"
        )
    return values.pop()


def _verdict(interval: list[float]) -> str:
    """Classify a paired delta (full minus arm) by its confidence interval."""
    low, high = float(interval[0]), float(interval[1])
    if low > SUPERIORITY_FLOOR:
        return DEMONSTRATED
    if high < SUPERIORITY_FLOOR:
        return REFUTED
    return NOT_DEMONSTRATED


def _coverage_gaps(arm: str, coverage: Optional[dict[str, Any]]) -> list[str]:
    """Reasons the `no_memory` arm could not have observed what it removes.

    Learned-memory writes are frozen for every arm during an experiment, so
    whatever the control retrieves must pre-exist the run. A corpus that
    matches nothing makes `full` and `no_memory` behaviourally identical, and
    the resulting NOT_DEMONSTRATED is an artifact of the empty corpus rather
    than a finding about learned memory. Underpowering is measured from the
    trials; this is not visible there at all, so it has to be supplied.
    """
    if arm != "no_memory":
        return []
    if coverage is None:
        return [
            "no corpus-coverage evidence for the learned-memory arm; run "
            "benchmarks/ablation_coverage.py and pass --memory-coverage to "
            "show the control could retrieve anything to begin with"
        ]
    observable = coverage.get("observable_pairs")
    total = coverage.get("scenario_count")
    gaps: list[str] = []
    if not observable:
        gaps.append(
            f"none of {total} scenarios could retrieve learned memory, so the "
            "arm removed nothing the control actually had"
        )
    elif total and observable < total:
        gaps.append(
            f"only {observable} of {total} scenarios could retrieve learned "
            "memory; the rest pull the paired delta toward zero regardless of "
            "whether the component works"
        )
    if not coverage.get("retrieval_path"):
        # The same corpus reads as 6/6 under the agent's interpreter and 3/6
        # under the container's bare `python`, which lacks the project's
        # dependencies and silently falls back to keyword matching. An
        # artifact that does not name its retrieval path cannot be attributed
        # to either stack.
        gaps.append(
            "the corpus-coverage artifact does not record which retrieval path "
            "produced it, so it cannot be shown to describe the stack the agent "
            "runs; re-run the preflight in the agent's environment"
        )
    if coverage.get("incident_memory_points") == 0:
        gaps.append(
            "incident recall was inert for every arm (no tenant-scoped points "
            "in the collection); only the verified-skill half was measured"
        )
    return gaps


def _evidence_gaps(
    report: dict[str, Any],
    *,
    minimum_pairs: int,
    maximum_ci_width: float,
    arm: str,
    coverage: Optional[dict[str, Any]],
) -> list[str]:
    """Reasons a null result means "cannot tell" rather than "no effect".

    Without these, an underpowered experiment reports NOT_DEMONSTRATED for
    every component and reads as a considered finding. It is the opposite:
    an experiment that could not have detected an effect if one existed.
    """
    gaps: list[str] = _coverage_gaps(arm, coverage)
    paired = report["paired"]
    if paired["pair_count"] < minimum_pairs:
        gaps.append(
            f"only {paired['pair_count']} paired trials; "
            f"{minimum_pairs} required to call a null result"
        )
    if paired["cost_usd"] is None:
        gaps.append("no paired cost evidence; the complexity's price is unmeasured")
    for key in ("diagnosis",):
        low, high = paired[key]["conservative_wilson_95"]
        if high - low > maximum_ci_width:
            gaps.append(
                f"paired {key} interval is {high - low:.3f} wide, over the "
                f"{maximum_ci_width:.3f} ceiling; too coarse to detect an effect"
            )
    return gaps


def evaluate_arm(
    trials: tuple[TrialRecord, ...],
    artifact: ArtifactEvidence,
    *,
    arm: str,
    arm_candidate_id: str,
    arm_manifest: dict[str, Any],
    full_candidate_id: str,
    full_manifest: dict[str, Any],
    minimum_pairs: int,
    maximum_ci_width: float,
    pass_k: int,
    bootstrap_seed: int,
    memory_coverage: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Compare the full stack against one arm and say what it bought."""
    if arm not in ARMS:
        raise AblationEvalError(
            f"unknown ablation arm {arm!r}; expected one of: {', '.join(sorted(ARMS))}"
        )
    if arm == FULL_ARM:
        raise AblationEvalError("the control arm cannot be compared against itself")

    arm_fingerprint = _single_fingerprint(trials, arm_candidate_id)
    full_fingerprint = _single_fingerprint(trials, full_candidate_id)
    arm_attestation = attest_arm(
        arm_manifest,
        expected_arm=arm,
        fingerprint=arm_fingerprint,
        source=f"arm {arm}",
    )
    full_attestation = attest_arm(
        full_manifest,
        expected_arm=FULL_ARM,
        fingerprint=full_fingerprint,
        source="control arm",
    )
    if arm_attestation["code_sha"] != full_attestation["code_sha"]:
        raise AblationEvalError(
            f"arm {arm} ran at code_sha {arm_attestation['code_sha']} but the "
            f"control ran at {full_attestation['code_sha']}; the arms differ "
            "by more than the component under measurement"
        )

    # The full stack is the candidate and the arm is the baseline, so every
    # paired delta below reads "full minus arm" — positive means the removed
    # component helped.
    try:
        report = compare_candidates(
            trials,
            baseline_id=arm_candidate_id,
            candidate_id=full_candidate_id,
            artifact=artifact,
            minimum_pairs=minimum_pairs,
            maximum_ci_width=maximum_ci_width,
            k=pass_k,
            bootstrap_seed=bootstrap_seed,
        )
    except StatisticalEvalError as exc:
        raise AblationEvalError(f"arm {arm}: {exc}") from exc

    paired = report["paired"]
    diagnosis_verdict = _verdict(paired["diagnosis"]["conservative_wilson_95"])
    quality_verdict = _verdict(paired["quality"]["conservative_wilson_95"])
    recovery_verdict = _verdict(paired["recovery"]["conservative_wilson_95"])

    # Cost runs the other way: a positive delta means the full stack spends
    # more. Both deltas are needed before the trade can be judged.
    cost = paired["cost_usd"]
    latency = paired["latency_seconds"]
    cost_delta = None if cost is None else cost["mean_delta"]
    pays_more = bool(cost is not None and cost["bootstrap_95"][0] > 0)
    slower = bool(latency["bootstrap_95"][0] > 0)

    # Approval-gated benchmark runs intentionally cannot recover. The
    # component question is therefore answered by the exact structured
    # diagnosis match, while recovery and end-to-end quality remain visible
    # as separate production outcomes and keep their release-gate authority.
    verdict = diagnosis_verdict
    # Power only needs auditing where nothing was detected. An interval that
    # already excludes zero was, by demonstration, sharp enough.
    gaps = (
        []
        if verdict == DEMONSTRATED
        else _evidence_gaps(
            report,
            minimum_pairs=minimum_pairs,
            maximum_ci_width=maximum_ci_width,
            arm=arm,
            coverage=memory_coverage,
        )
    )
    notes: list[str] = []
    if verdict == NOT_DEMONSTRATED and gaps:
        notes.append(
            "no effect was demonstrated, but the evidence is too thin to call "
            "this a null result"
        )
    if verdict != DEMONSTRATED and pays_more:
        notes.append(
            f"the full stack costs {cost_delta:+.4f} USD per incident more than "
            "this arm without a demonstrated diagnosis gain"
        )
    if verdict != DEMONSTRATED and slower:
        notes.append(
            f"the full stack is {latency['mean_delta']:+.1f}s slower than this "
            "arm without a demonstrated diagnosis gain"
        )
    if verdict == REFUTED:
        notes.append(
            "removing this component measurably improved outcomes; it does not "
            "earn its complexity"
        )

    return {
        "arm": arm,
        "summary": ARMS[arm].summary,
        "removed_components": arm_attestation["removed_components"],
        "verdict": verdict,
        "diagnosis_verdict": diagnosis_verdict,
        "quality_verdict": quality_verdict,
        "recovery_verdict": recovery_verdict,
        "insufficient_evidence": gaps,
        "notes": notes,
        "cost_of_complexity": {
            "cost_usd_delta": cost_delta,
            "cost_usd_bootstrap_95": None if cost is None else cost["bootstrap_95"],
            "latency_seconds_delta": latency["mean_delta"],
            "latency_seconds_bootstrap_95": latency["bootstrap_95"],
            "full_stack_costs_more": pays_more,
            "full_stack_is_slower": slower,
        },
        "attestation": {"arm": arm_attestation, "control": full_attestation},
        "paired_report": report,
    }


def build_ablation_report(
    trials: tuple[TrialRecord, ...],
    artifact: ArtifactEvidence,
    *,
    full_candidate_id: str,
    full_manifest: dict[str, Any],
    arms: list[tuple[str, str, dict[str, Any]]],
    minimum_pairs: int = 20,
    maximum_ci_width: float = 0.20,
    pass_k: int = 3,
    bootstrap_seed: int = 1729,
    memory_coverage: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if not arms:
        raise AblationEvalError("at least one ablation arm is required")
    names = [name for name, _, _ in arms]
    if len(names) != len(set(names)):
        raise AblationEvalError("each arm may be compared once")

    results = [
        evaluate_arm(
            trials,
            artifact,
            arm=name,
            arm_candidate_id=candidate_id,
            arm_manifest=manifest,
            full_candidate_id=full_candidate_id,
            full_manifest=full_manifest,
            minimum_pairs=minimum_pairs,
            maximum_ci_width=maximum_ci_width,
            pass_k=pass_k,
            bootstrap_seed=bootstrap_seed,
            memory_coverage=memory_coverage,
        )
        for name, candidate_id, manifest in arms
    ]
    verdicts = {result["arm"]: result["verdict"] for result in results}
    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": results[0]["paired_report"]["experiment_id"],
        "control_candidate_id": full_candidate_id,
        "arms": results,
        # Deliberately not a single pass/fail. Each component answers for
        # itself; collapsing three independent questions into one verdict is
        # how an architecture gets credited for the one part that worked.
        "verdicts": verdicts,
        "components_earning_complexity": sorted(
            arm for arm, verdict in verdicts.items() if verdict == DEMONSTRATED
        ),
        "components_refuted": sorted(
            arm for arm, verdict in verdicts.items() if verdict == REFUTED
        ),
        "policy": {
            "superiority_floor": SUPERIORITY_FLOOR,
            "minimum_pairs": minimum_pairs,
            "maximum_ci_width": maximum_ci_width,
            "pass_k": pass_k,
            "bootstrap_seed": bootstrap_seed,
            "rule": (
                "a component earns its complexity only when the lower bound of "
                "the paired full-minus-arm diagnosis delta is strictly above "
                "zero; an interval containing zero is reported as "
                "NOT_DEMONSTRATED, never as a pass"
            ),
        },
        "raw_artifacts": [
            {
                "path": artifact.path,
                "sha256": artifact.sha256,
                "records": artifact.records,
            }
        ],
    }


def _parse_arm_spec(value: str) -> tuple[str, str, Path]:
    parts = value.split("=")
    if len(parts) != 3 or not all(part.strip() for part in parts):
        raise argparse.ArgumentTypeError(
            f"--arm must be NAME=CANDIDATE_ID=MANIFEST_PATH, got {value!r}"
        )
    name, candidate_id, manifest = (part.strip() for part in parts)
    return name, candidate_id, Path(manifest)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure whether each architectural component earns its complexity"
    )
    parser.add_argument("trials", type=Path, help="Raw trial JSONL from sre_bench.py")
    parser.add_argument("--full-id", required=True, help="Control arm's candidate ID")
    parser.add_argument(
        "--full-manifest",
        type=Path,
        required=True,
        help="Run manifest the control arm actually ran under",
    )
    parser.add_argument(
        "--arm",
        action="append",
        type=_parse_arm_spec,
        required=True,
        metavar="NAME=CANDIDATE_ID=MANIFEST_PATH",
        help="An ablation arm to compare against the control (repeatable)",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minimum-pairs", type=int, default=20)
    parser.add_argument("--maximum-ci-width", type=float, default=0.20)
    parser.add_argument("--pass-k", type=int, default=3)
    parser.add_argument("--bootstrap-seed", type=int, default=1729)
    parser.add_argument(
        "--memory-coverage",
        type=Path,
        help=(
            "Corpus-coverage artifact from benchmarks/ablation_coverage.py. "
            "Without it a null result for the no_memory arm cannot be "
            "distinguished from a corpus that never held anything to remove."
        ),
    )
    args = parser.parse_args(argv)

    try:
        trials, artifact = load_trials(args.trials)
        report = build_ablation_report(
            trials,
            artifact,
            full_candidate_id=args.full_id,
            full_manifest=load_manifest(args.full_manifest),
            arms=[
                (name, candidate_id, load_manifest(path))
                for name, candidate_id, path in args.arm
            ],
            minimum_pairs=args.minimum_pairs,
            maximum_ci_width=args.maximum_ci_width,
            pass_k=args.pass_k,
            bootstrap_seed=args.bootstrap_seed,
            memory_coverage=(
                load_coverage(args.memory_coverage) if args.memory_coverage else None
            ),
        )
    except (AblationEvalError, StatisticalEvalError) as exc:
        parser.error(str(exc))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(report["verdicts"], sort_keys=True))
    # A refuted component actively costs more and performs worse; that is a
    # defect, not a neutral finding. An undemonstrated one is honest ignorance
    # and does not fail the run.
    return 2 if report["components_refuted"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
