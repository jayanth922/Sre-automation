#!/usr/bin/env python3
"""Tests for the learned-memory corpus preflight.

The preflight exists to stop one specific false conclusion: `no_memory`
reporting NOT_DEMONSTRATED because the corpus never held anything to remove,
which is indistinguishable in the artifact from the finding that learned
memory does not earn its complexity.

That makes the preflight's own correctness load-bearing in an unusual way — a
bug here does not produce a wrong number, it produces a *reassuring* one. The
field-mapping test below guards a bug that actually occurred while building
this: reading the alert the wrong way yields failure class `unknown` for every
scenario, which matches no skill, and reports a corpus gap that is really a
probe gap.
"""

from dataclasses import dataclass, field
from typing import Any, Dict

from benchmarks import ablation_coverage


@dataclass
class _Scenario:
    name: str
    alert: Dict[str, Any] = field(default_factory=dict)


@dataclass
class _Signature:
    failure_class: str


@dataclass
class _Skill:
    skill_id: str


def _assess(scenarios, *, hits_for=(), incident_points=0, path="semantic"):
    """Assess with a stub retrieval that fires only for the named scenarios."""
    return ablation_coverage.assess_split(
        scenarios,
        propose=lambda probe: (
            [_Skill("skill-x")] if probe["alert_name"] in hits_for else []
        ),
        signature_of=lambda probe: _Signature("latency"),
        incident_points=incident_points,
        path=path,
    )


# --- The probe's own correctness ---------------------------------------------


def test_the_dataset_alert_shape_is_mapped_to_the_runtime_signature_shape():
    """The dataset stores `alert.alertname` / `alert.service` flat; the runtime
    signature reads `alert_name` and `labels.service`. Conflating them makes
    every scenario look uncovered."""
    scenario = _Scenario(
        name="inventory_slow_queries",
        alert={"alertname": "InventorySlowQueries", "service": "inventory-service"},
    )
    probe = ablation_coverage.alert_probe(scenario)

    assert probe["alert_name"] == "InventorySlowQueries"
    assert probe["labels"]["service"] == "inventory-service"


def test_a_missing_service_is_named_unknown_rather_than_none():
    """`unknown` is the value the signature's service comparison expects; None
    would score as a match against another absent service."""
    probe = ablation_coverage.alert_probe(_Scenario(name="s", alert={"alertname": "A"}))

    assert probe["labels"]["service"] == "unknown"


# --- Coverage accounting ------------------------------------------------------


def test_a_scenario_with_no_skill_and_no_recall_is_blind():
    report = _assess([_Scenario("s1", {"alertname": "A"})], incident_points=0)

    assert report["blind_pairs"] == ["s1"]
    assert report["observable_pairs"] == 0


def test_reachable_incident_recall_makes_a_skill_less_scenario_observable():
    """Recall and skills are separate halves; either one alone is enough for
    the arm to have something to remove."""
    report = _assess([_Scenario("s1", {"alertname": "A"})], incident_points=12)

    assert report["blind_pairs"] == []
    assert report["observable_pairs"] == 1
    assert report["skill_hits"] == 0


def test_partial_coverage_counts_only_the_scenarios_that_can_retrieve():
    scenarios = [
        _Scenario("hit", {"alertname": "A"}),
        _Scenario("miss", {"alertname": "B"}),
    ]
    report = _assess(scenarios, hits_for=("A",), incident_points=0)

    assert report["skill_hits"] == 1
    assert report["observable_pairs"] == 1
    assert report["blind_pairs"] == ["miss"]


def test_an_unreachable_collection_is_unknown_not_empty():
    """A store that cannot be reached must not be reported as a store that is
    empty — the remedies are opposite."""
    report = _assess([_Scenario("s1", {"alertname": "A"})], incident_points=None)

    assert report["incident_memory_points"] is None
    assert report["recall_possible"] is False
    assert any(
        "unreachable" in line for line in ablation_coverage.verdict_lines(report)
    )


