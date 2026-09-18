#!/usr/bin/env python3
"""
Context budgeting and compaction.

Long, gnarly incidents produce long agent↔LLM histories. Past a token budget
that causes context rot, cost blow-up, and — at the hard edge — a provider 400
that kills the run outright. There are two different budgets here and they are
not interchangeable:

* **The hard input ceiling.** ``context window − reserved output − margin``.
  Exceeding it is not "expensive", it is a guaranteed API error, because the
  model needs room to *answer*. Reserving that room is the point: a 200k window
  with 4096 tokens of requested output has ~196k of input, not 200k. This is
  what :func:`fit_to_budget` and the per-iteration hook enforce.
* **The working budget** (``CONTEXT_MAX_TOKENS``). An operator's cost/quality
  preference, always clamped to the ceiling. This is what the pre-run
  LLM-summarizer path (:func:`compact`) uses.

Two mechanisms, matched to where growth actually happens:

* :func:`compact` — pre-run, LLM-backed. Replaces the older messages with one
  running summary and keeps the recent tail verbatim. Good for the initial
  state of a resumed or follow-up investigation, where history is prose.
* :func:`fit_to_budget` / :func:`make_pre_model_hook` — per-iteration,
  deterministic, no LLM call. Inside a specialist's ReAct loop the bulk is not
  prose, it is tool output: one ``kubectl get -o json`` can be tens of
  thousands of tokens, and the loop re-sends the whole transcript on every
  iteration. Shrinking the oldest tool payloads is both cheaper and more
  faithful than asking a model to summarize a JSON blob mid-loop.

Truncating what the *model* sees does not lose evidence. The specialist's full
transcript is captured separately (``evidence_artifacts.encode_specialist_trace``)
and LangGraph's ``llm_input_messages`` hook rewrites only the model input, never
graph state, so tool-failure detection and the stored trace still see every byte.

Token counts are estimates, deliberately conservative. Anthropic's tokenizer is
not available locally, so this counts with ``tiktoken`` when it is importable
and falls back to a character heuristic otherwise, then multiplies by a safety
ratio. An overestimate costs a little context; an underestimate costs the run.

Message-shape-agnostic (LangGraph ``BaseMessage`` or plain dicts) and
dependency-free apart from the optional tokenizer, so it stays fully testable.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Rough chars-per-token, used only when no tokenizer is importable.
_CHARS_PER_TOKEN = 4

# Claude's published family minimum. Declared, not discovered: the provider
# exposes no window field on the client, and model ids turn over faster than a
# hardcoded table would stay true, so operators override this per deployment
# rather than trusting a number this file guessed.
DEFAULT_CONTEXT_WINDOW_TOKENS = 200_000

# Mirrors `llm_utils._create_anthropic_llm`'s `max_tokens=4096`. If that moves,
# or a caller passes its own max_tokens, set CONTEXT_RESERVED_OUTPUT_TOKENS to
# match — under-reserving is what produces a 400 on the longest, most valuable
# investigations, which are exactly the ones you do not want to lose.
DEFAULT_RESERVED_OUTPUT_TOKENS = 4096

# Absorbs tokenizer mismatch on the *structural* side (message framing, tool
# schemas the provider adds, cache-control blocks) that this module cannot see.
DEFAULT_SAFETY_MARGIN_RATIO = 0.05

# Absorbs tokenizer mismatch on the *text* side: cl100k_base is not Claude's
# BPE, and the character fallback is cruder still.
DEFAULT_TOKEN_SAFETY_RATIO = 1.15

# Per-message framing (role, delimiters) the provider charges for and the raw
# text count misses.
_PER_MESSAGE_OVERHEAD_TOKENS = 8

# A tool result shrunk below this is no longer evidence, just noise; past this
# point the fitter drops whole turns instead of shaving further.
DEFAULT_TOOL_RESULT_FLOOR_CHARS = 1500

# Never hand back a ceiling so small the request is pointless.
_MIN_INPUT_CEILING_TOKENS = 1000

_ELISION_ROLE = "system"


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------


def _int_env(name: str, default: int) -> int:
    """Read a positive int from the environment, ignoring junk values."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a number; using %d", name, raw, default)
        return default
    if value <= 0:
        logger.warning("%s=%r must be positive; using %d", name, raw, default)
        return default
    return value


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    return value if value >= 0 else default


