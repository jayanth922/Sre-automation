import copy
import hashlib
import json
import re
import stat

import pytest
from benchmarks import perturbations
from benchmarks.evidence_locator import check_case

KEY = b"k" * 32
CATALOG = frozenset({"list_events", "list_pods", "get_metric", "list_commits"})


def _link(cause: str, effect: str) -> dict:
    return {"cause": cause, "effect": effect}


def _transcript(*calls: tuple[str, dict, str]) -> dict:
    turns = []
    for name, args, content in calls:
        turns.append({"role": "assistant", "text": "", "tool_calls": [{"name": name, "args": args}]})
        turns.append({"role": "tool", "name": name, "status": "success", "content": content})
    return {"source": "agent", "turns": turns, "tool_failures": []}


def _case(blind_id: str, chain: list, evidence: list, transcripts: list, missing: int = 0) -> dict:
    evaluation = {
        "schema_version": 1,
        "diagnosis": {"service": "payment-service", "fault_mode": "dependency"},
        "causal_chain": chain,
        "evidence": evidence,
    }
    return {
        "schema_version": 1,
        "rubric_version": "sre-structured-v1",
        "blind_case_id": blind_id,
        "review_input": {
            "summary_text": "Root cause: provider outage -> payment errors.",
            "benchmark_evaluation": copy.deepcopy(evaluation),
            "timeline_events": [
                {"event_type": "finding", "content": "metrics finding", "payload": {}},
                {
                    "event_type": "summary",
                    "content": "earlier draft",
                    "payload": {"benchmark_evaluation": {"causal_chain": [], "evidence": []}},
                },
                {
                    "event_type": "summary",
                    "content": "provider outage -> payment errors",
                    "payload": {
                        "narrative": "provider outage caused 3.19% errors",
                        "benchmark_evaluation": copy.deepcopy(evaluation),
                    },
                },
            ],
            "specialist_transcripts": transcripts,
            "missing_transcripts": missing,
        },
    }


CHAIN = [
    _link("payment provider outage", "provider calls time out"),
    _link("provider calls time out", "charge requests fail"),
    _link("charge requests fail", "checkout 5xx ratio rises"),
]


def _payment(blind_id: str = "case-pay", missing: int = 0, transcripts=None) -> dict:
    return _case(
        blind_id,
        copy.deepcopy(CHAIN),
        [
            {
                "source": "prometheus",
                "reference": "get_metric (query=payment_provider_up) — 0",
                "claim": "payment_provider_up was 0 for 6m while 5xx ratio peaked at 0.0319",
            },
            {
                "source": "kubernetes",
                "reference": "list_pods (namespace=meridian) — 3 pods",
                "claim": "all 3 payment pods Running since 2026-09-21",
            },
        ],
        transcripts
        if transcripts is not None
        else [
            _transcript(
                ("get_metric", {"query": "payment_provider_up"}, '{"value": 0, "ratio": 0.0319}'),
                ("list_pods", {"namespace": "meridian"}, '{"pods": 3, "restarts": 0}'),
            )
        ],
        missing,
    )


def _inventory(blind_id: str = "case-inv") -> dict:
    return _case(
        blind_id,
        [_link("slow inventory queries", "inventory p99 rises")],
        [
            {
                "source": "kubernetes",
                "reference": "list_events (involved_object=inventory-6d99b598bd-xwtdj, namespace=meridian) — BackOff",
                "claim": "inventory pod logged 14 BackOff events",
            }
        ],
        [_transcript(("list_events", {"involved_object": "inventory-6d99b598bd-xwtdj"}, '{"count": 14}'))],
    )


MAPPING = [
    {"blind_case_id": "case-pay", "scenario": "payment_provider_outage"},
    {"blind_case_id": "case-pay2", "scenario": "payment_provider_outage"},
    {"blind_case_id": "case-inv", "scenario": "inventory_slow_queries"},
]
SCENARIOS = {row["blind_case_id"]: row["scenario"] for row in MAPPING}


def test_number_pattern_skips_identifiers_dates_versions_and_status_classes():
    text = "p99 5xx 250ms 3.19% v1.2.3 limit=50 at 2026-09-21T13:50 pod-6d99b598bd 1Gi 14 events."

    assert [m.group(1) for m in perturbations._NUMBER.finditer(text)] == ["250", "3.19", "1", "14"]


