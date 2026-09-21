#!/usr/bin/env python3
"""Tests for the ablation arms and the harness that compares them.

Two properties matter more than the rest and are asserted first:

* with the environment variable unset, the graph is byte-for-byte the graph
  that shipped — an ablation facility that perturbs production is worse than
  no ablation facility;
* every way of getting the experiment wrong fails loudly rather than
  producing a plausible number.
"""

import json
import os

import pytest

from benchmarks import ablation_eval
from benchmarks.statistical_eval import (
    build_trial_record,
    configuration_fingerprint,
    load_trials,
    make_pair_id,
)
from sre_agent import ablation
from sre_agent.ablation import (
    ARMS,
    ENV_VAR,
    FULL_ARM,
    AblationError,
    current_ablation,
    resolve_arm,
)

DATASET_SHA = "d" * 64
EXPERIMENT_ID = "exp-ablation-tests"
PAIR_SEED = "seed-ablation"


# ---------------------------------------------------------------- arm parsing


def test_unset_environment_is_production_not_an_experiment():
    config = resolve_arm(None)
    assert config.arm.name == FULL_ARM
    assert config.experiment_active is False
    assert config.writes_learned_memory is True
    assert "production" in config.describe()


def test_control_arm_is_an_experiment_even_though_nothing_is_removed():
    config = resolve_arm(FULL_ARM)
    assert config.experiment_active is True
    assert config.arm.removed == ()
    # The control is a measurement run, so its learning is frozen too.
    assert config.writes_learned_memory is False


def test_a_typo_fails_closed_rather_than_running_the_control():
    with pytest.raises(AblationError) as excinfo:
        resolve_arm("no_reflectr")
    message = str(excinfo.value)
    assert "no_reflectr" in message
    # The message has to name the valid arms or the operator retries blind.
    for arm in ARMS:
        assert arm in message


@pytest.mark.parametrize("arm", sorted(ARMS))
def test_every_arm_freezes_learned_memory_writes_during_an_experiment(arm):
    assert resolve_arm(arm).writes_learned_memory is False


@pytest.mark.parametrize("arm", sorted(ARMS))
def test_each_arm_removes_at_most_one_component(arm):
    assert len(resolve_arm(arm).arm.removed) <= 1


def test_only_the_no_memory_arm_stops_reading_learned_memory():
    reads = {arm: resolve_arm(arm).reads_learned_memory for arm in ARMS}
    assert reads == {
        "full": True,
        "single_agent": True,
        "no_reflector": True,
        "no_memory": False,
    }


def test_manifest_entry_carries_the_arm_into_the_fingerprint(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "no_memory")
    entry = current_ablation().manifest_entry()
    assert entry == {
        "ablation_arm": "no_memory",
        "ablation_experiment": True,
        "learned_memory_writes": False,
    }


