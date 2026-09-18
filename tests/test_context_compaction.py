#!/usr/bin/env python3
"""Unit tests for context budgeting and compaction.

Two behaviors are covered: the pre-run LLM summarizer (`compact`) and the
per-iteration deterministic fitter (`fit_to_budget` / `make_pre_model_hook`).

The module is loaded by file path on purpose — it must stay free of package
relative imports so budgeting never drags the agent runtime into a test that
only wants to count tokens.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "sre_agent" / "context_compaction.py"
_spec = importlib.util.spec_from_file_location("context_compaction", _MODULE_PATH)
cc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cc
_spec.loader.exec_module(cc)

_OVERHEAD = cc._PER_MESSAGE_OVERHEAD_TOKENS


@pytest.fixture(autouse=True)
def _deterministic_counting(monkeypatch):
    """Pin counting to the character heuristic with no safety inflation.

    Token counts are otherwise tokenizer- and version-dependent, which makes
    exact assertions meaningless. The tokenizer path gets its own tests below.
    """
    for key in list(sys.modules["os"].environ):
        if key.startswith("CONTEXT_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CONTEXT_TOKENIZER", "heuristic")
    monkeypatch.setenv("CONTEXT_TOKEN_SAFETY_RATIO", "1.0")
    cc.reset_tokenizer_cache()
    yield
    cc.reset_tokenizer_cache()


def _msg(role, content):
    return {"role": role, "content": content}


def _ai_call(call_id, name="kubectl", args=None):
    return {
        "role": "ai",
        "content": "",
        "tool_calls": [{"id": call_id, "name": name, "args": args or {"q": "pods"}}],
    }


def _tool_result(call_id, content):
    return {"role": "tool", "content": content, "tool_call_id": call_id}


# ── Token counting ──────────────────────────────────────────────────────────


def test_estimate_tokens_is_the_character_heuristic():
    """`estimate_tokens` stays the crude fallback; nothing else changed under it."""
    assert cc.estimate_tokens("a" * 40) == 10
    assert cc.estimate_tokens("") == 0
    assert cc.estimate_tokens(None) == 0


def test_message_tokens_include_per_message_framing():
    """A message costs more than its text: roles and delimiters are billed too."""
    assert cc.message_tokens(_msg("user", "a" * 40)) == 10 + _OVERHEAD
    assert cc.messages_tokens([_msg("user", "a" * 40), _msg("ai", "b" * 40)]) == (
        2 * (10 + _OVERHEAD)
    )


def test_message_tokens_count_tool_call_arguments():
    """Tool-call args live outside `content` and used to be counted as free.

    An assistant message that requests a tool call usually has empty content,
    so the old sum scored a 4k-token argument payload as zero — the single
    largest way a budget check could under-count a ReAct loop.
    """
    small = _ai_call("t1", args={"q": "pods"})
    large = _ai_call("t1", args={"q": "x" * 4000})
    assert cc.message_tokens(large) > cc.message_tokens(small) + 900


def test_message_tokens_flatten_anthropic_block_content():
    """Content arrives as a list of blocks under prompt caching; count it anyway."""
    blocks = {"role": "system", "content": [{"type": "text", "text": "a" * 400}]}
    assert cc.message_tokens(blocks) == 100 + _OVERHEAD


def test_count_tokens_falls_back_to_the_heuristic_without_a_tokenizer(monkeypatch):
    """An air-gapped cluster with no tiktoken wheel still gets a usable number."""
    monkeypatch.setenv("CONTEXT_TOKENIZER", "heuristic")
    monkeypatch.setenv("CONTEXT_TOKEN_SAFETY_RATIO", "1.15")
    cc.reset_tokenizer_cache()
    assert cc.count_tokens("a" * 40) == int(10 * 1.15)
    assert cc.count_tokens("") == 0


def test_count_tokens_applies_a_safety_ratio():
    """Counts are deliberately pessimistic: overestimating costs context, under-
    estimating costs the whole request."""
    import os

    os.environ["CONTEXT_TOKEN_SAFETY_RATIO"] = "2.0"
    try:
        assert cc.count_tokens("a" * 40) == 20
    finally:
        os.environ["CONTEXT_TOKEN_SAFETY_RATIO"] = "1.0"


def test_tokenizer_resolution_is_attempted_only_once(monkeypatch):
    """`tiktoken.get_encoding` can hit the network on first use.

    Retrying it inside a per-iteration budget check would let a hung download
    stall every model call in a cluster with no egress, so a failure must be
    remembered, not retried.
    """
    calls = {"n": 0}

    class _Boom:
        def get_encoding(self, _name):
            calls["n"] += 1
            raise RuntimeError("no network")

    monkeypatch.setenv("CONTEXT_TOKENIZER", "auto")
    monkeypatch.setitem(sys.modules, "tiktoken", _Boom())
    cc.reset_tokenizer_cache()

    for _ in range(5):
        cc.count_tokens("hello world")
    assert calls["n"] == 1


# ── Output reservation ──────────────────────────────────────────────────────


def test_hard_ceiling_reserves_room_for_the_reply(monkeypatch):
    """Input budget is window − output − margin, never the whole window.

    Sending 200k of input to a 200k model that is asked for 4096 output tokens
    is not tight, it is a 400.
    """
    monkeypatch.setenv("CONTEXT_WINDOW_TOKENS", "100000")
    monkeypatch.setenv("CONTEXT_RESERVED_OUTPUT_TOKENS", "4096")
    monkeypatch.setenv("CONTEXT_SAFETY_MARGIN_RATIO", "0.05")
    assert cc.hard_input_ceiling_tokens() == 100000 - 4096 - 5000


def test_hard_ceiling_is_clamped_when_the_reservation_swallows_the_window(monkeypatch):
    monkeypatch.setenv("CONTEXT_WINDOW_TOKENS", "8000")
    monkeypatch.setenv("CONTEXT_RESERVED_OUTPUT_TOKENS", "8000")
    assert cc.hard_input_ceiling_tokens() == cc._MIN_INPUT_CEILING_TOKENS


def test_working_budget_is_clamped_to_the_hard_ceiling(monkeypatch):
    """An operator can ask for a smaller budget, never a larger one than fits."""
    monkeypatch.setenv("CONTEXT_WINDOW_TOKENS", "20000")
    monkeypatch.setenv("CONTEXT_RESERVED_OUTPUT_TOKENS", "4096")
    monkeypatch.setenv("CONTEXT_SAFETY_MARGIN_RATIO", "0.0")
    monkeypatch.setenv("CONTEXT_MAX_TOKENS", "999999")
    assert cc.default_max_tokens() == 20000 - 4096

    monkeypatch.setenv("CONTEXT_MAX_TOKENS", "5000")
    assert cc.default_max_tokens() == 5000


def test_iteration_budget_defaults_to_the_ceiling(monkeypatch):
    """Per-iteration trimming exists to prevent a 400, not to save money.

    Defaulting it to the small working budget would silently throw away
    evidence on every call; degrading quality for cost is opt-in.
    """
    monkeypatch.setenv("CONTEXT_WINDOW_TOKENS", "20000")
    monkeypatch.setenv("CONTEXT_RESERVED_OUTPUT_TOKENS", "4000")
    monkeypatch.setenv("CONTEXT_SAFETY_MARGIN_RATIO", "0.0")
    monkeypatch.setenv("CONTEXT_MAX_TOKENS", "1200")
    assert cc.iteration_budget_tokens() == 16000

    monkeypatch.setenv("CONTEXT_ITERATION_MAX_TOKENS", "9000")
    assert cc.iteration_budget_tokens() == 9000

    monkeypatch.setenv("CONTEXT_ITERATION_MAX_TOKENS", "999999")
    assert cc.iteration_budget_tokens() == 16000


def test_junk_env_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("CONTEXT_WINDOW_TOKENS", "not-a-number")
    assert cc.context_window_tokens() == cc.DEFAULT_CONTEXT_WINDOW_TOKENS
    monkeypatch.setenv("CONTEXT_RESERVED_OUTPUT_TOKENS", "-5")
    assert cc.reserved_output_tokens() == cc.DEFAULT_RESERVED_OUTPUT_TOKENS


# ── Turn groups ─────────────────────────────────────────────────────────────


def test_turn_groups_bind_tool_results_to_their_call():
    msgs = [
        _msg("user", "investigate"),
        _ai_call("t1"),
        _tool_result("t1", "pods"),
        _tool_result("t2", "logs"),
        _msg("ai", "done"),
    ]
    assert cc.turn_groups(msgs) == [(0, 1), (1, 4), (4, 5)]


def test_compact_never_orphans_a_tool_result():
    """Slicing `messages[-keep_recent:]` could cut between a call and its result.

    Anthropic rejects a `tool_result` with no matching `tool_use` outright, so
    the old tail slice could turn an over-long run into a hard 400 instead of a
    compacted one. The cut point now moves back to a turn boundary.
    """
    msgs = [
        _msg("user", "x" * 4000),
        _ai_call("t1"),
        _tool_result("t1", "y" * 4000),
        _msg("ai", "z" * 4000),
    ]

    async def summ(_):
        return "SUMMARY"

    out, did = asyncio.run(cc.compact(msgs, summ, keep_recent=2, max_tokens=100))
    assert did is True
    # Naive slicing would have started the tail at the tool result.
    assert out[1] is msgs[1] and out[2] is msgs[2]
    assert "SUMMARY" in out[0]["content"]


def test_compact_is_a_noop_when_the_whole_history_is_one_turn():
    """Nothing can be summarized without splitting a call from its result."""
    msgs = [_ai_call("t1"), _tool_result("t1", "y" * 40000)]

    async def summ(_):
        raise AssertionError("should not summarize")

    out, did = asyncio.run(cc.compact(msgs, summ, keep_recent=1, max_tokens=10))
    assert did is False and out is msgs


# ── Deterministic fitting ───────────────────────────────────────────────────


def test_fit_is_a_noop_under_budget():
    msgs = [_msg("user", "hi"), _msg("ai", "hello")]
    out, report = cc.fit_to_budget(msgs, budget_tokens=100000)
    assert out == msgs
    assert report.changed is False and report.fitted is True


def test_fit_shrinks_old_tool_results_first():
    """Tool output is the bulk and the most compressible part of a ReAct loop."""
    msgs = [
        _msg("system", "prompt"),
        _ai_call("t1"),
        _tool_result("t1", "A" * 40000),
        _ai_call("t2"),
        _tool_result("t2", "B" * 40000),
        _msg("ai", "conclusion"),
    ]
    out, report = cc.fit_to_budget(msgs, budget_tokens=3000, floor_chars=1000)

    assert report.fitted is True
    assert report.truncated_results >= 1
    assert len(out) == len(msgs)  # nothing dropped yet
    assert out[0] is msgs[0] and out[-1] is msgs[-1]
    assert "elided" in out[2]["content"]
    # Head and tail of the payload both survive; the middle is what goes.
    assert out[2]["content"].startswith("A" * 100)
    assert out[2]["content"].endswith("A" * 100)


def test_fit_drops_whole_turn_groups_and_says_so():
    msgs = [_msg("system", "prompt")]
    for i in range(6):
        msgs += [_ai_call(f"t{i}"), _tool_result(f"t{i}", "X" * 20000)]
    msgs.append(_msg("ai", "conclusion"))

    out, report = cc.fit_to_budget(msgs, budget_tokens=1200, floor_chars=800)

    assert report.dropped_groups >= 1
    assert report.dropped_messages == 2 * report.dropped_groups
    note = out[1]
    assert note["role"] == "system" and "elided" in note["content"]
    assert "evidence artifact" in note["content"]
    # The system prompt and the newest turn are never sacrificed.
    assert out[0] is msgs[0]
    assert out[-1] is msgs[-1]


def test_fit_leaves_no_orphaned_tool_result():
    """Whatever is dropped, the surviving list must still be a valid request."""
    msgs = [_msg("system", "prompt")]
    for i in range(8):
        msgs += [_ai_call(f"t{i}"), _tool_result(f"t{i}", "X" * 30000)]
    msgs.append(_msg("user", "what is wrong?"))

    out, _ = cc.fit_to_budget(msgs, budget_tokens=900, floor_chars=400)

    open_ids = set()
    for message in out:
        call_id = cc._tool_call_id(message)
        if call_id is not None:
            assert call_id in open_ids, f"tool result {call_id} has no matching call"
        for call in cc._tool_calls(message):
            open_ids.add(call["id"])


def test_fit_truncates_the_protected_tail_as_a_last_resort():
    """A single oversized message still has to be made to fit.

    Refusing to touch a protected message here would hand the provider a
    request it is certain to reject — a truncated answer beats no answer.
    """
    msgs = [_msg("user", "Q" * 200000)]
    out, report = cc.fit_to_budget(msgs, budget_tokens=2000, floor_chars=4000)
    assert report.fitted is True
    assert len(cc._content(out[0])) < 200000


def test_fit_does_not_mutate_the_input():
    original = _tool_result("t1", "A" * 40000)
    msgs = [_msg("system", "p"), _ai_call("t1"), original, _msg("ai", "done")]
    cc.fit_to_budget(msgs, budget_tokens=500, floor_chars=200)
    assert original["content"] == "A" * 40000
    assert len(msgs) == 4


def test_fit_report_round_trips_to_a_dict():
    msgs = [_msg("system", "p"), _ai_call("t1"), _tool_result("t1", "A" * 40000)]
    _, report = cc.fit_to_budget(msgs, budget_tokens=500, floor_chars=200)
    payload = report.as_dict()
    assert payload["budget_tokens"] == 500
    assert payload["before_tokens"] > payload["after_tokens"]
    assert payload["fitted"] is True


# ── The pre-model hook ──────────────────────────────────────────────────────


def test_pre_model_hook_rewrites_only_the_model_input():
    """`llm_input_messages` leaves graph state alone.

    That is the whole reason this is safe: `tool_failures` detection and the
    stored evidence artifact keep reading the untouched full transcript.
    """
    state = {"messages": [_msg("system", "p"), _ai_call("t1"), _tool_result("t1", "A" * 40000)]}
    hook = cc.make_pre_model_hook(budget_tokens=500)
    out = hook(state)

    assert set(out) == {"llm_input_messages"}
    assert "messages" not in out
    assert state["messages"][2]["content"] == "A" * 40000
    assert len(cc._content(out["llm_input_messages"][2])) < 40000


def test_pre_model_hook_reports_only_when_it_changed_something():
    seen = []
    hook = cc.make_pre_model_hook(budget_tokens=100000, on_report=seen.append)
    hook({"messages": [_msg("user", "hi")]})
    assert seen == []

    hook = cc.make_pre_model_hook(budget_tokens=200, on_report=seen.append)
    hook({"messages": [_msg("system", "p"), _ai_call("t1"), _tool_result("t1", "A" * 40000)]})
    assert len(seen) == 1 and seen[0].truncated_results >= 1


def test_pre_model_hook_survives_a_budgeting_failure(monkeypatch):
    """A crash in the budgeter must not take down an otherwise healthy run."""
    monkeypatch.setattr(cc, "fit_to_budget", lambda *a, **k: 1 / 0)
    messages = [_msg("user", "hi")]
    out = cc.make_pre_model_hook(budget_tokens=10)({"messages": messages})
    assert out["llm_input_messages"] == messages


def test_pre_model_hook_accepts_an_object_state():
    class _State:
        messages = [_msg("user", "hi")]

    out = cc.make_pre_model_hook(budget_tokens=100000)(_State())
    assert out["llm_input_messages"] == _State.messages


# ── Pre-run compaction (unchanged behavior) ─────────────────────────────────


def test_should_compact_threshold():
    small = [_msg("user", "hi")]
    big = [_msg("user", "x" * 80000)]
    assert cc.should_compact(small, max_tokens=100) is False
    assert cc.should_compact(big, max_tokens=100) is True


def test_compact_noop_when_under_budget():
    msgs = [_msg("user", "hi"), _msg("ai", "hello")]

    async def summ(_):
        raise AssertionError("should not summarize")

    out, did = asyncio.run(cc.compact(msgs, summ, max_tokens=100000))
    assert did is False and out is msgs


def test_compact_summarizes_head_keeps_tail():
    msgs = [_msg("user", "m" * 400) for _ in range(10)]
    captured = {}

    async def summ(text):
        captured["text"] = text
        return "SUMMARY"

    out, did = asyncio.run(cc.compact(msgs, summ, keep_recent=3, max_tokens=100))
    assert did is True
    assert len(out) == 4  # 1 summary + 3 recent
    assert out[0]["role"] == "system" and "SUMMARY" in out[0]["content"]
    # head (7 messages) went to the summarizer
    assert captured["text"].count("[user]") == 7


def test_compact_noop_when_fewer_than_keep_recent():
    msgs = [_msg("user", "x" * 80000)]

    async def summ(_):
        raise AssertionError("should not summarize")

    out, did = asyncio.run(cc.compact(msgs, summ, keep_recent=6, max_tokens=1))
    assert did is False


def test_compact_state_messages():
    state = {"messages": [_msg("user", "m" * 400) for _ in range(10)]}

    async def summ(_):
        return "S"

    out, did = asyncio.run(cc.compact_state_messages(state, summ, keep_recent=2, max_tokens=100))
    assert did is True and len(out) == 3


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
