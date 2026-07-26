#!/usr/bin/env python3
"""
Context compaction (interview Q3: context management).

Long, gnarly incidents produce long agent↔LLM histories. Past a token budget,
that causes context rot and cost blow-up. The standard fix (Manus/Claude) is to
**compact**: keep the most recent turns verbatim and replace the older ones with
a single running summary.

This module is the mechanism: estimate the history size, decide when to compact,
and — when over budget — summarize the older messages via an injected summarizer
(an LLM in production, a stub in tests) while preserving the recent tail. It is
message-shape-agnostic (works on LangGraph BaseMessages or plain dicts) and pure
except for the injected summarizer, so it is fully testable.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Rough chars-per-token; good enough for a budgeting heuristic.
_CHARS_PER_TOKEN = 4


def _content(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("content", ""))
    return str(getattr(message, "content", ""))


def _role(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("role") or message.get("type") or "message")
    return str(getattr(message, "type", getattr(message, "role", "message")))


def estimate_tokens(text: str) -> int:
    return len(text or "") // _CHARS_PER_TOKEN


def messages_tokens(messages: List[Any]) -> int:
    return sum(estimate_tokens(_content(m)) for m in (messages or []))


def default_max_tokens() -> int:
    return int(os.getenv("CONTEXT_MAX_TOKENS", "12000"))


def should_compact(messages: List[Any], max_tokens: Optional[int] = None) -> bool:
    return messages_tokens(messages) > (max_tokens if max_tokens is not None else default_max_tokens())


def format_history(messages: List[Any]) -> str:
    return "\n".join(f"[{_role(m)}] {_content(m)}" for m in messages)


async def compact(
    messages: List[Any],
    summarizer: Callable[[str], Awaitable[str]],
    keep_recent: Optional[int] = None,
    max_tokens: Optional[int] = None,
) -> Tuple[List[Any], bool]:
    """Compact ``messages`` if over budget.

    Keeps the last ``keep_recent`` messages verbatim and replaces everything
    before them with one summary message. Returns ``(new_messages, did_compact)``.
    When under budget, returns the input unchanged.
    """
    keep_recent = keep_recent if keep_recent is not None else int(os.getenv("CONTEXT_KEEP_RECENT", "6"))

    if not should_compact(messages, max_tokens):
        return messages, False
    if len(messages) <= keep_recent:
        return messages, False

    head = messages[:-keep_recent]
    tail = messages[-keep_recent:]
    summary_text = await summarizer(format_history(head))
    summary_message = {
        "role": "system",
        "content": f"[compacted summary of {len(head)} earlier messages]\n{summary_text}",
    }
    logger.info(f"🗜️  Context compaction: {len(head)} messages → 1 summary; kept {len(tail)} recent")
    return [summary_message, *tail], True


def make_llm_summarizer(llm: Any) -> Callable[[str], Awaitable[str]]:
    """Build an async summarizer backed by an LLM (routed to the fast tier upstream)."""

    async def _summarize(history_text: str) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        resp = await llm.ainvoke([
            SystemMessage(content=(
                "You compact SRE investigation history. Summarize the following "
                "into a compact running summary that preserves alert details, "
                "findings, hypotheses, decisions, and any actions taken. Be terse."
            )),
            HumanMessage(content=history_text),
        ])
        return str(getattr(resp, "content", resp))

    return _summarize


async def compact_state_messages(
    state: Any, summarizer: Callable[[str], Awaitable[str]], **kwargs
) -> Tuple[List[Any], bool]:
    """Convenience: compact the ``messages`` list on a graph state dict."""
    messages = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
    return await compact(messages, summarizer, **kwargs)
