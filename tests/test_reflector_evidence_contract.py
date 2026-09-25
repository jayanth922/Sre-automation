"""A hypothesis the reflector cannot source is one nobody can check.

The 2026-09-22 ``inventory_slow_queries`` trial returned five causal links and
an empty ``evidence`` list. That cost two structured criteria at once:
``evidence_support`` reads the list directly, and ``temporal_reasoning`` is
derived from the entries carrying ``observed_at``. The same analysis answered
``fault_mode`` as "injected_query_latency_runtime_config" -- the right
mechanism under a name the closed taxonomy does not contain -- and FAILed
``diagnosis`` for it while naming exactly the right service.

So two contracts are held here: the reflector is *offered* the vocabulary it
is graded against, and it is asked again when it hands back a hypothesis with
no sources behind it.
"""

import asyncio

import pytest

from sre_agent import graph_builder, model_router
from sre_agent.agent_state import FAULT_MODES, ReflectorAnalysis

SOURCED = [
    {
        "source": "prometheus",
        "reference": "histogram_quantile(0.90, sum by (le) (rate(...)))",
        "claim": "db p90 peaked at 1.591s",
        "observed_at": "2026-09-22T22:10:45+00:00",
    },
    {
        "source": "loki",
        "reference": '{job="inventory-service"} |= "Slow DB query"',
        "claim": "delay_seconds=2.35 on list_all_items",
        "observed_at": "2026-09-22T22:09:41+00:00",
    },
]


def _analysis(*, evidence=None, hypothesis="inventory-service queries are slow"):
    return ReflectorAnalysis(
        hypothesis=hypothesis,
        affected_service="inventory-service",
        fault_mode="slow_query",
        confidence=0.62,
        reasoning="db p90 is above the 1.0s action threshold",
        causal_chain=[
            {"cause": "injected query delay", "effect": "db p90 rose to 1.591s"}
        ],
        evidence=evidence or [],
    )


@pytest.fixture
def reflector(monkeypatch):
    """Drive _reflector_node against a scripted structured-output model."""
    answers: list = []
    prompts: list = []

    class FakeStructured:
        async def ainvoke(self, messages):
            prompts.append([str(getattr(m, "content", m)) for m in messages])
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

    class FakeLLM:
        def with_structured_output(self, schema, method=None):
            return FakeStructured()

    monkeypatch.setattr(model_router, "route_llm", lambda *a, **k: FakeLLM())
    return answers, prompts


def _run():
    return asyncio.run(
        graph_builder._reflector_node(
            {
                "agent_results": {"metrics_agent": "db p90 measured at 1.591s"},
                "alert_context": None,
                "metadata": {"llm_provider": "openai"},
                "thought_traces": {},
                "investigation_count": 0,
            }
        )
    )


def test_an_unsourced_hypothesis_is_asked_again_for_its_sources(reflector):
    answers, prompts = reflector
    answers.extend([_analysis(), _analysis(evidence=SOURCED)])

    result = _run()

    assert len(prompts) == 2, "the reflector was not asked again"
    assert any("empty `evidence` list" in message for message in prompts[1])
    assert len(result["reflector_analysis"].evidence) == 2


def test_a_sourced_analysis_is_never_asked_twice(reflector):
    answers, prompts = reflector
    answers.append(_analysis(evidence=SOURCED))

    result = _run()

    assert len(prompts) == 1
    assert len(result["reflector_analysis"].evidence) == 2


def test_a_second_empty_answer_keeps_the_first_analysis(reflector):
    """A re-ask that adds nothing must not cost the analysis already in hand."""
    answers, _ = reflector
    answers.extend(
        [
            _analysis(hypothesis="the first reading"),
            _analysis(hypothesis="an equally unsourced second reading"),
        ]
    )

    result = _run()

    assert result["reflector_analysis"].hypothesis == "the first reading"


def test_a_failed_re_ask_keeps_the_unsourced_analysis(reflector):
    answers, _ = reflector
    answers.extend([_analysis(hypothesis="the only reading"), RuntimeError("529")])

    result = _run()

    assert result["reflector_analysis"].hypothesis == "the only reading"
    assert result["next"] == "planner"


def test_the_reflection_prompt_names_the_closed_fault_mode_vocabulary(reflector):
    answers, prompts = reflector
    answers.append(_analysis(evidence=SOURCED))

    _run()

    prompt = "\n".join(prompts[0])
    for mode in FAULT_MODES:
        assert mode in prompt, mode
    # The prompt is an indented f-string, so it wraps mid-sentence; assert on
    # a fragment that survives the wrap rather than on the whole clause.
    assert "fault_mode outside that list" in prompt


def test_the_prompt_demands_sources_and_their_timestamps(reflector):
    answers, prompts = reflector
    answers.append(_analysis(evidence=SOURCED))

    _run()

    prompt = "\n".join(prompts[0])
    assert "non-empty evidence list" in prompt
    assert "observed_at" in prompt


def test_the_schema_offers_the_agent_the_whole_vocabulary():
    """The description reaches the model as the function-calling schema."""
    description = ReflectorAnalysis.model_fields["fault_mode"].description or ""

    for mode in FAULT_MODES:
        assert mode in description, mode


def test_the_vocabulary_is_a_closed_deduplicated_list():
    assert len(set(FAULT_MODES)) == len(FAULT_MODES)
    assert all(mode == mode.strip().lower() for mode in FAULT_MODES)
