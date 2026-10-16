import copy
import json

import pytest
from benchmarks import judge_view, perturbations

KEY = b"k" * 32
PROSE = "NARRATOR-PROSE"


def _case() -> dict:
    evaluation = {
        "schema_version": 1,
        "diagnosis": {"fault_mode": "provider_outage", "service": "payment-service"},
        "causal_chain": [
            {"cause": "provider unreachable", "effect": "payment_provider_up reads 0"},
            {"cause": "payment_provider_up reads 0", "effect": "charges fail"},
            {"cause": "charges fail", "effect": "PaymentProviderDown fires"},
        ],
        "evidence": [{"source": "prometheus", "reference": "get_metric (query=up)", "claim": "0"}],
        "timeline": [{"event_type": "provider unreachable " + PROSE, "observed_at": None}],
        "uncertainty": {"confidence": 0.9, "unknowns": [PROSE]},
    }
    return {
        "blind_case_id": "case-1",
        "review_input": {
            "summary_text": PROSE,
            "benchmark_evaluation": evaluation,
            "timeline_events": [
                {"event_type": "plan", "content": PROSE, "payload": {"narrative": PROSE}},
                {"event_type": "decision", "payload": {"reasoning": PROSE}},
                {
                    "event_type": "finding",
                    "content": "Prometheus Specialist here — up is 0",
                    "payload": {
                        "agent_name": "metrics_agent",
                        "narrative": "Prometheus Specialist here — up is 0",
                        "raw_response": "up is 0 since 05:45Z",
                        "tool_failures": [],
                    },
                },
                {
                    "event_type": "summary",
                    "content": PROSE,
                    "payload": {
                        "alert_context": "PaymentProviderDown critical payment_provider_up is 0",
                        "benchmark_evaluation": copy.deepcopy(evaluation),
                        "narrative": PROSE,
                        "hypothesis": PROSE,
                        "raw_final_response": PROSE,
                    },
                },
                {"event_type": "act", "payload": {"act_report": PROSE}},
            ],
            "specialist_transcripts": [
                {
                    "source": "metrics_agent",
                    "turns": [
                        {"role": "assistant", "text": "", "tool_calls": [{"name": "get_metric", "args": {"query": "up"}}]},
                        {"role": "tool", "name": "get_metric", "status": "success", "content": "0"},
                    ],
                    "tool_failures": 0,
                }
            ],
            "missing_transcripts": 0,
            "unattached_sources": [],
        },
    }


def test_the_view_is_the_claim_and_what_it_rests_on():
    view = judge_view.judge_view(_case())

    assert view["alert"] == "PaymentProviderDown critical payment_provider_up is 0"
    assert view["diagnosis"]["fault_mode"] == "provider_outage"
    assert len(view["causal_chain"]) == 3
    assert view["findings"] == [
        {"agent_name": "metrics_agent", "response": "up is 0 since 05:45Z", "tool_failures": []}
    ]
    assert view["transcripts"][0]["turns"][1]["content"] == "0"
    assert PROSE not in json.dumps(view)


@pytest.mark.parametrize("make", [perturbations.swap_cause_effect, perturbations.drop_link])
def test_a_perturbed_copy_differs_from_its_source_only_in_the_claim(make):
    case = _case()
    copied = perturbations.perturbed_case(case, make(case, KEY), "case-2")

    view, source = judge_view.judge_view(copied), judge_view.judge_view(case)

    assert view["causal_chain"] != source["causal_chain"]
    assert judge_view.leaked_fields(view, source) == []


def test_a_copy_that_changed_a_transcript_is_caught():
    case = _case()
    copied = copy.deepcopy(case)
    copied["review_input"]["specialist_transcripts"][0]["turns"][1]["content"] = "1"

    assert judge_view.leaked_fields(
        judge_view.judge_view(copied), judge_view.judge_view(case)
    ) == ["transcripts"]


def _jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_cli_checks_each_perturbed_view_against_its_source(tmp_path, capsys):
    case = _case()
    copied = perturbations.perturbed_case(case, perturbations.swap_cause_effect(case, KEY), "case-2")
    _jsonl(tmp_path / "review.jsonl", [case])
    _jsonl(tmp_path / "perturbed.jsonl", [copied])
    _jsonl(tmp_path / "map.jsonl", [{"blind_case_id": "case-2", "source_blind_case_id": "case-1"}])
    out = tmp_path / "views" / "perturbed.jsonl"

    argv = [str(tmp_path / "perturbed.jsonl"), "--output", str(out), "--sources"]
    assert judge_view.main(argv + [str(tmp_path / "review.jsonl"), str(tmp_path / "map.jsonl")]) == 0

    [row] = [json.loads(line) for line in out.read_text().splitlines()]
    assert row["blind_case_id"] == "case-2"
    assert "only in diagnosis, causal_chain, evidence" in capsys.readouterr().out

    copied["review_input"]["unattached_sources"] = ["kubernetes_agent"]
    _jsonl(tmp_path / "perturbed.jsonl", [copied])
    with pytest.raises(SystemExit):
        judge_view.main(argv + [str(tmp_path / "review.jsonl"), str(tmp_path / "map.jsonl")])
    assert "case-2: differs from its source in unattached_sources" in capsys.readouterr().err


def test_a_case_without_a_structured_block_has_no_view():
    case = _case()
    del case["review_input"]["benchmark_evaluation"]

    with pytest.raises(judge_view.JudgeViewError, match="no benchmark_evaluation"):
        judge_view.judge_view(case)
