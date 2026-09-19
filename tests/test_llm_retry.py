#!/usr/bin/env python3
"""A transient provider overload must not end an investigation.

On 2026-09-19 three consecutive background investigations died inside four
minutes, each on one Anthropic 529 (``overloaded_error``) raised mid-loop.
Neither LLM backend configured a retry, so the exception unwound to the
catch-all at ``agent_runtime.py:2069``, which logged "SaaS Background execution
failed" and abandoned the incident with the alert open and the fault still
live.

The failure is quiet and it is not rare: a complete investigation makes ~110
model calls over 21-49 minutes and needs every one of them to land. These tests
pin the retry budget, pin that *both* backends actually receive it — the two
paths reach the provider by completely different machinery, and a policy wired
into only one of them is the same outage with a smaller blast radius — and pin
the one external assumption the whole fix rests on.
"""

from __future__ import annotations

import pytest

from sre_agent.llm_retry import DEFAULT_LLM_MAX_RETRIES, llm_max_retries


@pytest.fixture(autouse=True)
def _no_inherited_setting(monkeypatch):
    monkeypatch.delenv("LLM_MAX_RETRIES", raising=False)


# --- the policy itself --------------------------------------------------------


def test_retries_are_on_by_default():
    """The defect was a missing default, not a wrong one. Nobody sets this env
    var in production, so an opt-in retry would have changed nothing."""
    assert llm_max_retries() == 5
    assert DEFAULT_LLM_MAX_RETRIES == 5


def test_the_budget_is_configurable(monkeypatch):
    monkeypatch.setenv("LLM_MAX_RETRIES", "9")
    assert llm_max_retries() == 9


def test_zero_is_honoured_not_treated_as_unset(monkeypatch):
    """Fail-fast stays reachable on purpose. Coercing 0 up to the default would
    make the old behaviour unavailable to anyone who wants it deliberately."""
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    assert llm_max_retries() == 0


def test_a_typo_does_not_stop_an_investigation_starting(monkeypatch):
    """This is read on the path that builds the model. Raising here would turn
    a misspelled env var into the thing that prevents any incident being
    worked at all — strictly worse than the bug being fixed."""
    monkeypatch.setenv("LLM_MAX_RETRIES", "five")
    assert llm_max_retries() == DEFAULT_LLM_MAX_RETRIES


def test_a_negative_budget_clamps_to_zero(monkeypatch):
    """-1 must not reach a retry loop that reads it as 'unbounded'."""
    monkeypatch.setenv("LLM_MAX_RETRIES", "-3")
    assert llm_max_retries() == 0


# --- both backends must actually receive it -----------------------------------


def test_the_litellm_backend_forwards_the_budget_to_the_completion_call():
    """ChatLiteLLM has no retry field, so the budget rides in ``model_kwargs``.

    Asserting on ``_default_params`` rather than on our own kwargs dict is the
    point: that property is what ChatLiteLLM actually spreads into
    ``litellm.acompletion``. A setting we store but never forward looks
    identical from the outside to the bug this fixes.
    """
    from sre_agent.litellm_backend import build_litellm_llm

    llm = build_litellm_llm("anthropic/claude-haiku-4-5-20251001", api_key="test-key")
    assert llm._default_params["num_retries"] == 5


def test_the_anthropic_backend_overrides_the_thin_library_default():
    """ChatAnthropic defaults to 2, which is reasonable for a library and too
    thin here. Pinning it catches a silent regression to the default."""
    from sre_agent.llm_utils import _create_anthropic_llm

    llm = _create_anthropic_llm(
        {"model_id": "claude-haiku-4-5-20251001", "api_key": "test-key"}
    )
    assert llm.max_retries == 5


def test_both_backends_agree(monkeypatch):
    """The two paths must not drift. Same env, same budget, either transport."""
    monkeypatch.setenv("LLM_MAX_RETRIES", "7")
    from sre_agent.litellm_backend import build_litellm_llm
    from sre_agent.llm_utils import _create_anthropic_llm

    via_litellm = build_litellm_llm(
        "anthropic/claude-haiku-4-5-20251001", api_key="test-key"
    )
    via_anthropic = _create_anthropic_llm(
        {"model_id": "claude-haiku-4-5-20251001", "api_key": "test-key"}
    )
    assert via_litellm._default_params["num_retries"] == 7
    assert via_anthropic.max_retries == 7


# --- the assumption the fix rests on ------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 503, 529])
def test_the_statuses_we_are_buying_retries_for_are_retryable(status):
    """A budget only helps for statuses LiteLLM agrees to retry.

    529 is the one that actually cost us three investigations, and it is the
    least obvious of the four — it is not in the classic 5xx set and an upgrade
    could plausibly drop it. If that happens the budget keeps being configured,
    keeps being forwarded, and silently stops working; this test is the only
    thing that would say so.
    """
    from litellm.utils import _should_retry

    assert _should_retry(status) is True
