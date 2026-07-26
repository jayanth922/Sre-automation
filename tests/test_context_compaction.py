#!/usr/bin/env python3
"""Unit tests for context compaction (interview Q3)."""

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


def _msg(role, content):
    return {"role": role, "content": content}


def test_estimate_and_sum_tokens():
    assert cc.estimate_tokens("a" * 40) == 10
    assert cc.messages_tokens([_msg("user", "a" * 40), _msg("ai", "b" * 40)]) == 20


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