def test_swap_reverses_one_link_and_keeps_the_rest():
    result = perturbations.swap_cause_effect(_payment(), KEY)

    i = result.detail["link"]
    assert result.evaluation["causal_chain"][i] == _link(CHAIN[i]["effect"], CHAIN[i]["cause"])
    assert [link for j, link in enumerate(result.evaluation["causal_chain"]) if j != i] == [
        link for j, link in enumerate(CHAIN) if j != i
    ]


def test_swap_needs_a_link_whose_sides_differ():
    case = _case("case-x", [_link("pool exhausted", " Pool  exhausted ")], [], [])

    assert perturbations.swap_cause_effect(case, KEY) == "no link with distinct cause and effect"


def test_drop_link_removes_an_interior_link_and_leaves_a_gap():
    result = perturbations.drop_link(_payment(), KEY)

    assert result.detail == {"link": 1, "dropped": CHAIN[1]}
    assert result.evaluation["causal_chain"] == [CHAIN[0], CHAIN[2]]


@pytest.mark.parametrize(
    "chain, reason",
    [
        (CHAIN[:2], "chain has fewer than 3 links"),
        (
            [_link("a", "b"), _link("b", "c"), _link("b", "d")],
            "every interior link's neighbours already join",
        ),
    ],
)
def test_drop_link_only_when_the_gap_is_real(chain, reason):
    assert perturbations.drop_link(_case("case-x", chain, [], []), KEY) == reason


def test_change_number_alters_a_value_the_tool_returned():
    result = perturbations.change_number(_payment(), KEY)

    assert result.detail["evidence"] == 0
    assert result.detail["from"] == "0.0319"
    assert result.detail["to"] == "0.0957"
    assert result.evaluation["evidence"][0]["claim"] == (
        "payment_provider_up was 0 for 6m while 5xx ratio peaked at 0.0957"
    )
    assert result.evaluation["evidence"][1] == _payment()["review_input"]["benchmark_evaluation"]["evidence"][1]


def test_change_number_ignores_numbers_no_return_states():
    case = _payment(transcripts=[_transcript(("get_metric", {}, '{"status": "ok"}'))])

    assert perturbations.change_number(case, KEY) == "no claimed number appears in a tool return"


def test_change_number_avoids_a_replacement_the_returns_also_state():
    case = _payment(transcripts=[_transcript(("get_metric", {}, '{"ratio": 0.0319, "max": 0.0957}'))])

    assert perturbations.change_number(case, KEY).detail["to"] == "0.1595"


def test_invent_reference_cites_another_scenarios_call_this_run_never_made():
    payment, inventory = _payment(), _inventory()

    result = perturbations.invent_reference(payment, KEY, [payment, inventory], SCENARIOS, CATALOG)

    i = result.detail["evidence"]
    assert result.evaluation["evidence"][i]["reference"] == inventory["review_input"][
        "benchmark_evaluation"
    ]["evidence"][0]["reference"]
    assert result.evaluation["evidence"][i]["claim"] == payment["review_input"][
        "benchmark_evaluation"
    ]["evidence"][i]["claim"]
    assert result.detail["donor"] == "case-inv"
    perturbed = perturbations.perturbed_case(payment, result, "case-new")
    assert check_case(perturbed, CATALOG).state == "FAIL"


def test_invent_reference_needs_complete_transcripts_and_another_scenario():
    same_scenario = _payment("case-pay2")
    same_scenario["review_input"]["benchmark_evaluation"] = _inventory()["review_input"]["benchmark_evaluation"]

    assert perturbations.invent_reference(
        _payment(missing=1), KEY, [_inventory()], SCENARIOS, CATALOG
    ) == "transcripts missing, so no call is provably absent"
    assert perturbations.invent_reference(
        _payment(), KEY, [same_scenario], SCENARIOS, CATALOG
    ) == "no other scenario's reference is unlocatable here"


def test_lift_evidence_replaces_an_entry_with_another_scenarios():
    payment, inventory = _payment(), _inventory()

    result = perturbations.lift_evidence(payment, KEY, [payment, inventory], SCENARIOS, CATALOG)

    i = result.detail["evidence"]
    assert result.evaluation["evidence"][i] == inventory["review_input"]["benchmark_evaluation"]["evidence"][0]
    assert result.detail["donor_reference_state"] == "UNLOCATABLE"
    assert len(result.evaluation["evidence"]) == 2


def test_lift_evidence_needs_transcripts_to_disprove_the_claim():
    assert perturbations.lift_evidence(
        _payment(transcripts=[]), KEY, [_inventory()], SCENARIOS, CATALOG
    ) == "no transcripts attached, so the lifted claim cannot be disproved"


