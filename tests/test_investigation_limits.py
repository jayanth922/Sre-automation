from types import SimpleNamespace

from sre_agent.agent_nodes import SpecialistTurnBudget
from sre_agent.investigation_limits import investigation_limits


def _step(*, tool_call: bool):
    return {
        "messages": [
            SimpleNamespace(tool_calls=[{"name": "query"}] if tool_call else [])
        ]
    }


def test_defaults_bound_the_expensive_loops(monkeypatch):
    for name in (
        "SPECIALIST_MAX_MODEL_TURNS",
        "SPECIALIST_TIMEOUT_SECONDS",
        "SPECIALIST_MAX_OUTPUT_TOKENS",
        "MAX_INVESTIGATION_DEPTH",
    ):
        monkeypatch.delenv(name, raising=False)

    limits = investigation_limits()

    assert limits.specialist_model_turns == 6
    assert limits.specialist_timeout_seconds == 120
    # 4096, not a round 3000: context_compaction already reserves exactly
    # this many output tokens out of every input budget on this path, and
    # a ceiling below the reservation only makes the reservation unusable.
    assert limits.specialist_max_output_tokens == 4096
    assert limits.reinvestigation_rounds == 1


def test_operator_values_are_clamped_and_invalid_values_fail_to_defaults(monkeypatch):
    monkeypatch.setenv("SPECIALIST_MAX_MODEL_TURNS", "999")
    monkeypatch.setenv("SPECIALIST_TIMEOUT_SECONDS", "not-a-number")
    monkeypatch.setenv("SPECIALIST_MAX_OUTPUT_TOKENS", "1")
    monkeypatch.setenv("MAX_INVESTIGATION_DEPTH", "-4")

    limits = investigation_limits()

    assert limits.specialist_model_turns == 20
    assert limits.specialist_timeout_seconds == 120
    assert limits.specialist_max_output_tokens == 256
    assert limits.reinvestigation_rounds == 0


def test_specialist_stops_only_when_the_last_allowed_turn_requests_more_work():
    budget = SpecialistTurnBudget(limit=2)

    assert budget.observe(_step(tool_call=True)) is False
    assert budget.observe(_step(tool_call=True)) is True
    assert budget.turns == 2
    assert budget.exhausted is True


def test_a_final_answer_on_the_limit_is_not_called_exhausted():
    budget = SpecialistTurnBudget(limit=2)

    assert budget.observe(_step(tool_call=True)) is False
    assert budget.observe(_step(tool_call=False)) is False
    assert budget.turns == 2
    assert budget.exhausted is False
