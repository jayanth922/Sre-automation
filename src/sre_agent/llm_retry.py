#!/usr/bin/env python3
"""How many times one model call may be retried, shared by both LLM backends.

A transient provider overload must not end an investigation. On 2026-09-19
three consecutive background investigations died inside four minutes, each on a
single Anthropic 529 (``overloaded_error``) raised mid-ReAct-loop:

    {agent_runtime.py:2069},ERROR,SaaS Background execution failed:
    litellm.InternalServerError: AnthropicError -
    {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}

There was no retry anywhere in the call path — neither backend configured one —
so the exception unwound past every stage and hit the catch-all at
``agent_runtime.py:2069``, which logs that line and abandons the incident. The
alert stays open, the fault stays live, and nothing says why.

That is survivable for a single interactive call and fatal for this workload. A
complete investigation on the live cluster makes ~110 model calls across 21-49
minutes (Phase 0, 2026-09-19), and the run only counts if *every* one of them
lands. With no retry, even a 1% per-call overload rate loses roughly two runs in
three; at the rate observed that afternoon it lost three for three.

This module holds the number rather than either backend so the two paths cannot
drift apart — a retry policy that applies to only one of them is the same
outage with a smaller blast radius.

Retries here cover *transport* failures the provider reports as transient:
429, 500, 503 and 529. They deliberately do not cover bad requests, auth
failures, or a spent credit balance, which fail identically however many times
they are sent. Both backends space attempts out with exponential backoff.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Five attempts of exponential backoff outlast a normal provider capacity blip
# while still surfacing a genuine outage in under a minute or so. The cost of
# being wrong is asymmetric: one extra retry costs seconds, one unretried 529
# costs a 45-minute trial and everything already spent on it.
DEFAULT_LLM_MAX_RETRIES = 5


def llm_max_retries() -> int:
    """Retry budget for a single model call, from ``LLM_MAX_RETRIES``.

    Configurable because the right number depends on how much a lost call
    costs. A one-shot interactive request can afford to fail fast and let the
    caller decide; a benchmark trial that dies at minute 40 forfeits the whole
    trial, its API spend, and — because arms are only comparable at equal
    ``code_sha`` — cannot simply be re-run alongside the others later.

    A malformed value warns and falls back rather than raising: this is read on
    the path that builds the model, and a typo in an env var should not be the
    thing that stops an investigation from starting. ``0`` is honoured, so the
    old fail-fast behaviour stays available deliberately rather than by
    accident.
    """
    raw = os.getenv("LLM_MAX_RETRIES", "").strip()
    if not raw:
        return DEFAULT_LLM_MAX_RETRIES
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "LLM_MAX_RETRIES=%r is not an integer; using %d",
            raw,
            DEFAULT_LLM_MAX_RETRIES,
        )
        return DEFAULT_LLM_MAX_RETRIES
    if value < 0:
        logger.warning("LLM_MAX_RETRIES=%d is negative; using 0", value)
        return 0
    return value