def test_current_ablation_tracks_the_environment(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert current_ablation().experiment_active is False
    monkeypatch.setenv(ENV_VAR, "single_agent")
    assert current_ablation().arm.name == "single_agent"


# ----------------------------------------------------------------- arm shapes


def _graph_shape(monkeypatch, arm):
    """Build the graph under one arm and return its node and edge sets."""
    if arm is None:
        monkeypatch.delenv(ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(ENV_VAR, arm)
    monkeypatch.setenv("ACT_PHASE_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", os.getenv("ANTHROPIC_API_KEY", "test"))
    from sre_agent.graph_builder import build_multi_agent_graph

    graph = build_multi_agent_graph(tools=[], llm_provider="anthropic").get_graph()
    return (
        frozenset(graph.nodes),
        frozenset((edge.source, edge.target) for edge in graph.edges),
    )


def test_the_control_arm_graph_is_the_production_graph(monkeypatch):
    """The regression that would quietly invalidate every result."""
    assert _graph_shape(monkeypatch, FULL_ARM) == _graph_shape(monkeypatch, None)


def test_no_memory_changes_behaviour_without_changing_the_graph(monkeypatch):
    assert _graph_shape(monkeypatch, "no_memory") == _graph_shape(monkeypatch, None)


def test_no_reflector_removes_orient_and_routes_supervisor_to_the_planner(monkeypatch):
    nodes, edges = _graph_shape(monkeypatch, "no_reflector")
    assert "reflector" not in nodes
    # The reflector owns the re-investigation loop; without ORIENT there is no
    # loop to re-enter.
    assert "investigation_swarm" not in nodes
    assert ("supervisor", "planner") in edges
    # DECIDE and ACT are untouched: only one component was removed.
    assert {"planner", "aggregate", "approval_gate", "act_gate"} <= nodes


def test_single_agent_collapses_the_specialists_into_one_investigator(monkeypatch):
    nodes, edges = _graph_shape(monkeypatch, "single_agent")
    assert "single_agent" in nodes
    assert (
        not {
            "supervisor",
            "kubernetes_agent",
            "metrics_agent",
            "logs_agent",
            "github_agent",
            "infra_prescan",
        }
        & nodes
    )
    assert ("prepare", "single_agent") in edges
    assert ("single_agent", "reflector") in edges
    # The reflector survives: this arm removes the split, not ORIENT.
    assert "reflector" in nodes
    # And the report writer survives, so report quality stays comparable.
    assert "aggregate" in nodes


def _configured_tools(agent_name):
    from sre_agent.agent_nodes import _load_agent_config

    return set(_load_agent_config()["agents"].get(agent_name, {}).get("tools", []))


def test_the_single_investigator_holds_every_specialist_tool():
    """A baseline with fewer tools measures tool access, not architecture."""
    single = _configured_tools("single_agent")
    assert single
    for specialist in (
        "kubernetes_agent",
        "metrics_agent",
        "logs_agent",
        "github_agent",
    ):
        missing = _configured_tools(specialist) - single
        assert not missing, f"single_agent is missing {specialist} tools: {missing}"


def test_the_single_investigator_stays_read_only():
    """The specialists it replaces are read-only; a baseline that can act is
    not the same experiment."""
    writes = {
        tool
        for tool in _configured_tools("single_agent")
        if any(
            verb in tool
            for verb in ("create_pr", "write_", "apply_", "delete_", "patch_", "scale_")
        )
    }
    assert not writes


# ------------------------------------------------------- harness attestation


def _manifest(arm, *, experiment=True, writes=False, code_sha="sha-under-test"):
    return {
        "provenance": {"code_sha": code_sha, "graph": {"sha256": "g" * 64}},
        "models": {"primary": "claude-opus-5"},
        "tools": {"count": 29},
        "runtime": {
            "ablation_arm": arm,
            "ablation_experiment": experiment,
            "learned_memory_writes": writes,
        },
    }


def _trial(
    candidate,
    fingerprint,
    index,
    *,
    quality,
    diagnosis=None,
    cost=0.30,
    latency=60.0,
):
    scenario = f"scenario-{index % 5}"
    if diagnosis is None:
        diagnosis = quality
    return build_trial_record(
        experiment_id=EXPERIMENT_ID,
        pair_id=make_pair_id(
            experiment_id=EXPERIMENT_ID,
            dataset_sha256=DATASET_SHA,
            scenario=scenario,
            scenario_version="v2",
            trial_index=index,
            pair_seed=PAIR_SEED,
        ),
        candidate_id=candidate,
        config_fingerprint=fingerprint,
        scenario=scenario,
        scenario_version="v2",
        dataset_sha256=DATASET_SHA,
        risk_class="high",
        oracle_status="VERIFIED_RECOVERED" if quality else "UNRESOLVED",
        resolved=quality,
        false_resolved=False,
        grader_status="PASS" if quality else "NOT_APPLICABLE",
        diagnosis_status="PASS" if diagnosis else "FAIL",
        safety_ok=True,
        mttr_seconds=120.0 if quality else None,
        latency_seconds=latency,
        cost_usd=cost,
        trace_complete=True,
        trace_span_count=12,
        trace_evidence_sha256="e" * 64,
        trace_evidence_artifact="s3://traces/x",
        failure_categories=[],
        oracle_artifact="s3://oracle/x",
        grader_artifact="s3://grader/x",
    )


def _corpus(
    tmp_path,
    *,
    full_manifest,
    arm_manifest,
    full_wins,
    arm_wins,
    full_diagnosis_wins=None,
    arm_diagnosis_wins=None,
    pairs=24,
    full_cost=0.40,
    arm_cost=0.15,
):
    """Write a paired trial artifact plus the two manifests that attest it."""
    full_fp = configuration_fingerprint(full_manifest)
    arm_fp = configuration_fingerprint(arm_manifest)
    if full_diagnosis_wins is None:
        full_diagnosis_wins = full_wins
    if arm_diagnosis_wins is None:
        arm_diagnosis_wins = arm_wins
    records = []
    for index in range(1, pairs + 1):
        records.append(
            _trial(
                "full-v1",
                full_fp,
                index,
                quality=index <= full_wins,
                diagnosis=index <= full_diagnosis_wins,
                cost=full_cost,
            )
        )
        records.append(
            _trial(
                "arm-v1",
                arm_fp,
                index,
                quality=index <= arm_wins,
                diagnosis=index <= arm_diagnosis_wins,
                cost=arm_cost,
            )
        )
    trials_path = tmp_path / "trials.jsonl"
    trials_path.write_text(
        "\n".join(json.dumps(record.to_dict(), sort_keys=True) for record in records)
        + "\n",
        encoding="utf-8",
    )
    return load_trials(trials_path)


def _report(
    tmp_path,
    *,
    full_manifest,
    arm_manifest,
    arm="single_agent",
    memory_coverage=None,
    **kwargs,
):
    trials, artifact = _corpus(
        tmp_path, full_manifest=full_manifest, arm_manifest=arm_manifest, **kwargs
    )
    return ablation_eval.build_ablation_report(
        trials,
        artifact,
        full_candidate_id="full-v1",
        full_manifest=full_manifest,
        arms=[(arm, "arm-v1", arm_manifest)],
        memory_coverage=memory_coverage,
    )


def _coverage(scenario_count=6, observable=6, incident_points=12, path="semantic"):
    return {
        "scenario_count": scenario_count,
        "observable_pairs": observable,
        "incident_memory_points": incident_points,
        "retrieval_path": path,
    }


def test_a_clear_benefit_is_reported_as_demonstrated(tmp_path):
    report = _report(
        tmp_path,
        full_manifest=_manifest(FULL_ARM),
        arm_manifest=_manifest("single_agent"),
        full_wins=24,
        arm_wins=10,
    )
    assert report["verdicts"] == {"single_agent": "DEMONSTRATED"}
    assert report["components_earning_complexity"] == ["single_agent"]
    assert report["components_refuted"] == []
    arm = report["arms"][0]
    assert arm["cost_of_complexity"]["full_stack_costs_more"] is True
    # A demonstrated component is allowed to cost more; that is the trade.
    assert arm["notes"] == []


def test_diagnosis_ablation_works_while_every_action_is_approval_gated(tmp_path):
    report = _report(
        tmp_path,
        full_manifest=_manifest(FULL_ARM),
        arm_manifest=_manifest("single_agent"),
        full_wins=0,
        arm_wins=0,
        full_diagnosis_wins=24,
        arm_diagnosis_wins=10,
    )

    arm = report["arms"][0]
    assert arm["verdict"] == "DEMONSTRATED"
    assert arm["diagnosis_verdict"] == "DEMONSTRATED"
    assert arm["quality_verdict"] == "NOT_DEMONSTRATED"
    assert arm["recovery_verdict"] == "NOT_DEMONSTRATED"
    assert arm["paired_report"]["paired"]["diagnosis"]["mean_delta"] > 0
    assert arm["paired_report"]["paired"]["quality"]["mean_delta"] == 0
    assert arm["paired_report"]["paired"]["recovery"]["mean_delta"] == 0


def test_no_difference_is_not_reported_as_a_pass(tmp_path):
    report = _report(
        tmp_path,
        full_manifest=_manifest(FULL_ARM),
        arm_manifest=_manifest("no_reflector"),
        arm="no_reflector",
        full_wins=18,
        arm_wins=18,
    )
    arm = report["arms"][0]
    assert arm["verdict"] == "NOT_DEMONSTRATED"
    assert report["components_earning_complexity"] == []
    # An identical-outcome run that still costs more must say so.
    assert any("without a demonstrated diagnosis gain" in note for note in arm["notes"])


def test_an_underpowered_null_is_flagged_rather_than_read_as_no_effect(tmp_path):
    report = _report(
        tmp_path,
        full_manifest=_manifest(FULL_ARM),
        arm_manifest=_manifest("no_memory"),
        arm="no_memory",
        full_wins=3,
        arm_wins=3,
        pairs=4,
    )
    arm = report["arms"][0]
    assert arm["verdict"] == "NOT_DEMONSTRATED"
    assert any("paired trials" in gap for gap in arm["insufficient_evidence"])
    assert any("too thin" in note for note in arm["notes"])


# --- Learned-memory corpus coverage -------------------------------------------
#
# Writes are frozen for every arm during an experiment, so whatever `full`
# retrieves has to pre-exist the run. A corpus that matches nothing makes the
# two arms behaviourally identical, and the NOT_DEMONSTRATED that follows reads
# exactly like the finding "learned memory does not earn its complexity".


def test_a_blind_corpus_makes_the_memory_arms_null_result_uninformative(tmp_path):
    report = _report(
        tmp_path,
        full_manifest=_manifest(FULL_ARM),
        arm_manifest=_manifest("no_memory"),
        arm="no_memory",
        full_wins=18,
        arm_wins=18,
        memory_coverage=_coverage(observable=0, incident_points=0),
    )
    arm = report["arms"][0]
    assert arm["verdict"] == "NOT_DEMONSTRATED"
    assert any(
        "removed nothing the control actually had" in gap
        for gap in arm["insufficient_evidence"]
    )


def test_unproven_coverage_is_itself_an_evidence_gap(tmp_path):
    """Absent coverage evidence is not evidence of coverage."""
    report = _report(
        tmp_path,
        full_manifest=_manifest(FULL_ARM),
        arm_manifest=_manifest("no_memory"),
        arm="no_memory",
        full_wins=18,
        arm_wins=18,
    )
    assert any(
        "ablation_coverage.py" in gap
        for gap in report["arms"][0]["insufficient_evidence"]
    )


def test_partial_coverage_is_flagged_as_dilution(tmp_path):
    report = _report(
        tmp_path,
        full_manifest=_manifest(FULL_ARM),
        arm_manifest=_manifest("no_memory"),
        arm="no_memory",
        full_wins=18,
        arm_wins=18,
        memory_coverage=_coverage(scenario_count=6, observable=3),
    )
    assert any(
        "only 3 of 6 scenarios" in gap
        for gap in report["arms"][0]["insufficient_evidence"]
    )


def test_inert_incident_recall_is_named_separately_from_skills(tmp_path):
    """Half the arm can be dead while the other half still measures."""
    report = _report(
        tmp_path,
        full_manifest=_manifest(FULL_ARM),
        arm_manifest=_manifest("no_memory"),
        arm="no_memory",
        full_wins=18,
        arm_wins=18,
        memory_coverage=_coverage(observable=6, incident_points=0),
    )
    gaps = report["arms"][0]["insufficient_evidence"]
    assert any("only the verified-skill half was measured" in gap for gap in gaps)
    assert not any("removed nothing" in gap for gap in gaps)


def test_full_coverage_adds_no_corpus_gap(tmp_path):
    report = _report(
        tmp_path,
        full_manifest=_manifest(FULL_ARM),
        arm_manifest=_manifest("no_memory"),
        arm="no_memory",
        full_wins=18,
        arm_wins=18,
        memory_coverage=_coverage(),
    )
    gaps = report["arms"][0]["insufficient_evidence"]
    assert not any("learned memory" in gap or "recall" in gap for gap in gaps)


def test_coverage_of_unknown_provenance_is_not_accepted_as_evidence(tmp_path):
    """The preflight answers differently depending on where it runs — 6/6 on an
    operator host with the dev extras, 3/6 in the agent container without them.
    An artifact that does not name its retrieval path cannot be attributed."""
    coverage = _coverage()
    del coverage["retrieval_path"]
    report = _report(
        tmp_path,
        full_manifest=_manifest(FULL_ARM),
        arm_manifest=_manifest("no_memory"),
        arm="no_memory",
        full_wins=18,
        arm_wins=18,
        memory_coverage=coverage,
    )
    gaps = report["arms"][0]["insufficient_evidence"]
    assert any("retrieval path" in gap for gap in gaps)


def test_corpus_coverage_is_not_demanded_of_arms_that_keep_memory(tmp_path):
    """`single_agent` and `no_reflector` both keep learned memory; asking them
    for a corpus artifact would be noise on an unrelated arm."""
    report = _report(
        tmp_path,
        full_manifest=_manifest(FULL_ARM),
        arm_manifest=_manifest("no_reflector"),
        arm="no_reflector",
        full_wins=18,
        arm_wins=18,
    )
    assert not any(
        "ablation_coverage.py" in gap
        for gap in report["arms"][0]["insufficient_evidence"]
    )


def test_a_component_that_hurts_is_refuted_and_fails_the_run(tmp_path):
    report = _report(
        tmp_path,
        full_manifest=_manifest(FULL_ARM),
        arm_manifest=_manifest("single_agent"),
        full_wins=6,
        arm_wins=24,
    )
    arm = report["arms"][0]
    assert arm["verdict"] == "REFUTED"
    assert report["components_refuted"] == ["single_agent"]
    assert any("does not earn its complexity" in note for note in arm["notes"])


def test_a_mislabelled_arm_is_rejected(tmp_path):
    """The hole `BENCH_CONFIG_FINGERPRINT` leaves open on its own."""
    arm_manifest = _manifest("no_memory")
    with pytest.raises(ablation_eval.AblationEvalError, match="ablation_arm"):
        _report(
            tmp_path,
            full_manifest=_manifest(FULL_ARM),
            arm_manifest=arm_manifest,
            arm="single_agent",
            full_wins=24,
            arm_wins=10,
        )


def test_trials_from_a_different_configuration_are_rejected(tmp_path):
    full_manifest = _manifest(FULL_ARM)
    arm_manifest = _manifest("single_agent")
    trials, artifact = _corpus(
        tmp_path,
        full_manifest=full_manifest,
        arm_manifest=arm_manifest,
        full_wins=24,
        arm_wins=10,
    )
    # Same declared arm, but the run used a different model.
    substituted = _manifest("single_agent")
    substituted["models"] = {"primary": "some-other-model"}
    with pytest.raises(ablation_eval.AblationEvalError, match="does not match"):
        ablation_eval.build_ablation_report(
            trials,
            artifact,
            full_candidate_id="full-v1",
            full_manifest=full_manifest,
            arms=[("single_agent", "arm-v1", substituted)],
        )


def test_a_control_run_with_learning_live_is_not_a_control(tmp_path):
    full_manifest = _manifest(FULL_ARM, experiment=False, writes=True)
    with pytest.raises(ablation_eval.AblationEvalError, match="not recorded under"):
        _report(
            tmp_path,
            full_manifest=full_manifest,
            arm_manifest=_manifest("single_agent"),
            full_wins=24,
            arm_wins=10,
        )


def test_an_arm_that_wrote_learned_memory_is_rejected(tmp_path):
    arm_manifest = _manifest("single_agent", writes=True)
    with pytest.raises(ablation_eval.AblationEvalError, match="not frozen"):
        _report(
            tmp_path,
            full_manifest=_manifest(FULL_ARM),
            arm_manifest=arm_manifest,
            full_wins=24,
            arm_wins=10,
        )


def test_arms_run_at_different_revisions_are_rejected(tmp_path):
    arm_manifest = _manifest("single_agent", code_sha="a-later-commit")
    with pytest.raises(ablation_eval.AblationEvalError, match="code_sha"):
        _report(
            tmp_path,
            full_manifest=_manifest(FULL_ARM),
            arm_manifest=arm_manifest,
            full_wins=24,
            arm_wins=10,
        )


def test_the_control_cannot_be_compared_against_itself(tmp_path):
    with pytest.raises(ablation_eval.AblationEvalError, match="against itself"):
        _report(
            tmp_path,
            full_manifest=_manifest(FULL_ARM),
            arm_manifest=_manifest(FULL_ARM),
            arm=FULL_ARM,
            full_wins=24,
            arm_wins=10,
        )


def test_an_unknown_arm_name_is_rejected(tmp_path):
    with pytest.raises(ablation_eval.AblationEvalError, match="unknown ablation arm"):
        _report(
            tmp_path,
            full_manifest=_manifest(FULL_ARM),
            arm_manifest=_manifest("single_agent"),
            arm="single_agnet",
            full_wins=24,
            arm_wins=10,
        )


def test_a_manifest_without_a_runtime_section_cannot_attest_an_arm(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"provenance": {}, "models": {}, "tools": {}}))
    with pytest.raises(ablation_eval.AblationEvalError, match="no runtime section"):
        ablation_eval.load_manifest(path)


