#!/usr/bin/env python3
"""The offline retrieval harness: derived labels, and a gate that can fail.

The value of this harness is entirely in where its labels come from, so the
tests concentrate there — that a paraphrase probe is verified against the
taxonomy before it is used, that the isolation probes really are issued under
a different tenant, that a skipped store is never reported as a pass, and that
the gate blocks when recall regresses rather than printing a number nobody
reads.
"""

import json

import pytest

from benchmarks import retrieval_eval as re_eval
from benchmarks.scoring import ScenarioSpec
from sre_agent.retrieval_metrics import MEMORY_STORE, SKILL_STORE


def _scenario(name, alertname, service, actions=("restart",)):
    return ScenarioSpec(
        name=name,
        alert={
            "alertname": alertname,
            "service": service,
            "severity": "warning",
            "summary": f"{alertname} on {service}",
            "description": "fixture",
        },
        ground_truth_service=service,
        root_cause_keywords=["fixture"],
        expected_action_types=set(actions),
        expected_severity_band={"SEV3"},
        recovery_probe=None,
    )


# ------------------------------------------------------------ derived labels


def test_paraphrase_is_verified_against_the_taxonomy_before_use():
    # "slow" and "latency" both map to the latency class, so a paraphrase
    # exists and lands where intended.
    paraphrase = re_eval.paraphrase_alert_name("InventorySlowQueries")
    assert paraphrase is not None
    assert paraphrase.lower() != "inventoryslowqueries"
    from sre_agent.skill_store import _failure_class

    assert _failure_class(paraphrase) == _failure_class("InventorySlowQueries")


def test_no_paraphrase_is_invented_when_the_class_has_one_keyword():
    # high_error_rate is reachable only through "error", which the original
    # name already contains. Returning None is the honest answer; the harness
    # records it as a skipped probe rather than fabricating one.
    assert re_eval.paraphrase_alert_name("CheckoutHighErrorRate") is None
    assert re_eval.paraphrase_alert_name("SomethingEntirelyNovel") is None


def test_distractor_probes_cannot_match_any_corpus_class():
    from sre_agent.skill_store import _failure_class

    names = re_eval.distractor_alert_names(["latency", "oom"])
    assert _failure_class(names[0]) == "unknown"
    for name in names[1:]:
        assert _failure_class(name) not in {"latency", "oom"}


def test_corpus_probes_cover_every_derived_family():
    corpus = re_eval.build_skill_corpus(
        [
            _scenario("slow", "InventorySlowQueries", "inventory-service"),
            _scenario("oom", "CheckoutOOMKilled", "checkout-service"),
        ]
    )
    kinds = {probe.kind for probe in corpus.probes}
    assert kinds == {
        "self",
        "paraphrase",
        "tenant_isolation",
        "cluster_isolation",
        "distractor",
        "invalidated",
    }


def test_isolation_probes_are_issued_under_a_different_tenant():
    corpus = re_eval.build_skill_corpus(
        [_scenario("slow", "InventorySlowQueries", "inventory-service")]
    )
    tenant = next(p for p in corpus.probes if p.kind == "tenant_isolation")
    cluster = next(p for p in corpus.probes if p.kind == "cluster_isolation")

    assert tenant.organization_id != re_eval.CORPUS_ORG
    assert cluster.cluster_id != re_eval.CORPUS_CLUSTER
    assert tenant.query.expects_empty and cluster.query.expects_empty


def test_the_corpus_is_built_through_the_production_promotion_path():
    # record_successful_remediation enforces RESOLVED provenance and a source
    # incident; a corpus assembled around those checks would measure a store
    # the runtime would never have populated.
    corpus = re_eval.build_skill_corpus(
        [_scenario("slow", "InventorySlowQueries", "inventory-service")]
    )
    skills = corpus.store.all()
    assert skills
    for skill in skills:
        assert skill.verification_status == "RESOLVED"
        assert skill.source_incident_id


def test_a_scenario_set_that_yields_nothing_is_an_error():
    with pytest.raises(re_eval.RetrievalEvalError):
        re_eval.build_skill_corpus([])


# ------------------------------------------------------------------ scoring


def test_the_skill_store_recalls_its_own_incidents_first():
    corpus = re_eval.build_skill_corpus(
        [
            _scenario("slow", "InventorySlowQueries", "inventory-service"),
            _scenario("oom", "CheckoutOOMKilled", "checkout-service"),
        ]
    )
    report = re_eval.evaluate_skill_store(corpus)

    assert report.store == SKILL_STORE
    assert report.mrr == pytest.approx(1.0)
    assert report.hit_rate == pytest.approx(1.0)
    assert report.false_positive_rate == 0.0
    assert report.empty_probe_queries >= 4