def test_cli_refuses_an_unscoped_memory_count(capsys):
    code = ablation_coverage.main(["--organization-id", "org-only"])

    assert code == ablation_coverage.EXIT_ERROR
    assert "--organization-id and --cluster-id are required" in capsys.readouterr().err


# --- What the operator is told ------------------------------------------------


def test_a_fully_blind_split_says_the_verdict_would_be_an_artifact():
    report = _assess([_Scenario("s1", {"alertname": "A"})], incident_points=0)
    lines = " ".join(ablation_coverage.verdict_lines(report))

    assert "BLIND" in lines
    assert "artifact" in lines


def test_an_empty_incident_collection_is_called_out_even_when_skills_cover():
    report = _assess(
        [_Scenario("s1", {"alertname": "A"})], hits_for=("A",), incident_points=0
    )
    lines = " ".join(ablation_coverage.verdict_lines(report))

    assert "COVERED" in lines
    assert "provably inert" in lines


# --- Which stack was measured -------------------------------------------------
#
# The same corpus reported 6/6 under the agent's own interpreter and 3/6 under
# the container's bare `python`, which has none of the project's dependencies
# and silently falls back to keyword matching. The 3/6 looked exactly like a
# finding and was quoted as one. A coverage number that depends on which
# interpreter produced it, and does not say so, is worse than none.


def test_the_retrieval_path_is_recorded_in_the_report():
    report = _assess([_Scenario("s1", {"alertname": "A"})], path="keyword_only")

    assert report["retrieval_path"] == "keyword_only"


def test_a_keyword_only_run_says_semantic_recall_was_off():
    report = _assess(
        [_Scenario("s1", {"alertname": "A"})],
        hits_for=("A",),
        incident_points=5,
        path="keyword_only",
    )
    lines = " ".join(ablation_coverage.verdict_lines(report))

    assert "keyword_only" in lines
    assert "OFF" in lines


def test_every_verdict_carries_the_transferability_caveat():
    """Including the COVERED case — that is the one an operator acts on."""
    report = _assess(
        [_Scenario("s1", {"alertname": "A"})], hits_for=("A",), incident_points=5
    )
    lines = " ".join(ablation_coverage.verdict_lines(report))

    assert "COVERED" in lines
    assert "agent's own process" in lines


def test_the_path_is_read_from_the_stores_own_semantic_flag():
    class _Store:
        _semantic_available = True

    assert ablation_coverage.retrieval_path(_Store()) == "semantic"


def test_a_store_without_the_flag_is_keyword_only_not_an_error():
    """`InMemorySkillStore` has no such attribute; absence means no semantics."""
    assert ablation_coverage.retrieval_path(object()) == "keyword_only"


# --- The preflight must not write to what it measures -------------------------


def _reachable_semantic_store(monkeypatch, tmp_path, writes):
    """A store whose constructor reaches both writes without a live Qdrant.

    `_init_semantic` is stubbed down to the one line that matters here —
    the ensure/backfill calls — because standing up Qdrant and the embedding
    model would make this test slow, networked, and unable to prove anything
    the stub cannot.
    """
    from sre_agent import skill_store

    monkeypatch.setattr(
        skill_store.SemanticSkillStore,
        "_backfill_index",
        lambda self: writes.append("backfill"),
    )
    monkeypatch.setattr(
        skill_store.SemanticSkillStore,
        "_ensure_collection",
        lambda self: writes.append("ensure"),
    )
    monkeypatch.setattr(
        skill_store.SemanticSkillStore,
        "_init_semantic",
        lambda self, url: (self._ensure_collection(), self._backfill_index()),
    )
    monkeypatch.setattr(skill_store, "_GLOBAL_STORE", None)
    monkeypatch.setenv("SKILL_STORE_PATH", str(tmp_path / "skills.json"))
    return skill_store