def test_the_api_row_wrapper_is_unwrapped(tmp_path):
    path = tmp_path / "manifest.json"
    inner = _manifest("no_memory")
    path.write_text(json.dumps({"job_id": "j1", "manifest": inner, "comparable": True}))
    assert ablation_eval.load_manifest(path) == inner


def test_a_run_the_runtime_already_called_non_comparable_is_rejected(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "job_id": "j1",
                "manifest": _manifest("no_memory"),
                "comparable": False,
                "non_comparable_reasons": ["model fell back mid-run"],
            }
        )
    )
    with pytest.raises(ablation_eval.AblationEvalError, match="non-comparable"):
        ablation_eval.load_manifest(path)


def test_the_superiority_floor_is_zero_not_a_tolerance():
    """A component credited for an interval containing zero is not measured."""
    assert ablation_eval.SUPERIORITY_FLOOR == 0.0
    assert ablation_eval._verdict([0.0, 0.4]) == "NOT_DEMONSTRATED"
    assert ablation_eval._verdict([0.01, 0.4]) == "DEMONSTRATED"
    assert ablation_eval._verdict([-0.4, -0.01]) == "REFUTED"


def test_the_harness_covers_every_arm_the_runtime_offers():
    """A new arm in `sre_agent.ablation` must not be silently uncomparable."""
    comparable = set(ARMS) - {FULL_ARM}
    assert comparable == {"single_agent", "no_reflector", "no_memory"}
    assert set(ARMS) == set(ablation.ARMS)
