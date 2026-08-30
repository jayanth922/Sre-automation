#!/usr/bin/env python3
"""Unit tests for narrator turn-by-turn memory (recent_turns)."""

import asyncio

from sre_agent import narrative


# ── _format_recent_turns_block ──────────────────────────────────────────────
def test_format_recent_turns_block_empty():
    assert narrative._format_recent_turns_block(None) == "(no prior turns in this conversation)"
    assert narrative._format_recent_turns_block([]) == "(no prior turns in this conversation)"


def test_format_recent_turns_block_labels_roles():
    turns = [
        {"role": "user", "content": "what's the error rate on checkout?"},
        {"role": "assistant", "content": "It's sitting at 3% right now."},
    ]
    block = narrative._format_recent_turns_block(turns)
    assert "User: what's the error rate on checkout?" in block
    assert "You: It's sitting at 3% right now." in block


# ── FakeLLM double ───────────────────────────────────────────────────────────
class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class FakeLLM:
    """Records every (system, user) prompt pair it's invoked with."""

    def __init__(self, reply: str = "It's still at 3%, same as before.") -> None:
        self.reply = reply
        self.calls = []

    async def ainvoke(self, messages):
        system = messages[0].content
        user = messages[1].content
        self.calls.append((system, user))
        return _FakeResponse(self.reply)


# ── memory continuity through narrate_followup_answer ───────────────────────
def test_followup_answer_prompt_includes_recent_turns():
    async def scenario():
        llm = FakeLLM()
        recent_turns = [
            {"role": "user", "content": "what's the error rate on checkout?"},
            {"role": "assistant", "content": "It's sitting at 3% right now."},
        ]
        reply = await narrative.narrate_followup_answer(
            llm,
            question="is it still that high?",
            objective="checkout error rate spike",
            alert_context={"alert": "HighErrorRate"},
            agent_results={},
            prior_summary="Checkout error rate spiked to 3%.",
            incident_status="RESOLVED",
            recent_turns=recent_turns,
        )
        return reply, llm.calls

    reply, calls = asyncio.run(scenario())
    assert reply == "It's still at 3%, same as before."
    assert len(calls) == 1
    _, user_prompt = calls[0]
    assert "what's the error rate on checkout?" in user_prompt
    assert "It's sitting at 3% right now." in user_prompt


def test_chat_greeting_prompt_includes_recent_turns():
    async def scenario():
        llm = FakeLLM(reply="Hey! Still here if you need anything on this one.")
        recent_turns = [
            {"role": "user", "content": "can you restart the pod?"},
            {"role": "assistant", "content": "Queued that for the next checkpoint."},
        ]
        reply = await narrative.narrate_chat_greeting(
            llm,
            user_message="thanks",
            objective="checkout error rate spike",
            alert_context={"alert": "HighErrorRate"},
            incident_status="INVESTIGATING",
            prior_summary="",
            recent_turns=recent_turns,
        )
        return reply, llm.calls

    reply, calls = asyncio.run(scenario())
    assert reply == "Hey! Still here if you need anything on this one."
    _, user_prompt = calls[0]
    assert "can you restart the pod?" in user_prompt
    assert "Queued that for the next checkpoint." in user_prompt


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