def test_opening_the_store_suppresses_the_constructor_backfill(monkeypatch, tmp_path):
    """`SemanticSkillStore.__init__` upserts every verified skill into Qdrant.
    A preflight that did that would be reporting on a corpus it just wrote."""
    writes: list[str] = []
    _reachable_semantic_store(monkeypatch, tmp_path, writes)

    ablation_coverage.open_store_read_only()

    assert writes == []


def test_the_backfill_is_restored_afterwards(monkeypatch, tmp_path):
    """The suppression is scoped to construction; a later `add()` must still
    index, or the preflight would leave the process silently degraded."""
    writes: list[str] = []
    skill_store = _reachable_semantic_store(monkeypatch, tmp_path, writes)
    backfill = skill_store.SemanticSkillStore._backfill_index
    ensure = skill_store.SemanticSkillStore._ensure_collection

    ablation_coverage.open_store_read_only()

    assert skill_store.SemanticSkillStore._backfill_index is backfill
    assert skill_store.SemanticSkillStore._ensure_collection is ensure


def test_an_absent_semantic_collection_is_not_reported_as_an_active_path(monkeypatch):
    from sre_agent import skill_store

    class _Client:
        def get_collections(self):
            return type("Collections", (), {"collections": []})()

    store = type(
        "Store",
        (),
        {"_semantic_available": True, "_qdrant": _Client()},
    )()
    monkeypatch.setattr(skill_store, "get_skill_store", lambda: store)

    opened = ablation_coverage.open_store_read_only()

    assert opened is store
    assert ablation_coverage.retrieval_path(opened) == "keyword_only"


def test_a_preexisting_semantic_collection_keeps_the_path_active(monkeypatch):
    from sre_agent import skill_store

    class _Client:
        def get_collections(self):
            collection = type(
                "Collection", (), {"name": skill_store.SKILLS_COLLECTION}
            )()
            return type("Collections", (), {"collections": [collection]})()

    store = type(
        "Store",
        (),
        {"_semantic_available": True, "_qdrant": _Client()},
    )()
    monkeypatch.setattr(skill_store, "get_skill_store", lambda: store)

    assert (
        ablation_coverage.retrieval_path(ablation_coverage.open_store_read_only())
        == "semantic"
    )


def test_a_renamed_backfill_raises_rather_than_resuming_the_write(monkeypatch):
    """Failing closed matters more than usual here: the failure mode of a
    silent fallback is a preflight that mutates the corpus under measurement."""
    import pytest

    from sre_agent import skill_store

    monkeypatch.delattr(skill_store.SemanticSkillStore, "_backfill_index")

    with pytest.raises(RuntimeError, match="_backfill_index"):
        ablation_coverage.open_store_read_only()


def test_a_renamed_collection_initializer_also_fails_closed(monkeypatch):
    import pytest

    from sre_agent import skill_store

    monkeypatch.delattr(skill_store.SemanticSkillStore, "_ensure_collection")

    with pytest.raises(RuntimeError, match="_ensure_collection"):
        ablation_coverage.open_store_read_only()


# --- The split a published number is reported on ------------------------------


def _stop_at_the_dataset_load(monkeypatch):
    """Capture how main() asks for the split, then stop before Qdrant."""
    seen: Dict[str, Any] = {}

    def fake_load_dataset(root, version, split, **kwargs):
        seen["split"] = split
        seen.update(kwargs)
        raise RuntimeError("stop here; the load is all these tests need")

    monkeypatch.setattr(ablation_coverage, "load_dataset", fake_load_dataset)
    monkeypatch.delenv("BENCH_ALLOW_HOLDOUT", raising=False)
    monkeypatch.delenv("BENCH_DATASET_SPLIT", raising=False)
    return seen