def context_window_tokens() -> int:
    """Total context window of the model in use, as declared by the operator."""
    return _int_env("CONTEXT_WINDOW_TOKENS", DEFAULT_CONTEXT_WINDOW_TOKENS)


def reserved_output_tokens() -> int:
    """Tokens held back for the model's reply. Never spend these on input."""
    return _int_env("CONTEXT_RESERVED_OUTPUT_TOKENS", DEFAULT_RESERVED_OUTPUT_TOKENS)


def safety_margin_ratio() -> float:
    return _float_env("CONTEXT_SAFETY_MARGIN_RATIO", DEFAULT_SAFETY_MARGIN_RATIO)


def hard_input_ceiling_tokens() -> int:
    """The most input this deployment may send: window − output − margin.

    This is a structural limit, not a preference. A request above it fails at
    the provider no matter how valuable its contents are.
    """
    window = context_window_tokens()
    ceiling = window - reserved_output_tokens() - int(window * safety_margin_ratio())
    if ceiling < _MIN_INPUT_CEILING_TOKENS:
        logger.warning(
            "context window %d leaves only %d input tokens after reserving %d for "
            "output; clamping to %d",
            window,
            ceiling,
            reserved_output_tokens(),
            _MIN_INPUT_CEILING_TOKENS,
        )
        return _MIN_INPUT_CEILING_TOKENS
    return ceiling


def default_max_tokens() -> int:
    """Working budget for the pre-run summarizer path, clamped to the ceiling."""
    return min(_int_env("CONTEXT_MAX_TOKENS", 12000), hard_input_ceiling_tokens())


def iteration_budget_tokens() -> int:
    """Budget enforced before each model call inside an agent loop.

    Defaults to the hard ceiling: the loop's job is to not blow up the request,
    and trimming a specialist's evidence for cost reasons is an explicit choice
    an operator opts into with ``CONTEXT_ITERATION_MAX_TOKENS``, not a default
    that silently degrades diagnosis quality.
    """
    ceiling = hard_input_ceiling_tokens()
    configured = os.getenv("CONTEXT_ITERATION_MAX_TOKENS", "").strip()
    if not configured:
        return ceiling
    return min(_int_env("CONTEXT_ITERATION_MAX_TOKENS", ceiling), ceiling)


def tool_result_floor_chars() -> int:
    return _int_env("CONTEXT_TOOL_RESULT_FLOOR_CHARS", DEFAULT_TOOL_RESULT_FLOOR_CHARS)


# --------------------------------------------------------------------------
# Token counting
# --------------------------------------------------------------------------

_TOKENIZER_UNSET = object()
_tokenizer_cache: Any = _TOKENIZER_UNSET


def reset_tokenizer_cache() -> None:
    """Forget the resolved tokenizer (tests, or after changing CONTEXT_TOKENIZER)."""
    global _tokenizer_cache
    _tokenizer_cache = _TOKENIZER_UNSET


def _tokenizer() -> Any:
    """Resolve a tokenizer once, or ``None`` to use the character heuristic.

    Resolution is attempted exactly once per process and never retried: the
    first ``tiktoken.get_encoding`` call may reach out for its BPE file, and a
    budget check on the hot path must not be able to hang on a network timeout
    once per iteration in an air-gapped cluster.
    """
    global _tokenizer_cache
    if _tokenizer_cache is not _TOKENIZER_UNSET:
        return _tokenizer_cache

    mode = os.getenv("CONTEXT_TOKENIZER", "auto").strip().lower()
    if mode in ("heuristic", "none", "off"):
        _tokenizer_cache = None
        return None

    try:
        import tiktoken

        _tokenizer_cache = tiktoken.get_encoding("cl100k_base")
        logger.debug("context budgeting using tiktoken/cl100k_base (Claude estimate)")
    except Exception as exc:  # missing wheel, no cache, no network — all the same
        logger.info(
            "context budgeting falling back to the character heuristic (%s)", exc
        )
        _tokenizer_cache = None
    return _tokenizer_cache


def token_safety_ratio() -> float:
    return _float_env("CONTEXT_TOKEN_SAFETY_RATIO", DEFAULT_TOKEN_SAFETY_RATIO) or 1.0


def estimate_tokens(text: str) -> int:
    """Character heuristic. Kept as the tokenizer-free fallback and baseline."""
    return len(text or "") // _CHARS_PER_TOKEN


