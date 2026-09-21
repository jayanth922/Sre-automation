"""The evaluation mix is accounted for across corpora.

"22 scenarios" reads as a complete evaluation, and for v2 it is not one: the
mix the evaluation design asks for spans three corpora. This test is the
accounting, so the claim and the corpora cannot drift apart.

It used to assert that missing-data was measured nowhere, and was written to
fail as soon as that stopped being true. v3 measures it, so that assertion has
been replaced by the positive ones below — including the constraint that makes
a missing-data scenario gradable at all.

See the coverage table in `benchmarks/datasets/README.md`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
V2 = REPO / "benchmarks" / "datasets" / "v2"
V3 = REPO / "benchmarks" / "datasets" / "v3"
ADVERSARIAL = REPO / "benchmarks" / "adversarial" / "v1" / "cases.json"
DATASETS_README = REPO / "benchmarks" / "datasets" / "README.md"

# Category -> how many v2 scenarios must carry it. These are the three
# categories the mix requires that a recovery-probe corpus can express.
REQUIRED_V2_CATEGORIES = {"clean": 3, "noisy": 3, "multi_fault": 3}

# The adversarial categories standing in for the mix's prompt-injection and
# cross-tenant requirements. They are a different question — refusal, not
# recovery — graded by a different harness.
REQUIRED_ADVERSARIAL_CATEGORIES = {
    "indirect_injection",
    "malicious_runbook",
    "tool_result_spoofing",
    "cross_tenant_bait",
}


def _scenarios(root: Path):
    scenarios = []
    for split in ("train", "dev", "holdout"):
        payload = json.loads((root / f"{split}.json").read_text())
        scenarios.extend(payload["scenarios"])
    return scenarios


def _categories(root: Path):
    counts: dict[str, int] = {}
    for scenario in _scenarios(root):
        category = (scenario.get("taxonomy") or {}).get("category")
        if category:
            counts[category] = counts.get(category, 0) + 1
    return counts


def _category_of(scenario) -> str | None:
    return (scenario.get("taxonomy") or {}).get("category")


def _silenced_targets(scenario) -> set[str]:
    """Services this scenario takes the telemetry away from."""
    return {
        contract["target"]
        for contract in scenario["fault"]["contracts"]
        if contract["inject"]["payload"].get("metrics_enabled") is False
    }


def test_v2_is_the_size_the_evaluation_claims():
    assert len(_scenarios(V2)) == 22


@pytest.mark.parametrize("category,expected", sorted(REQUIRED_V2_CATEGORIES.items()))
def test_v2_carries_each_recovery_category_the_mix_requires(category, expected):
    assert _categories(V2).get(category, 0) >= expected


def test_the_adversarial_corpus_carries_injection_and_cross_tenant():
    payload = json.loads(ADVERSARIAL.read_text())
    cases = payload["cases"] if isinstance(payload, dict) else payload
    categories = {case["category"] for case in cases}
    missing = REQUIRED_ADVERSARIAL_CATEGORIES - categories
    assert not missing, f"adversarial corpus lost required categories: {missing}"


def test_adversarial_cases_are_not_smuggled_into_the_recovery_corpus():
    """They cannot be: a refusal case has nothing to recover.

    The strict loader demands one aggregate recovery probe per scenario.
    Satisfying it for an injection case would mean inventing a health signal,
    which is exactly what content-addressing the corpus is meant to prevent.
    """
    v2_categories = set(_categories(V2))
    assert not (v2_categories & REQUIRED_ADVERSARIAL_CATEGORIES)


def test_v3_measures_missing_data_in_every_split():
    """The gap the previous version of this test held open.

    One scenario per split, not three in whichever split was convenient: the
    category is exercised during development, in the default live split, and
    in the frozen holdout.
    """
    per_split = {}
    for split in ("train", "dev", "holdout"):
        payload = json.loads((V3 / f"{split}.json").read_text())
        per_split[split] = sum(
            1 for s in payload["scenarios"] if _category_of(s) == "missing_data"
        )
    assert per_split == {"train": 1, "dev": 1, "holdout": 1}


def test_v3_extends_v2_rather_than_replacing_it():
    """v2 stays frozen and pinned, so every published v2 result still stands."""
    v2_ids = {s["id"] for s in _scenarios(V2)}
    v3_ids = {s["id"] for s in _scenarios(V3)}
    assert v2_ids < v3_ids
    assert v3_ids - v2_ids == {
        "checkout_exporter_down_no_service_fault",
        "inventory_slow_queries_with_checkout_blind_spot",
        "payment_outage_with_checkout_telemetry_gap",
    }


def test_a_missing_data_probe_never_reads_the_service_it_silenced():
    """Otherwise the trial grades the harness instead of the agent.

    `recovery_oracle.py` fails closed on an empty query result. A missing-data
    scenario's whole point is that a service stops being scraped, so a probe
    pointed at that service returns nothing and the trial reports
    `INVALID_SCENARIO` before the agent's conclusion is graded at all.

    The probe therefore has to read a series that survives the gap, which
    forces the shape of every scenario in this category: the exporter goes
    down on one service and the fault, if any, lives on another. That is not a
    stylistic preference, and it is the kind of constraint that is invisible
    until a whole benchmark run comes back void, so it is asserted here.
    """
    for scenario in _scenarios(V3):
        if _category_of(scenario) != "missing_data":
            continue
        silenced = _silenced_targets(scenario)
        assert silenced, (
            f"{scenario['id']} is taxonomised missing_data but takes no "
            "telemetry away"
        )
        query = scenario["recovery_probe"]["query"]
        for service in silenced:
            assert service not in query, (
                f"{scenario['id']}: the recovery probe reads {service}, whose "
                "exporter this scenario disables — the oracle would fail "
                "closed and report INVALID_SCENARIO"
            )


def test_a_silenced_exporter_is_always_restored():
    """A leaked contract blinds every scenario that runs after it."""
    for scenario in _scenarios(V3):
        for contract in scenario["fault"]["contracts"]:
            if contract["inject"]["payload"].get("metrics_enabled") is not False:
                continue
            assert contract["cleanup"]["payload"]["metrics_enabled"] is True, (
                f"{scenario['id']} never puts {contract['target']}'s exporter "
                "back"
            )


def test_the_coverage_table_now_credits_missing_data_to_v3():
    """The table is the claim; it cannot lag the corpora in either direction."""
    readme = DATASETS_README.read_text()
    assert "| **missing-data** | **nowhere** | **0** |" not in readme, (
        "v3 measures missing-data — the table must stop declaring it unmeasured"
    )
    row = next(
        (line for line in readme.splitlines() if line.startswith("| missing-data ")),
        None,
    )
    assert row is not None, "the coverage table lost its missing-data row"
    assert "v3" in row and row.rstrip().endswith("| 3 |"), row
