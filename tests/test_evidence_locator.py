import json

import pytest
from benchmarks import evidence_locator
from benchmarks.evidence_locator import ToolCallRef, check_case, parse_reference

CATALOG = frozenset({"list_events", "list_pods", "list_commits", "get_metric", "get_pod_logs"})


def _transcript(*calls: tuple[str, dict], source: str = "k8s_agent") -> dict:
    return {
        "source": source,
        "turns": [
            {"role": "assistant", "text": "", "tool_calls": [{"name": n, "args": a}]}
            for n, a in calls
        ]
        + [{"role": "tool", "name": n, "status": "success", "content": "{}"} for n, _ in calls],
        "tool_failures": 0,
    }


def _case(references: list, transcripts: list, missing: int = 0) -> dict:
    return {
        "blind_case_id": "case-1",
        "review_input": {
            "summary_text": "",
            "benchmark_evaluation": {
                "evidence": [
                    {"source": "k8s", "reference": r, "claim": "c", "observed_at": None}
                    for r in references
                ]
            },
            "timeline_events": [],
            "specialist_transcripts": transcripts,
            "missing_transcripts": missing,
        },
    }


@pytest.mark.parametrize(
    "reference, expected",
    [
        (
            "list_events (involved_object=inventory-service-6d99b598bd-xwtdj, "
            "namespace=meridian, limit=50) — Warning/BackOff",
            ToolCallRef(
                "list_events",
                {
                    "involved_object": "inventory-service-6d99b598bd-xwtdj",
                    "namespace": "meridian",
                    "limit": "50",
                },
            ),
        ),
        (
            "list_pods (label_selector=app=inventory-service, namespace=meridian) — 2 pods",
            ToolCallRef("list_pods", {"label_selector": "app=inventory-service", "namespace": "meridian"}),
        ),
        (
            "list_commits (since=2026-09-21T13:50:50Z, limit=20, no path filter) → {\"commits\": []}",
            ToolCallRef("list_commits", {"since": "2026-09-21T13:50:50Z", "limit": "20"}),
        ),
        (
            'get_metric (query=sum by (service, code) (rate(http_requests_total{service="checkout",'
            'code=~"5.."}[5m])), time=now) — 0.031',
            ToolCallRef(
                "get_metric",
                {
                    "query": 'sum by (service, code) (rate(http_requests_total{service="checkout",'
                    'code=~"5.."}[5m]))',
                    "time": "now",
                },
            ),
        ),
        ("`get_pod_logs`(pod=checkout-1)", ToolCallRef("get_pod_logs", {"pod": "checkout-1"})),
    ],
)
def test_parses_the_reference_formats_agents_write(reference, expected):
    assert parse_reference(reference) == expected


@pytest.mark.parametrize(
    "reference",
    [
        "golden-signals query for inventory-service p99 (as reported)",
        'InventorySlowQueries annotations.summary — "90th percentile above 2s"',
        "list_pods (namespace=meridian",
        None,
        42,
    ],
)
def test_prose_and_malformed_references_name_no_call(reference):
    assert parse_reference(reference) is None


def test_a_matching_call_locates_the_reference_despite_extra_or_unpassed_args():
    case = _case(
        ["list_events (involved_object=pod-x, namespace=Meridian, limit=50) — Warning"],
        [_transcript(("list_events", {"involved_object": "pod-x", "namespace": "meridian", "types": "Warning"}))],
    )

    result = check_case(case, CATALOG)

    assert result.state == "PASS"
    assert [e.state for e in result.entries] == ["LOCATED"]


def test_numbers_and_instants_compare_by_value():
    case = _case(
        ["list_commits (since=2026-09-21T13:50:50Z, limit=20) → {}"],
        [_transcript(("list_commits", {"since": "2026-09-21T13:50:50+00:00", "limit": 20}))],
    )

    assert check_case(case, CATALOG).state == "PASS"


def test_server_prefixed_call_names_still_match():
    case = _case(
        ["list_pods (namespace=meridian) — 2 pods"],
        [_transcript(("k8s__list_pods", {"namespace": "meridian"}))],
    )

    assert check_case(case, CATALOG).state == "PASS"