def test_the_block_is_replaced_wherever_the_run_copied_it_and_prose_is_untouched():
    source = _payment()
    before = copy.deepcopy(source)
    result = perturbations.swap_cause_effect(source, KEY)

    out = perturbations.perturbed_case(source, result, "case-new")

    events = out["review_input"]["timeline_events"]
    assert out["blind_case_id"] == "case-new"
    assert out["review_input"]["benchmark_evaluation"] == result.evaluation
    assert events[2]["payload"]["benchmark_evaluation"] == result.evaluation
    assert events[1] == before["review_input"]["timeline_events"][1]
    assert out["review_input"]["summary_text"] == before["review_input"]["summary_text"]
    assert events[2]["payload"]["narrative"] == before["review_input"]["timeline_events"][2]["payload"]["narrative"]
    assert source == before


def _cases():
    return [_payment(), _inventory()]


def test_set_is_reproducible_blind_and_labelled():
    first = perturbations.build_perturbed_set(_cases(), MAPPING, blind_key=KEY, catalog=CATALOG)
    second = perturbations.build_perturbed_set(_cases(), MAPPING, blind_key=KEY, catalog=CATALOG)
    other_key = perturbations.build_perturbed_set(_cases(), MAPPING, blind_key=b"z" * 32, catalog=CATALOG)

    ids = [case["blind_case_id"] for case in first.review_cases]
    assert first == second
    assert ids == sorted(ids)
    assert all(re.fullmatch(r"case-[0-9a-f]{24}", i) for i in ids)
    assert not set(ids) & {"case-pay", "case-inv"}
    assert not set(ids) & {c["blind_case_id"] for c in other_key.review_cases}
    assert [row["blind_case_id"] for row in first.private_mapping] == ids
    assert {row["label"] for row in first.private_mapping} == {"FAIL"}
    assert {(row["source_blind_case_id"], row["kind"]) for row in first.private_mapping} == {
        ("case-pay", "swap_cause_effect"),
        ("case-pay", "drop_link"),
        ("case-pay", "change_number"),
        ("case-pay", "invent_reference"),
        ("case-pay", "lift_evidence"),
        ("case-inv", "swap_cause_effect"),
        ("case-inv", "change_number"),
        ("case-inv", "invent_reference"),
        ("case-inv", "lift_evidence"),
    }
    assert first.skipped == {"drop_link": {"chain has fewer than 3 links": 1}}
    for row in first.private_mapping:
        assert row["criterion"] == perturbations.KINDS[row["kind"]]


def test_per_kind_caps_each_kind():
    capped = perturbations.build_perturbed_set(
        _cases(), MAPPING, blind_key=KEY, catalog=CATALOG, per_kind=1
    )

    kinds = [row["kind"] for row in capped.private_mapping]
    assert sorted(kinds) == sorted(perturbations.KINDS)


def test_a_case_missing_from_the_mapping_stops_the_build():
    with pytest.raises(perturbations.PerturbationError, match="1 cases are not in the private mapping"):
        perturbations.build_perturbed_set(_cases(), MAPPING[:2], blind_key=KEY, catalog=CATALOG)


def test_cli_writes_a_private_mapping_and_a_manifest_that_addresses_both(tmp_path, capsys):
    review = tmp_path / "review.jsonl"
    mapping = tmp_path / "private-map.jsonl"
    key = tmp_path / "blind.key"
    review.write_text("".join(json.dumps(c) + "\n" for c in _cases()))
    mapping.write_text("".join(json.dumps(r) + "\n" for r in MAPPING))
    key.write_bytes(KEY)
    out = tmp_path / "perturbed"

    assert perturbations.main(
        [
            str(review),
            "--private-mapping", str(mapping),
            "--blind-key-file", str(key),
            "--review-output", str(out / "review.jsonl"),
            "--private-mapping-output", str(out / "private-map.jsonl"),
            "--manifest-output", str(out / "manifest.json"),
        ]
    ) == 0

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["cases"] == 9
    assert manifest["kinds"]["drop_link"] == 1
    assert manifest["review_sha256"] == hashlib.sha256((out / "review.jsonl").read_bytes()).hexdigest()
    assert manifest["private_mapping_sha256"] == hashlib.sha256(
        (out / "private-map.jsonl").read_bytes()
    ).hexdigest()
    assert manifest["source_review_sha256"] == hashlib.sha256(review.read_bytes()).hexdigest()
    assert stat.S_IMODE((out / "private-map.jsonl").stat().st_mode) == 0o600
    assert "perturbed cases: 9" in capsys.readouterr().out