def test_the_holdout_split_can_be_assessed_at_all(monkeypatch):
    """`load_dataset` refuses the protected split unless asked, and this was
    calling it without asking — so the one split a published number is
    reported on was the one split the preflight could never check."""
    seen = _stop_at_the_dataset_load(monkeypatch)

    exit_code = ablation_coverage.main(
        [
            "--split",
            "holdout",
            "--allow-holdout",
            "--organization-id",
            "org-1",
            "--cluster-id",
            "cluster-1",
        ]
    )

    assert exit_code == ablation_coverage.EXIT_ERROR  # the stub raised
    assert seen["split"] == "holdout"
    assert seen["allow_holdout"] is True


def test_the_holdout_stays_protected_unless_it_is_asked_for(monkeypatch):
    """Assessing it reveals which holdout scenarios learned memory can reach,
    so it takes the same explicit opt-in `sre_bench` requires."""
    seen = _stop_at_the_dataset_load(monkeypatch)

    ablation_coverage.main(
        ["--organization-id", "org-1", "--cluster-id", "cluster-1"]
    )

    assert seen["allow_holdout"] is False


def test_the_environment_grants_holdout_access_the_same_way_the_benchmark_does(
    monkeypatch,
):
    """BENCH_ALLOW_HOLDOUT, so a preflight and its run are configured alike."""
    seen = _stop_at_the_dataset_load(monkeypatch)
    monkeypatch.setenv("BENCH_ALLOW_HOLDOUT", "true")

    ablation_coverage.main(
        [
            "--split",
            "holdout",
            "--organization-id",
            "org-1",
            "--cluster-id",
            "cluster-1",
        ]
    )

    assert seen["allow_holdout"] is True


# --- A hit, and a hit that is about this incident -----------------------------


@dataclass
class _ScoredSkill:
    skill_id: str
    signature: Any


def test_a_retrieved_skill_is_not_yet_a_relevant_one():
    """The semantic path compares a cosine similarity against `propose_skills`'
    0.5, a number written for `match_score`'s scale, so it returns skills from
    unrelated failure classes. `skill_hit` counts those; `signature_hit` is the
    number a "learned memory helped" claim rests on."""
    scenarios = [_Scenario("oom_pair", {"alertname": "A"})]
    report = ablation_coverage.assess_split(
        scenarios,
        propose=lambda probe: [
            _ScoredSkill("latency-inventory-service", _Signature("latency"))
        ],
        signature_of=lambda probe: _Signature("oom"),
        incident_points=0,
        path="semantic",
        score_of=lambda signature, skill: (
            1.0 if signature.failure_class == skill.signature.failure_class else 0.0
        ),
    )

    assert report["skill_hits"] == 1
    assert report["signature_hits"] == 0
    assert report["pairs"][0]["signature_matches"] == []
    assert any("Weaker than that reads" in line
               for line in ablation_coverage.verdict_lines(report))


def test_a_skill_from_the_scenarios_own_failure_class_counts():
    scenarios = [_Scenario("oom_pair", {"alertname": "A"})]
    report = ablation_coverage.assess_split(
        scenarios,
        propose=lambda probe: [_ScoredSkill("oom-checkout-service", _Signature("oom"))],
        signature_of=lambda probe: _Signature("oom"),
        incident_points=0,
        path="semantic",
        score_of=lambda signature, skill: (
            1.0 if signature.failure_class == skill.signature.failure_class else 0.0
        ),
    )

    assert report["signature_hits"] == 1
    assert report["pairs"][0]["signature_matches"] == ["oom-checkout-service"]
    assert not any("Weaker than that reads" in line
                   for line in ablation_coverage.verdict_lines(report))


def test_ungraded_relevance_is_unknown_and_never_reported_as_zero():
    """A zero here would read like a finding — that the corpus holds nothing
    relevant — when it only means nothing checked."""
    report = _assess([_Scenario("p", {"alertname": "A"})], hits_for=("A",))

    assert report["skill_hits"] == 1
    assert report["signature_hits"] is None
    assert report["pairs"][0]["signature_hit"] is None
    assert any("not graded" in line for line in ablation_coverage.verdict_lines(report))