def test_a_known_tool_that_was_never_called_fails_the_case():
    case = _case(
        [
            "list_pods (namespace=meridian) — 2 pods",
            "get_pod_logs (pod=checkout-1, tail=200) — OOMKilled",
        ],
        [_transcript(("list_pods", {"namespace": "meridian"}))],
    )

    result = check_case(case, CATALOG)

    assert result.state == "FAIL"
    assert [e.state for e in result.entries] == ["LOCATED", "UNLOCATABLE"]
    assert "evidence[1]: no recorded call to get_pod_logs" in result.detail


def test_every_call_disagreeing_on_a_named_argument_fails_the_case():
    case = _case(
        ["list_events (involved_object=pod-invented, namespace=meridian) — Warning"],
        [
            _transcript(
                ("list_events", {"involved_object": "pod-a", "namespace": "meridian"}),
                ("list_events", {"involved_object": "pod-b"}),
            )
        ],
    )

    result = check_case(case, CATALOG)

    assert result.state == "FAIL"
    assert "every list_events call (2) disagrees on involved_object=pod-invented" in result.detail


def test_one_agreeing_call_among_several_is_enough():
    case = _case(
        ["list_events (involved_object=pod-b) — Warning"],
        [
            _transcript(("list_events", {"involved_object": "pod-a"})),
            _transcript(("list_events", {"involved_object": "pod-b"}), source="metrics_agent"),
        ],
    )

    assert check_case(case, CATALOG).state == "PASS"


def test_a_name_outside_the_catalog_is_left_to_the_judge():
    case = _case(
        ["payment_provider_up (service=payment-service) = 0 for 6m"],
        [_transcript(("get_metric", {"query": "payment_provider_up"}))],
    )

    result = check_case(case, CATALOG)

    assert result.state == "UNCHECKABLE"
    assert result.entries[0].detail == "payment_provider_up is not a known tool"


def test_missing_transcripts_downgrade_a_failure_to_uncheckable():
    case = _case(
        ["get_pod_logs (pod=checkout-1) — OOMKilled", "list_pods (namespace=meridian) — ok"],
        [_transcript(("list_pods", {"namespace": "meridian"}))],
        missing=1,
    )

    result = check_case(case, CATALOG)

    assert result.state == "PASS"
    assert result.entries[0].state == "UNCHECKABLE"
    assert result.entries[0].detail.endswith("some transcripts are missing")


def test_a_case_without_transcripts_or_evidence_is_uncheckable():
    no_transcripts = _case(["list_pods (namespace=meridian) — ok"], [])
    no_evidence = _case([], [_transcript(("list_pods", {}))])

    assert check_case(no_transcripts, CATALOG).state == "UNCHECKABLE"
    assert check_case(no_evidence, CATALOG).state == "UNCHECKABLE"


def test_the_catalog_is_read_from_the_mcp_servers():
    catalog = evidence_locator.tool_catalog()

    assert {"list_events", "list_pods", "list_commits", "get_metric", "get_pod_logs"} <= catalog
    assert "payment_provider_up" not in catalog


def test_cli_writes_results_beside_the_review_set_and_leaves_it_untouched(tmp_path, capsys):
    review = tmp_path / "review.jsonl"
    cases = [
        _case(["list_pods (namespace=meridian) — ok"], [_transcript(("list_pods", {"namespace": "meridian"}))]),
        _case(["get_pod_logs (pod=x) — OOM"], [_transcript(("list_pods", {}))]),
    ]
    cases[1]["blind_case_id"] = "case-2"
    original = "".join(json.dumps(c, sort_keys=True) + "\n" for c in cases)
    review.write_text(original)
    output = tmp_path / "out" / "locatable.jsonl"

    assert evidence_locator.main([str(review), "--output", str(output)]) == 0

    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert [(r["blind_case_id"], r["locatable"]) for r in rows] == [("case-1", "PASS"), ("case-2", "FAIL")]
    assert review.read_text() == original
    printed = capsys.readouterr().out
    assert "cases: 2 PASS=1 FAIL=1 UNCHECKABLE=0" in printed
    assert "case-2: evidence[0]: no recorded call to get_pod_logs" in printed