def count_tokens(text: Any) -> int:
    """Budget-grade token count for a string: tokenizer if available, then margin."""
    if text is None:
        return 0
    text = text if isinstance(text, str) else str(text)
    if not text:
        return 0

    encoder = _tokenizer()
    if encoder is None:
        raw = estimate_tokens(text)
    else:
        try:
            raw = len(encoder.encode(text, disallowed_special=()))
        except Exception:  # pragma: no cover - encoder should not throw
            raw = estimate_tokens(text)

    return max(1, int(raw * token_safety_ratio()))


# --------------------------------------------------------------------------
# Message shape helpers (BaseMessage or dict)
# --------------------------------------------------------------------------


def _raw_content(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("content", "")
    return getattr(message, "content", "")


def _content(message: Any) -> str:
    """Message text, flattening Anthropic's list-of-blocks content shape."""
    content = _raw_content(message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                # text blocks carry "text"; tool_use blocks carry "input"
                parts.append(str(block.get("text") or block.get("input") or ""))
            else:
                parts.append(str(block))
        return "\n".join(part for part in parts if part)
    return str(content or "")


def _role(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("role") or message.get("type") or "message")
    return str(getattr(message, "type", getattr(message, "role", "message")))


def _tool_calls(message: Any) -> List[Any]:
    if isinstance(message, dict):
        calls = message.get("tool_calls")
    else:
        calls = getattr(message, "tool_calls", None)
    return list(calls) if calls else []


def _tool_call_id(message: Any) -> Optional[str]:
    """The id a tool *result* answers, or ``None`` if this is not one."""
    if isinstance(message, dict):
        value = message.get("tool_call_id")
    else:
        value = getattr(message, "tool_call_id", None)
    return str(value) if value else None


def _is_tool_result(message: Any) -> bool:
    return _tool_call_id(message) is not None or _role(message) == "tool"


def _tool_call_tokens(message: Any) -> int:
    """Tool-call arguments cost tokens even though ``content`` is usually empty."""
    calls = _tool_calls(message)
    if not calls:
        return 0
    try:
        payload = json.dumps(calls, default=str)
    except (TypeError, ValueError):
        payload = str(calls)
    return count_tokens(payload)


def message_tokens(message: Any) -> int:
    """What one message actually costs: text + tool-call payload + framing."""
    return (
        count_tokens(_content(message))
        + _tool_call_tokens(message)
        + _PER_MESSAGE_OVERHEAD_TOKENS
    )


def messages_tokens(messages: List[Any]) -> int:
    return sum(message_tokens(m) for m in (messages or []))


def _with_content(message: Any, text: str) -> Any:
    """A copy of ``message`` carrying ``text``, without mutating the original."""
    if isinstance(message, dict):
        return {**message, "content": text}
    model_copy = getattr(message, "model_copy", None)
    if callable(model_copy):
        try:
            return model_copy(update={"content": text})
        except Exception:  # pragma: no cover - non-pydantic message subclass
            pass
    clone = copy.copy(message)
    try:
        clone.content = text
    except Exception:  # pragma: no cover - frozen and not pydantic
        return message
    return clone


def _make_note(text: str) -> Dict[str, str]:
    return {"role": _ELISION_ROLE, "content": text}


# --------------------------------------------------------------------------
# Turn groups: never separate a tool call from its result
# --------------------------------------------------------------------------


def turn_groups(messages: List[Any]) -> List[Tuple[int, int]]:
    """``(start, end)`` half-open spans that must be kept or dropped together.

    An assistant message carrying ``tool_calls`` and the tool results answering
    it are one unit. Splitting them is not a quality regression, it is a hard
    provider error — Anthropic rejects a ``tool_result`` with no matching
    ``tool_use``, and the old tail slice in :func:`compact` could produce
    exactly that on any history that happened to be cut mid-turn.
    """
    groups: List[Tuple[int, int]] = []
    for index, message in enumerate(messages or []):
        if groups and _is_tool_result(message):
            start, _ = groups[-1]
            groups[-1] = (start, index + 1)
        else:
            groups.append((index, index + 1))
    return groups


def _safe_tail_start(messages: List[Any], desired_start: int) -> int:
    """Move a cut point back to a group boundary so no tool result is orphaned."""
    if desired_start <= 0:
        return 0
    for start, end in turn_groups(messages):
        if start < desired_start < end:
            return start
    return desired_start


# --------------------------------------------------------------------------
# Deterministic per-iteration fitting
# --------------------------------------------------------------------------


@dataclass
class FitReport:
    """What fitting had to give up. Log it; silent truncation is a lie."""

    budget_tokens: int
    before_tokens: int
    after_tokens: int
    truncated_results: int = 0
    dropped_messages: int = 0
    dropped_groups: int = 0
    fitted: bool = True
    actions: List[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.truncated_results or self.dropped_messages)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "budget_tokens": self.budget_tokens,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "truncated_results": self.truncated_results,
            "dropped_messages": self.dropped_messages,
            "dropped_groups": self.dropped_groups,
            "fitted": self.fitted,
            "actions": list(self.actions),
        }


def _shrink_text(text: str, floor_chars: int) -> str:
    """Keep the head and tail of a payload; elide the middle.

    Both ends carry signal — a JSON envelope and status up front, the error or
    the last rows at the end — so a plain ``text[:n]`` throws away the half that
    most often holds the answer.
    """
    if len(text) <= floor_chars:
        return text
    head = max(floor_chars // 2, 1)
    tail = max(floor_chars - head, 0)
    removed = len(text) - head - tail
    marker = (
        f"\n… [{removed} characters elided to fit the model's context budget; "
        "the full tool output is preserved in this run's evidence artifact] …\n"
    )
    return text[:head] + marker + (text[-tail:] if tail else "")


def fit_to_budget(
    messages: List[Any],
    budget_tokens: Optional[int] = None,
    *,
    floor_chars: Optional[int] = None,
) -> Tuple[List[Any], FitReport]:
    """Fit ``messages`` under ``budget_tokens`` without calling a model.

    Order of sacrifice, cheapest information first:

    1. Shrink the oldest tool results toward ``floor_chars``.
    2. Drop whole oldest turn groups, replaced by one counted note.
    3. As a last resort shrink the protected tail too — a truncated request
       that answers beats a well-formed one the provider refuses.

    The first message (a system prompt, when present) and the most recent turn
    group are protected through steps 1 and 2. Returns the fitted list and a
    :class:`FitReport`; the input is never mutated.
    """
    budget = budget_tokens if budget_tokens is not None else iteration_budget_tokens()
    floor = floor_chars if floor_chars is not None else tool_result_floor_chars()
    working = list(messages or [])

    # Cost each message once and keep a running total. This runs before *every*
    # model call in a ReAct loop, and re-counting a 50k-token tool result on
    # each pass would make the budgeter itself the expensive part.
    costs = [message_tokens(m) for m in working]
    total = sum(costs)
    report = FitReport(budget_tokens=budget, before_tokens=total, after_tokens=total)

    if total <= budget or not working:
        return working, report

    groups = turn_groups(working)
    # Protected: the leading system prompt and the newest turn.
    protected_head = 1 if _role(working[0]) in ("system", "SystemMessage") else 0
    protected_tail_start = groups[-1][0] if groups else len(working)

    # 1. Shrink old tool results, oldest first.
    for index in range(protected_head, protected_tail_start):
        if total <= budget:
            break
        message = working[index]
        if not _is_tool_result(message):
            continue
        text = _content(message)
        if len(text) <= floor:
            continue
        working[index] = _with_content(message, _shrink_text(text, floor))
        total -= costs[index]
        costs[index] = message_tokens(working[index])
        total += costs[index]
        report.truncated_results += 1

    # 2. Drop whole old turn groups.
    if total > budget:
        dropped: set[int] = set()
        for start, end in turn_groups(working):
            if total <= budget:
                break
            if start < protected_head or end > protected_tail_start:
                continue
            dropped.update(range(start, end))
            total -= sum(costs[start:end])
            report.dropped_groups += 1
        if dropped:
            report.dropped_messages = len(dropped)
            note = _make_note(
                f"[{report.dropped_groups} earlier tool round(s) "
                f"({report.dropped_messages} messages) elided to fit the model's "
                "context budget; the full transcript is preserved in this run's "
                "evidence artifact]"
            )
            kept = [m for i, m in enumerate(working) if i not in dropped]
            working = kept[:protected_head] + [note] + kept[protected_head:]
            costs = [c for i, c in enumerate(costs) if i not in dropped]
            costs.insert(protected_head, message_tokens(note))
            total = sum(costs)

    # 3. Last resort: what is left, protected tail included, is still too big.
    #    A truncated request that gets an answer beats a well-formed one the
    #    provider refuses outright.
    if total > budget:
        hard_floor = max(floor // 4, 200)
        for index in range(len(working) - 1, -1, -1):
            if total <= budget:
                break
            text = _content(working[index])
            if len(text) <= hard_floor:
                continue
            working[index] = _with_content(working[index], _shrink_text(text, hard_floor))
            total -= costs[index]
            costs[index] = message_tokens(working[index])
            total += costs[index]
            report.truncated_results += 1
            report.actions.append(f"hard-truncated message {index}")

    report.after_tokens = total
    report.fitted = report.after_tokens <= budget
    if not report.fitted:
        logger.warning(
            "context still over budget after fitting: %d > %d tokens "
            "(the irreducible system prompt and tool schemas may exceed it)",
            report.after_tokens,
            budget,
        )
    elif report.changed:
        # Debug, not info: callers pass `on_report` to log this with the
        # specialist's name attached, and two lines per iteration is noise.
        logger.debug(
            "context fit: %d → %d tokens (budget %d); %d tool result(s) "
            "truncated, %d message(s) dropped",
            report.before_tokens,
            report.after_tokens,
            budget,
            report.truncated_results,
            report.dropped_messages,
        )
    return working, report


def make_pre_model_hook(
    budget_tokens: Optional[int] = None,
    *,
    on_report: Optional[Callable[[FitReport], None]] = None,
) -> Callable[[Any], Dict[str, Any]]:
    """A LangGraph ``pre_model_hook`` that budgets the history before each call.

    Returns ``{"llm_input_messages": ...}``, which rewrites only what the model
    sees. Graph state keeps the full transcript, so tool-failure detection and
    the stored evidence artifact are unaffected by anything trimmed here.

    Fitting failures are swallowed: an over-long prompt is a bad request, but a
    crash in the budgeter would take down an investigation that would otherwise
    have succeeded.
    """

    def _hook(state: Any) -> Dict[str, Any]:
        messages = (
            state.get("messages", [])
            if isinstance(state, dict)
            else getattr(state, "messages", [])
        )
        try:
            fitted, report = fit_to_budget(messages, budget_tokens)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("context budgeting skipped (non-fatal): %s", exc)
            return {"llm_input_messages": list(messages or [])}
        if on_report is not None and report.changed:
            try:
                on_report(report)
            except Exception:  # pragma: no cover - reporting must not break the loop
                logger.debug("context fit report callback failed", exc_info=True)
        return {"llm_input_messages": fitted}

    return _hook


# --------------------------------------------------------------------------
# Pre-run, LLM-backed compaction
# --------------------------------------------------------------------------


def should_compact(messages: List[Any], max_tokens: Optional[int] = None) -> bool:
    budget = max_tokens if max_tokens is not None else default_max_tokens()
    return messages_tokens(messages) > budget


def format_history(messages: List[Any]) -> str:
    return "\n".join(f"[{_role(m)}] {_content(m)}" for m in messages)


async def compact(
    messages: List[Any],
    summarizer: Callable[[str], Awaitable[str]],
    keep_recent: Optional[int] = None,
    max_tokens: Optional[int] = None,
) -> Tuple[List[Any], bool]:
    """Compact ``messages`` if over budget.

    Keeps roughly the last ``keep_recent`` messages verbatim and replaces
    everything before them with one summary message. The cut point is moved
    back to a turn boundary when needed, so a tool result is never separated
    from the call it answers. Returns ``(new_messages, did_compact)``; under
    budget, returns the input unchanged.
    """
    keep_recent = (
        keep_recent if keep_recent is not None else _int_env("CONTEXT_KEEP_RECENT", 6)
    )

    if not should_compact(messages, max_tokens):
        return messages, False
    if len(messages) <= keep_recent:
        return messages, False

    tail_start = _safe_tail_start(messages, len(messages) - keep_recent)
    if tail_start <= 0:
        # Every message belongs to one unsplittable turn; nothing to summarize.
        return messages, False

    head = messages[:tail_start]
    tail = messages[tail_start:]
    summary_text = await summarizer(format_history(head))
    summary_message = _make_note(
        f"[compacted summary of {len(head)} earlier messages]\n{summary_text}"
    )
    logger.info(
        f"🗜️  Context compaction: {len(head)} messages → 1 summary; kept {len(tail)} recent"
    )
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
