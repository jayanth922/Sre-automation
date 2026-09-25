"""The prose wrap-up must not contradict the diagnosis it ships beside.

`narrate_supervisor_summary` and the structured `benchmark_evaluation` leave the
supervisor in the same timeline payload, built in the same block - and the
narrator was never handed the reflector's conclusion. Across eight recorded
trials, three shipped "root cause: Unknown" over a structured diagnosis naming a
specific fault mode on a specific service with 9-12 supporting evidence entries.
The on-call engineer reads the prose.

The fix is not "always name a cause". In a fourth trial the reflector had
settled on nothing (fault_mode=None, evidence=[]) and "Unknown" was the honest
answer, so these tests pin both directions.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from sre_agent import narrative


def _link(cause, effect):
    return SimpleNamespace(cause=cause, effect=effect)


def _evidence(source, claim):
    return SimpleNamespace(source=source, claim=claim)


SETTLED = SimpleNamespace(
    hypothesis=(
        "inventory-service is serving every request through a deliberately "
        "slowed database path, driving p99 latency past the SLO"
    ),
    affected_service="inventory-service",
    fault_mode="slow_query",
    confidence=0.82,
    causal_chain=[_link("slow_query_rate at 1.0", "p99 latency 1.8s")],
    evidence=[_evidence("prometheus", "p99 latency 1.84s at 02:32:08Z")],
    unknowns=["whether the knob was set by a deploy or by hand"],
)

# The threefix trial: a hypothesis the reflector could not support. The prose
# said Unknown and was right to.
UNSUPPORTED = SimpleNamespace(
    hypothesis="possibly a downstream dependency, but nothing confirmed it",
    affected_service=None,
    fault_mode=None,
    confidence=0.2,
    causal_chain=[],
    evidence=[],
    unknowns=[],
)


# --------------------------------------------------------------------------
# What counts as a settled conclusion
# --------------------------------------------------------------------------


def test_no_reflector_means_no_conclusion():
    assert narrative._format_reflector_conclusion(None) == ""


def test_a_hypothesis_nothing_supports_is_not_a_conclusion():
    """`hypothesis` is a required field, so its presence proves nothing."""
    assert narrative._format_reflector_conclusion(UNSUPPORTED) == ""


def test_a_supported_hypothesis_is_rendered_with_its_diagnosis():
    block = narrative._format_reflector_conclusion(SETTLED)
    assert "Fault mode: slow_query" in block
    assert "Affected service: inventory-service" in block
    assert "Causal link 1: slow_query_rate at 1.0 -> p99 latency 1.8s" in block
    assert "Evidence (prometheus): p99 latency 1.84s at 02:32:08Z" in block
    assert "Still unresolved: whether the knob was set" in block


def test_a_causal_chain_alone_is_enough_support():
    analysis = SimpleNamespace(
        hypothesis="cold start after the restart",
        affected_service="inventory-service",
        fault_mode="post_restart_cold_start_latency",
        confidence=0.6,
        causal_chain=[_link("pod restarted 02:30", "cache empty, p99 spike")],
        evidence=[],
        unknowns=[],
    )
    assert "Causal link 1" in narrative._format_reflector_conclusion(analysis)


def test_dict_shaped_links_and_evidence_render_too():
    analysis = SimpleNamespace(
        hypothesis="h",
        affected_service=None,
        fault_mode=None,
        confidence=None,
        causal_chain=[{"cause": "c", "effect": "e"}],
        evidence=[{"source": "loki", "claim": "timeouts"}],
        unknowns=[],
    )
    block = narrative._format_reflector_conclusion(analysis)
    assert "Causal link 1: c -> e" in block
    assert "Evidence (loki): timeouts" in block


# --------------------------------------------------------------------------
# What the narrator is told to do about it
# --------------------------------------------------------------------------


def test_a_settled_conclusion_forbids_unknown():
    rule = narrative._reflector_root_cause_rule(True)
    assert "DO NOT WRITE 'UNKNOWN'" in rule
    assert "MUST state that hypothesis" in rule


def test_a_settled_conclusion_still_allows_disagreement():
    """Agreement is not the point; silent contradiction is."""
    rule = narrative._reflector_root_cause_rule(True)
    assert "disagree with it outright" in rule


def test_no_conclusion_keeps_unknown_required():
    rule = narrative._reflector_root_cause_rule(False)
    assert "'Unknown' is then the correct and required answer" in rule
    assert "not manufacture a cause" in rule


# --------------------------------------------------------------------------
# End to end through the narrator
# --------------------------------------------------------------------------


def _narrate(reflector_analysis):
    captured = {}

    async def _fake(llm, system, user):
        captured["system"] = system
        captured["user"] = user
        return "## TL;DR\nwritten"

    with patch.object(narrative, "_invoke_llm", new=AsyncMock(side_effect=_fake)):
        out = asyncio.run(
            narrative.narrate_supervisor_summary(
                llm=object(),
                objective="p99 latency on inventory-service",
                alert_context={"labels": {"service": "inventory-service"}},
                agent_results={"metrics_agent": "p99 is 1.84s"},
                reflector_analysis=reflector_analysis,
            )
        )
    return out, captured


def test_the_narrator_receives_the_conclusion_shipping_beside_it():
    out, captured = _narrate(SETTLED)
    assert out == "## TL;DR\nwritten"
    assert "REFLECTOR CONCLUSION" in captured["user"]
    assert "slow_query" in captured["user"]
    assert "DO NOT WRITE 'UNKNOWN'" in captured["system"]


def test_an_unsupported_hypothesis_is_not_smuggled_into_the_prompt():
    _out, captured = _narrate(UNSUPPORTED)
    assert "REFLECTOR CONCLUSION" not in captured["user"]
    assert "possibly a downstream dependency" not in captured["user"]
    assert "correct and required answer" in captured["system"]


def test_the_plain_aggregation_path_still_works_without_a_reflector():
    """supervisor.py's second call site has no reflector in scope."""
    _out, captured = _narrate(None)
    assert "REFLECTOR CONCLUSION" not in captured["user"]
    assert "correct and required answer" in captured["system"]


def test_the_conclusion_is_wrapped_as_untrusted_content():
    """It is model output derived from tool results, injection and all. The
    narrator must report it without obeying anything written inside it."""
    _out, captured = _narrate(SETTLED)
    assert "reflector_conclusion" in captured["user"]


@pytest.mark.parametrize("analysis", [SETTLED, UNSUPPORTED, None])
def test_narration_falls_back_rather_than_raising(analysis):
    with patch.object(narrative, "_invoke_llm", new=AsyncMock(return_value="")):
        out = asyncio.run(
            narrative.narrate_supervisor_summary(
                llm=object(),
                objective="the incident",
                alert_context={},
                agent_results={"metrics_agent": "p99 is 1.84s"},
                reflector_analysis=analysis,
            )
        )
    assert "## TL;DR" in out
