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


# --- the effect, not the knob -------------------------------------------------
# Everything above pins that the budget is computed and handed over. None of it
# sends a 529. The whole fix rests on a chain -- our env, our builder, the
# Anthropic SDK, its retry predicate, its backoff -- and a break anywhere in it
# looks exactly like the fix working. `x-should-retry`, a provider-side header
# the SDK obeys before anything else, could switch the whole policy off from
# outside without changing a line here.
#
# This is the "live fire" the backlog asks for, minus the waiting: a 529 cannot
# be summoned on demand, so watching for one during a paid campaign is hoping,
# not proving.

OVERLOADED = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
COMPLETION = {
    "id": "msg_01",
    "type": "message",
    "role": "assistant",
    "model": "claude-opus-5",
    "content": [{"type": "text", "text": "the database connection pool is saturated"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 12, "output_tokens": 7},
}


@pytest.fixture
def anthropic_transport(monkeypatch):
    """Answer the Anthropic SDK's HTTP calls from a scripted list of statuses.

    Reached through `_base_client.httpx` rather than our own `import httpx`:
    anthropic 1.6.0 vendors its HTTP stack as `httpx2`, so patching the httpx
    *we* import stubs a module the SDK never touches and the call goes to the
    real API. Binding to the SDK's own module also means a future unvendoring
    needs no change here.

    `handle_request` is the transport floor, below the retry loop, so the
    builder, the client construction, the retry predicate and the backoff are
    all the production ones. Sleep is stubbed because the assertion is on the
    attempt count, not the wall clock.
    """
    import time

    pytest.importorskip("anthropic")
    from anthropic import _base_client

    # Vendored as `httpx2` in anthropic 1.6.0; plain `httpx` if it is ever
    # unvendored. Either way this binds to the module the SDK is really using.
    sdk_httpx = getattr(_base_client, "httpx2", None) or _base_client.httpx

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)

    def install(statuses):
        sent = []

        def fake_handle_request(_self, request):
            status = statuses[min(len(sent), len(statuses) - 1)]
            sent.append(status)
            body = COMPLETION if status == 200 else OVERLOADED
            return sdk_httpx.Response(status, json=body, request=request)

        monkeypatch.setattr(
            sdk_httpx.HTTPTransport, "handle_request", fake_handle_request
        )
        return sent

    return install


def _anthropic_llm():
    from sre_agent.llm_utils import _create_anthropic_llm

    return _create_anthropic_llm(
        {"model_id": "claude-opus-5", "max_tokens": 64}
    )


def test_a_529_is_actually_retried_and_the_investigation_survives(
    anthropic_transport,
):
    """Two overloads then a success: the call returns, and the investigation
    that would have died at minute 40 does not."""
    pytest.importorskip("langchain_anthropic")
    sent = anthropic_transport([529, 529, 200])

    answer = _anthropic_llm().invoke("what is wrong with inventory-service?")

    assert answer.content == "the database connection pool is saturated"
    assert sent == [529, 529, 200]


def test_the_budget_is_spent_and_then_the_call_gives_up(anthropic_transport):
    """A retry budget that never stops is an outage that never surfaces. Five
    retries means six attempts, and then a real error the caller can see."""
    pytest.importorskip("langchain_anthropic")
    import anthropic as anthropic_sdk

    sent = anthropic_transport([529])

    with pytest.raises(anthropic_sdk.APIStatusError):
        _anthropic_llm().invoke("what is wrong?")

    assert len(sent) == DEFAULT_LLM_MAX_RETRIES + 1


def test_a_zero_budget_really_does_fail_on_the_first_529(
    anthropic_transport, monkeypatch
):
    """The fail-fast escape hatch is only real if it reaches the transport."""
    pytest.importorskip("langchain_anthropic")
    import anthropic as anthropic_sdk

    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    sent = anthropic_transport([529])

    with pytest.raises(anthropic_sdk.APIStatusError):
        _anthropic_llm().invoke("what is wrong?")

    assert sent == [529]


def test_a_bad_request_is_not_retried(anthropic_transport):
    """The budget must not turn a permanent failure into six of them. A 400
    costs the same money every time and will never come back different."""
    pytest.importorskip("langchain_anthropic")
    import anthropic as anthropic_sdk

    sent = anthropic_transport([400])

    with pytest.raises(anthropic_sdk.APIStatusError):
        _anthropic_llm().invoke("what is wrong?")

    assert sent == [400]
