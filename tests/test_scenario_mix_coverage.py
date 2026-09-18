"""The evaluation mix is accounted for across corpora, including its hole.

"22 scenarios" reads as a complete evaluation. It is not one: the mix the
evaluation design asks for spans three corpora, and one required category is
not measured anywhere. This test is the accounting, so the claim and the
corpora cannot drift apart — and so the missing-data gap cannot be closed in
prose without being closed in a dataset.

See the coverage table in `benchmarks/datasets/README.md`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
V2 = REPO / "benchmarks" / "datasets" / "v2"
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


def _v2_scenarios():
    scenarios = []
    for split in ("train", "dev", "holdout"):
        payload = json.loads((V2 / f"{split}.json").read_text())
        scenarios.extend(payload["scenarios"])
    return scenarios


def _v2_categories():
    counts: dict[str, int] = {}
    for scenario in _v2_scenarios():
        category = (scenario.get("taxonomy") or {}).get("category")
        if category:
            counts[category] = counts.get(category, 0) + 1
    return counts


def test_v2_is_the_size_the_evaluation_claims():
    assert len(_v2_scenarios()) == 22


@pytest.mark.parametrize("category,expected", sorted(REQUIRED_V2_CATEGORIES.items()))
def test_v2_carries_each_recovery_category_the_mix_requires(category, expected):
    assert _v2_categories().get(category, 0) >= expected


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
    v2_categories = set(_v2_categories())
    assert not (v2_categories & REQUIRED_ADVERSARIAL_CATEGORIES)


def test_missing_data_is_still_uncovered_and_still_declared():
    """A deliberate failing-honestly test, not a bug.

    No corpus measures what the agent concludes from absent telemetry. When a
    v3 adds that scenario, this test fails and is replaced by a positive
    assertion — which is the point: the gap cannot be forgotten, and closing
    it in the README alone will not make the suite green.
    """
    assert "missing_data" not in _v2_categories()

    readme = DATASETS_README.read_text()
    assert "| **missing-data** | **nowhere** | **0** |" in readme, (
        "the coverage table must keep declaring missing-data as unmeasured "
        "until a dataset actually measures it"
    )