def test_an_invalidated_skill_stops_being_recalled():
    corpus = re_eval.build_skill_corpus(
        [_scenario("slow", "InventorySlowQueries", "inventory-service")]
    )
    probe = next(p for p in corpus.probes if p.kind == "invalidated")

    from sre_agent.skill_store import signature_from_alert

    hits = corpus.store.find_matching(
        signature_from_alert(
            probe.alert,
            organization_id=probe.organization_id,
            cluster_id=probe.cluster_id,
        )
    )
    assert hits == []


def test_a_tenant_leak_fails_the_gate():
    corpus = re_eval.build_skill_corpus(
        [_scenario("slow", "InventorySlowQueries", "inventory-service")]
    )
    # Drop the tenant boundary the way a regression would: make every stored
    # skill belong to the querying tenant.
    from sre_agent.skill_store import IncidentSignature

    leaked = {}
    for key, skill in corpus.store._skills.items():
        skill.signature = IncidentSignature(
            alert_name=skill.signature.alert_name,
            service=skill.signature.service,
            failure_class=skill.signature.failure_class,
            organization_id=re_eval.OTHER_ORG,
            cluster_id=skill.signature.cluster_id,
        )
        leaked[key] = skill
    corpus.store._skills = leaked

    report = re_eval.evaluate_skill_store(corpus)
    reasons = re_eval.check_thresholds(report, re_eval.Thresholds())

    assert report.false_positive_rate > 0.0
    assert any("false_positive_rate" in reason for reason in reasons)


def test_a_recall_regression_fails_the_gate():
    corpus = re_eval.build_skill_corpus(
        [_scenario("slow", "InventorySlowQueries", "inventory-service")]
    )
    corpus.store._skills = {}  # the index came back empty

    report = re_eval.evaluate_skill_store(corpus)
    reasons = re_eval.check_thresholds(report, re_eval.Thresholds())

    assert report.hit_rate == 0.0
    assert any("mrr" in reason for reason in reasons)
    assert any("hit_rate" in reason for reason in reasons)


def test_a_report_with_no_probes_fails_rather_than_passing_vacuously():
    from sre_agent.retrieval_metrics import aggregate

    empty = aggregate(SKILL_STORE, [])
    assert re_eval.check_thresholds(empty, re_eval.Thresholds()) == [
        f"{SKILL_STORE}: no probes ran"
    ]


# -------------------------------------------------------------------- report


def test_build_report_against_the_checked_in_dataset(tmp_path):
    report = re_eval.build_report(
        dataset_root=re_eval.Path("evals/benchmarks/datasets"),
        dataset_version_dir="v1",
        splits=["train", "dev"],
        k=5,
        thresholds=re_eval.Thresholds(),
    )

    assert report["status"] == "PASS"
    assert report["stores"][SKILL_STORE]["mrr"] == 1.0
    # Every split is named with the digest it was read at, so a corpus that
    # drifted cannot quietly change what these numbers mean.
    assert all(entry["sha256"] for entry in report["dataset"]["splits"])
    assert report["probes"]["tenant_isolation"] >= 1


def test_a_skipped_memory_store_is_not_a_pass():
    report = re_eval.build_report(
        dataset_root=re_eval.Path("evals/benchmarks/datasets"),
        dataset_version_dir="v1",
        splits=["dev"],
        k=5,
        thresholds=re_eval.Thresholds(),
    )
    memory = report["stores"][MEMORY_STORE]
    assert memory["status"] == "skipped"
    assert "mrr" not in memory


def test_holdout_is_refused():
    with pytest.raises(re_eval.RetrievalEvalError, match="holdout"):
        re_eval.load_scenarios(re_eval.Path("evals/benchmarks/datasets"), "v1", ["holdout"])


def test_the_production_memory_collection_is_refused():
    from sre_agent.memory_store import INCIDENTS_COLLECTION

    with pytest.raises(re_eval.RetrievalEvalError, match="production collection"):
        re_eval.evaluate_memory_store(
            [],
            qdrant_url="http://localhost:6333",
            collection_name=INCIDENTS_COLLECTION,
        )


def test_cli_writes_a_report_and_exits_zero_on_pass(tmp_path):
    output = tmp_path / "release-retrieval.json"
    code = re_eval.main(["--output", str(output)])

    assert code == 0
    payload = json.loads(output.read_text())
    assert payload["schema_version"] == re_eval.SCHEMA_VERSION
    assert payload["status"] == "PASS"
    assert payload["gated_metrics"] == ["mrr", "hit_rate", "false_positive_rate"]


def test_cli_exits_nonzero_when_the_gate_fails(tmp_path):
    output = tmp_path / "release-retrieval.json"
    # A threshold no store can meet stands in for a regression; the point is
    # that a failing report is a non-zero exit, not a printed number.
    code = re_eval.main(
        ["--output", str(output), "--maximum-false-positive-rate", "-0.1"]
    )

    assert code == 1
    assert json.loads(output.read_text())["status"] == "FAIL"
