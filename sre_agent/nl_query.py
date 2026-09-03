#!/usr/bin/env python3
"""
Natural-language → verified metric query (project #3: Slack + AI / PromptQL).

PromptQL's core idea is that an NL question is turned into a *plan* that generates
a query in a constrained language, which is then **verified** and executed
deterministically — so you get reliable answers instead of NL-to-SQL
hallucination. This module applies that to the SRE agent: an on-call engineer
(in chat) asks "what's the checkout error rate over the last hour?" and gets a
validated PromQL query executed against the Prometheus MCP.

Pipeline (the "plan"):
    parse intent → generate PromQL → **validate** (allow-listed metrics/functions,
    bounded window, balanced syntax) → execute (only if valid).

Deterministic templates cover the common intents (error rate, latency, traffic,
saturation, payment failures) so the core is reliable and fully testable; an LLM
fallback can be layered on for out-of-pattern questions. Chat transport
(Slack/Buzz) is a thin outer layer — see docs/CHAT_INTEGRATION.md.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Allow-listed metrics (common app service metrics) and PromQL functions.
_ALLOWED_METRICS = {
    "http_requests_total", "http_errors_total", "http_request_duration_seconds_bucket",
    "payment_failures_total", "payment_provider_up", "process_memory_bytes_simulated",
    "db_query_duration_seconds_bucket",
}
_ALLOWED_FUNCS = {"rate", "sum", "avg", "max", "min", "histogram_quantile", "by", "increase"}

_KNOWN_SERVICES = {
    "checkout": "checkout-service", "checkout-service": "checkout-service",
    "inventory": "inventory-service", "inventory-service": "inventory-service",
    "payment": "payment-service", "payment-service": "payment-service",
    "gateway": "api-gateway", "api-gateway": "api-gateway", "api gateway": "api-gateway",
}

_MAX_WINDOW_HOURS = 24


@dataclass
class QueryIntent:
    metric_kind: str            # error_rate | latency | traffic | saturation | payment_failures
    service: Optional[str]
    window: str = "5m"
    quantile: float = 0.95


@dataclass
class QueryPlan:
    question: str
    steps: List[str]
    intent: Optional[QueryIntent]
    promql: str
    valid: bool
    reason: str = ""
    generated_by: str = "template"  # "template" | "llm"


@dataclass
class NLQueryResult:
    question: str
    plan: QueryPlan
    executed: bool = False
    data: Any = None
    error: str = ""


# ── Intent parsing ───────────────────────────────────────────────────────────
def _parse_service(q: str) -> Optional[str]:
    for key, svc in _KNOWN_SERVICES.items():
        if key in q:
            return svc
    return None


def _parse_window(q: str) -> str:
    m = re.search(r"last\s+(\d+)\s*(second|sec|minute|min|hour|hr|day)s?", q)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        code = {"second": "s", "sec": "s", "minute": "m", "min": "m",
                "hour": "h", "hr": "h", "day": "d"}[unit]
        return f"{n}{code}"
    if "hour" in q or "hr" in q:
        return "1h"
    if "day" in q:
        return "1d"
    return "5m"


def _parse_quantile(q: str) -> float:
    if "p99" in q or "99th" in q:
        return 0.99
    if "median" in q or "p50" in q or "50th" in q:
        return 0.50
    return 0.95


def parse_intent(question: str) -> Optional[QueryIntent]:
    q = " ".join(question.lower().split())
    service = _parse_service(q)
    window = _parse_window(q)
    quantile = _parse_quantile(q)

    if "payment" in q and ("fail" in q or "failure" in q):
        return QueryIntent("payment_failures", service, window, quantile)
    if any(w in q for w in ("error rate", "error", "5xx", "failing", "failure")):
        return QueryIntent("error_rate", service, window, quantile)
    if any(w in q for w in ("latency", "slow", "response time", "p95", "p99", "duration")):
        return QueryIntent("latency", service, window, quantile)
    if any(w in q for w in ("traffic", "throughput", "rps", "request rate", "requests per")):
        return QueryIntent("traffic", service, window, quantile)
    if any(w in q for w in ("memory", "saturation", "cpu", "resource")):
        return QueryIntent("saturation", service, window, quantile)
    return None


# ── PromQL generation ────────────────────────────────────────────────────────
def _sel(service: Optional[str], base: str = "") -> str:
    if service:
        inner = f'service="{service}"' + (f",{base}" if base else "")
        return "{" + inner + "}"
    return "{" + base + "}" if base else ""


def build_promql(intent: QueryIntent) -> str:
    s, w = intent.service, intent.window
    if intent.metric_kind == "error_rate":
        return (f"sum(rate(http_errors_total{_sel(s)}[{w}])) "
                f"/ sum(rate(http_requests_total{_sel(s)}[{w}]))")
    if intent.metric_kind == "latency":
        return f"histogram_quantile({intent.quantile}, rate(http_request_duration_seconds_bucket{_sel(s)}[{w}]))"
    if intent.metric_kind == "traffic":
        return f"sum(rate(http_requests_total{_sel(s)}[{w}]))"
    if intent.metric_kind == "saturation":
        return f"process_memory_bytes_simulated{_sel(s)}"
    if intent.metric_kind == "payment_failures":
        return f"rate(payment_failures_total[{w}])"
    return ""


# ── Verification (the PromptQL-style guarantee) ──────────────────────────────
def _identifiers_to_check(query: str) -> List[str]:
    """Metric/function identifiers only — with label keys/values and range
    windows stripped out so we don't mistake a label key (e.g. `service`) for a
    metric name."""
    q = re.sub(r'"[^"]*"', "", query)      # strip quoted label values
    q = re.sub(r"\{[^}]*\}", "", q)         # strip label-selector blocks (label keys)
    q = re.sub(r"\[[^\]]*\]", "", q)         # strip range windows
    return re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", q)


def validate_promql(
    query: str, allowed_metrics: Optional[FrozenSet[str]] = None
) -> Tuple[bool, str]:
    """The 'verify' step: only run queries we can vouch for.

    allowed_metrics: when given, replaces the static _ALLOWED_METRICS allow-list
    with a live metric catalog (see fetch_metric_catalog) — grounding validation
    in what this cluster's Prometheus actually has, rather than a fixed set
    that only covers the demo app. Defaults to the static set so existing
    offline callers/tests are unaffected.
    """
    if not query or not query.strip():
        return False, "empty query"
    if query.count("{") != query.count("}") or query.count("(") != query.count(")"):
        return False, "unbalanced braces/parentheses"
    if ";" in query or "\n" in query.strip():
        return False, "illegal characters"

    # Windows must be bounded.
    for n, unit in re.findall(r"\[(\d+)([smhd])\]", query):
        hours = int(n) * {"s": 1 / 3600, "m": 1 / 60, "h": 1, "d": 24}[unit]
        if hours > _MAX_WINDOW_HOURS:
            return False, f"time window exceeds {_MAX_WINDOW_HOURS}h cap"

    # Every referenced identifier must be an allowed metric or function.
    metrics = allowed_metrics if allowed_metrics is not None else _ALLOWED_METRICS
    unknown = set(_identifiers_to_check(query)) - metrics - _ALLOWED_FUNCS
    if unknown:
        return False, f"references non-allow-listed identifiers: {sorted(unknown)}"

    return True, "ok"


def plan_and_generate(question: str) -> QueryPlan:
    """Produce the full plan: parse → generate → validate (no execution).

    Deterministic-only (template intents, static allow-list, regex-based
    validation) — no I/O, always available, what run_nl_query uses unless a
    caller explicitly opts into the live/LLM-augmented pipeline below via
    tool_caller/llm/use_live_* on run_nl_query.
    """
    steps = ["parse intent", "generate PromQL", "validate query", "execute"]
    intent = parse_intent(question)
    if intent is None:
        return QueryPlan(question, steps, None, "", False, "could not map question to a known metric intent")
    promql = build_promql(intent)
    valid, reason = validate_promql(promql)
    return QueryPlan(question, steps, intent, promql, valid, reason)


# ── Production-grade augmentation: live catalog, real parser, LLM fallback ──
#
# The deterministic path above is intentionally closed (fixed intents, fixed
# metric allow-list, regex structural checks) — reliable and fully testable
# with no I/O. The functions below extend it without replacing it: they
# ground validation in the cluster's *actual* live metric catalog and a real
# PromQL parser (Prometheus's own /api/v1/format_query), and add an LLM
# generation path for questions that don't map to a known template — while
# keeping the PromptQL guarantee intact, since an LLM-generated query still
# has to pass the same verify step (now stricter, not weaker) before it can
# execute.

def _parsed_tool_result(raw: Any) -> Any:
    """Unwrap and parse an MCP tool_caller's return value.

    MCP tool callers built via `build_mcp_tool_caller`/`build_metrics_tool_caller`
    (langchain_mcp_adapters) return a list of content blocks (e.g.
    `[{"type": "text", "text": "<json>"}]`), not a bare string or dict — the
    same shape that silently broke live-catalog grounding in
    `service_topology.py` until that unwrap was added there. Test doubles
    that return a plain dict/string directly still work unchanged.
    """
    if isinstance(raw, list) and raw:
        first = raw[0]
        if isinstance(first, dict) and "text" in first:
            raw = first["text"]
        elif hasattr(first, "text"):
            raw = first.text
    return json.loads(raw) if isinstance(raw, str) else raw


async def fetch_metric_catalog(
    tool_caller: Callable[[str, Dict[str, Any]], Any],
) -> Optional[FrozenSet[str]]:
    """Fetch the live metric catalog from Prometheus (list_metric_names MCP
    tool). Returns None (not raises) if the tool is unavailable or the call
    fails — callers should fall back to the static allow-list, since a
    missing catalog must never block validation, only make it less precise.
    """
    try:
        raw = await tool_caller("list_metric_names", {})
        data = _parsed_tool_result(raw)
        names = data.get("metric_names") if isinstance(data, dict) else None
        if not names:
            return None
        return frozenset(names)
    except Exception as e:
        logger.warning(f"NLQuery: live metric catalog fetch failed, using static allow-list ({e})")
        return None


async def validate_promql_syntax_live(
    query: str, tool_caller: Callable[[str, Dict[str, Any]], Any]
) -> Tuple[bool, str]:
    """Real-parser syntax check via Prometheus's /api/v1/format_query
    (validate_promql_syntax MCP tool), layered on top of (not instead of) the
    structural checks in validate_promql. Defense in depth: the regex checks
    catch injection/unbounded-window issues cheaply with no I/O; this catches
    genuine PromQL grammar errors the regex approach can't detect.

    Fails open — (True, "...") — when the tool is unavailable/unreachable,
    since this is an additional guarantee, not the only one.
    """
    try:
        raw = await tool_caller("validate_promql_syntax", {"query": query})
        data = _parsed_tool_result(raw)
        if not isinstance(data, dict):
            return True, "live syntax check returned unexpected shape; skipped"
        if data.get("valid"):
            return True, "ok"
        return False, str(data.get("error", "rejected by Prometheus parser"))
    except Exception as e:
        logger.warning(f"NLQuery: live PromQL syntax check unavailable, skipping ({e})")
        return True, "live syntax check unavailable"


_FEW_SHOT_EXAMPLES = (
    'Q: "checkout error rate over the last hour"\n'
    'PromQL: sum(rate(http_errors_total{service="checkout-service"}[1h])) '
    '/ sum(rate(http_requests_total{service="checkout-service"}[1h]))\n\n'
    'Q: "p99 latency for inventory over the last 15 minutes"\n'
    'PromQL: histogram_quantile(0.99, rate(http_request_duration_seconds_bucket'
    '{service="inventory-service"}[15m]))\n'
)


async def generate_promql_llm(
    question: str, llm: Any, allowed_metrics: Optional[FrozenSet[str]] = None
) -> Optional[str]:
    """LLM-generated PromQL for questions the deterministic templates can't
    map, grounded in the live (or static-fallback) metric catalog. The
    output still has to pass validate_promql / validate_promql_syntax_live
    before it can ever execute — this only widens what can be *proposed*,
    it does not weaken what can be *run*.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    metrics = sorted(allowed_metrics) if allowed_metrics else sorted(_ALLOWED_METRICS)
    system = (
        "You translate an SRE's natural-language question into exactly one PromQL query.\n"
        f"Only use these metric names: {', '.join(metrics)}\n"
        f"Only use these functions: {', '.join(sorted(_ALLOWED_FUNCS))}\n"
        f"Range windows must not exceed {_MAX_WINDOW_HOURS}h.\n"
        "Output ONLY the PromQL query on a single line — no markdown fences, no explanation.\n\n"
        f"{_FEW_SHOT_EXAMPLES}"
    )
    try:
        resp = await llm.ainvoke([SystemMessage(content=system), HumanMessage(content=question)])
        text = str(getattr(resp, "content", resp)).strip()
        # Strip an accidental code fence — models do this even when told not to.
        text = re.sub(r"^```[a-zA-Z]*\n?|```$", "", text).strip()
        first_line = text.splitlines()[0].strip() if text else ""
        return first_line or None
    except Exception as e:
        logger.warning(f"NLQuery: LLM PromQL generation failed ({e})")
        return None


async def plan_and_generate_verified(
    question: str,
    *,
    tool_caller: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    llm: Optional[Any] = None,
    use_live_catalog: bool = False,
    use_live_syntax_check: bool = False,
) -> QueryPlan:
    """Production pipeline: parse → (template | LLM fallback) → verify
    (structural + live catalog + real parser) — no execution.

    Strictly additive over plan_and_generate: with every optional arg at its
    default this produces the identical deterministic-only plan.
    """
    steps = ["parse intent", "generate PromQL", "validate query", "execute"]

    allowed_metrics: Optional[FrozenSet[str]] = None
    if use_live_catalog and tool_caller is not None:
        allowed_metrics = await fetch_metric_catalog(tool_caller)

    intent = parse_intent(question)
    if intent is not None:
        promql, generated_by = build_promql(intent), "template"
    elif llm is not None:
        promql = await generate_promql_llm(question, llm, allowed_metrics)
        generated_by = "llm"
        if not promql:
            return QueryPlan(
                question, steps, None, "", False,
                "could not map question to a known metric intent, and LLM generation failed",
                generated_by="llm",
            )
    else:
        return QueryPlan(
            question, steps, None, "", False,
            "could not map question to a known metric intent", generated_by="template",
        )

    valid, reason = validate_promql(promql, allowed_metrics=allowed_metrics)
    if valid and use_live_syntax_check and tool_caller is not None:
        live_valid, live_reason = await validate_promql_syntax_live(promql, tool_caller)
        if not live_valid:
            valid, reason = False, f"rejected by Prometheus parser: {live_reason}"

    return QueryPlan(question, steps, intent, promql, valid, reason, generated_by=generated_by)


async def run_nl_query(
    question: str,
    tool_caller: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    execute: bool = True,
    metric_tool: str = "get_metric",
    *,
    llm: Optional[Any] = None,
    use_live_catalog: bool = False,
    use_live_syntax_check: bool = False,
) -> NLQueryResult:
    """Plan → generate → verify → (execute). Only executes a validated query.

    llm/use_live_catalog/use_live_syntax_check are strictly opt-in: with all
    three at their defaults this calls the same deterministic-only
    plan_and_generate as before, and — for a tool_caller — the same one
    metric_tool call on success or zero calls on an invalid plan, unchanged
    from prior behavior. Passing any of them routes through
    plan_and_generate_verified instead, which only ever *adds* calls
    (catalog fetch, live syntax check) beyond the final metric_tool call.
    """
    if llm is None and not use_live_catalog and not use_live_syntax_check:
        plan = plan_and_generate(question)
    else:
        plan = await plan_and_generate_verified(
            question,
            tool_caller=tool_caller,
            llm=llm,
            use_live_catalog=use_live_catalog,
            use_live_syntax_check=use_live_syntax_check,
        )
    logger.info(f"NLQuery: '{question}' → {plan.promql!r} valid={plan.valid}")

    if not plan.valid:
        return NLQueryResult(question, plan, executed=False, error=plan.reason)
    if not execute or tool_caller is None:
        return NLQueryResult(question, plan, executed=False, error="not executed (no tool_caller)")

    try:
        data = await tool_caller(metric_tool, {"query": plan.promql})
        return NLQueryResult(question, plan, executed=True, data=data)
    except Exception as e:
        return NLQueryResult(question, plan, executed=False, error=f"execution failed: {e}")


# ── Chat routing (the Slack/Buzz "AI member" behavior) ───────────────────────
def classify_chat_message(text: str) -> Dict[str, Any]:
    """Route a chat message to: a data query, an incident steer, or a greeting.

    This is the logic behind "tag the SRE agent in Slack/Buzz and it responds":
    a data question runs an NL query; a steer feeds the existing human-checkpoint
    queue; a greeting is acknowledged.
    """
    t = " ".join((text or "").lower().split())
    if not t or t in {"hi", "hello", "hey", "thanks", "thank you", "ok", "cool"}:
        return {"mode": "greeting"}
    if any(w in t for w in ("focus", "pause", "stop", "prioritize", "skip", "rollback now", "check logs")):
        return {"mode": "steer"}
    if parse_intent(t) is not None and any(t.startswith(w) for w in ("show", "what", "how", "get", "give", "is ")):
        return {"mode": "query"}
    return {"mode": "steer"}  # default: treat as an instruction to the investigation


def build_incident_message_payload(incident_id: str, text: str) -> Tuple[str, Dict[str, Any]]:
    """Shape a chat 'steer' into the existing mission-control message endpoint,
    which feeds the human-checkpoint queue the supervisor already consumes."""
    return f"/api/v1/incidents/{incident_id}/message", {"message": text}


# ── Ad hoc chat (no tracked incident): genuine LLM conversation ──────────────
# In-thread replies inside a tracked incident already get a real, memory-backed
# conversation via mission_control.handle_incident_message (see war_room.py).
# This is the *other* case: a bare @mention or DM with no incident context,
# which used to silently fall through classify_chat_message's default "steer"
# branch and get answered with a misleading "I'll fold that into the live
# investigation" reply even though there's no investigation to fold into.
_CHAT_SYSTEM_PROMPT = (
    "You are Sentinel, an on-call SRE assistant embedded in Slack. Reply "
    "conversationally and concisely (2-4 sentences, plain text — no markdown "
    "headers). You are not currently inside a tracked incident investigation, "
    "so never claim to be investigating, executing, or steering anything; if "
    "the user wants to steer or ask about a live incident, tell them to "
    "mention you inside that incident's thread instead. Answer general "
    "SRE/on-call questions helpfully, and say so plainly if you don't know "
    "something rather than inventing details."
)

_CHAT_HISTORY_TURNS = 6  # user+assistant pairs of memory kept per session

_CHAT_FALLBACK_REPLY = (
    "I don't have a live LLM connection configured right now, so I can't hold "
    "a full conversation — but I'm listening. Mention me inside an incident "
    "thread to steer or ask about that investigation, or ask a direct metric "
    "question (e.g. \"what's the checkout error rate?\")."
)


async def generate_chat_reply_llm(
    text: str, *, history: Optional[List[str]] = None, llm: Any = None
) -> Optional[str]:
    """Generate a conversational reply for ad hoc chat (no tracked incident).

    Best-effort: returns None (never raises) if no LLM is available or the
    call fails, so callers can fall back to a graceful canned reply.
    """
    if llm is None:
        return None
    try:
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    except Exception:
        return None

    messages: List[Any] = [SystemMessage(content=_CHAT_SYSTEM_PROMPT)]
    for line in (history or [])[-_CHAT_HISTORY_TURNS * 2 :]:
        if line.startswith("assistant: "):
            messages.append(AIMessage(content=line[len("assistant: ") :]))
        elif line.startswith("user: "):
            messages.append(HumanMessage(content=line[len("user: ") :]))
    messages.append(HumanMessage(content=text))

    try:
        response = await llm.ainvoke(messages)
        reply = (getattr(response, "content", "") or "").strip()
        return reply or None
    except Exception as e:
        logger.warning(f"NLQuery: chat reply generation failed ({e})")
        return None


async def _handle_ad_hoc_chat(
    text: str, *, llm: Any = None, session_key: Optional[str] = None
) -> Dict[str, Any]:
    """The 'chat' mode: no tracked incident, so hold a genuine short-memory
    conversation instead of pretending to steer an investigation."""
    history: List[str] = []
    store = None
    if session_key:
        try:
            from .redis_state_store import get_state_store

            store = get_state_store()
            history = store.get_logs(session_key)
        except Exception as e:
            logger.warning(f"NLQuery: chat history unavailable ({e})")

    resolved_llm = llm
    if resolved_llm is None:
        try:
            from .llm_utils import create_llm_with_fallback

            resolved_llm = create_llm_with_fallback()
        except Exception as e:
            logger.warning(f"NLQuery: no LLM available for chat reply ({e})")

    reply = await generate_chat_reply_llm(text, history=history, llm=resolved_llm)
    if reply is None:
        return {"mode": "chat", "reply": _CHAT_FALLBACK_REPLY, "llm_used": False}

    if store is not None:
        try:
            store.append_log(session_key, f"user: {text}")
            store.append_log(session_key, f"assistant: {reply}")
        except Exception as e:
            logger.warning(f"NLQuery: failed to persist chat history ({e})")

    return {"mode": "chat", "reply": reply, "llm_used": True}


# ── Runtime integration ──────────────────────────────────────────────────────
async def answer_metric_question(question: str, metrics_uri: Optional[str] = None) -> NLQueryResult:
    """End-to-end: run an NL query against the live Prometheus MCP server.

    Opts into the full production pipeline (live metric catalog, real-parser
    validation, LLM fallback for unmapped questions). The LLM is constructed
    best-effort via create_llm_with_fallback; if that fails (no provider
    creds configured, etc.) this still runs the deterministic-only path —
    same as calling run_nl_query with no extra kwargs.
    """
    from .executor import build_metrics_tool_caller  # lazy (MCP adapter)

    caller = await build_metrics_tool_caller(uri=metrics_uri)

    llm = None
    try:
        from .llm_utils import create_llm_with_fallback

        llm = create_llm_with_fallback()
    except Exception as e:
        logger.warning(f"NLQuery: no LLM available for fallback generation, template-only ({e})")

    return await run_nl_query(
        question,
        tool_caller=caller,
        llm=llm,
        use_live_catalog=True,
        use_live_syntax_check=True,
    )


async def handle_chat_message(
    text: str,
    incident_id: Optional[str] = None,
    *,
    llm: Any = None,
    session_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Dispatch a chat message (the Slack/Buzz 'AI member' behavior).

    - query    → run a verified NL metric query and return the result.
    - steer    → shape a POST to the incident message endpoint (human-checkpoint),
                 when there's a tracked incident to steer.
    - chat     → no tracked incident: a genuine LLM-driven conversational reply
                 with short-term memory (see _handle_ad_hoc_chat).
    - greeting → acknowledge.
    The chat transport calls this; the transport itself is deployment-specific.
    `llm`/`session_key` are optional: without them the 'chat' path degrades to
    a graceful canned reply (no crash, no misleading "steer" language).
    """
    route = classify_chat_message(text)
    mode = route["mode"]

    if mode == "query":
        result = await answer_metric_question(text)
        return {
            "mode": "query",
            "promql": result.plan.promql,
            "valid": result.plan.valid,
            "executed": result.executed,
            "data": result.data,
            "error": result.error,
        }
    if mode == "steer" and incident_id:
        path, body = build_incident_message_payload(incident_id, text)
        return {"mode": "steer", "post": {"path": path, "body": body}}
    if mode == "steer" and not incident_id:
        return await _handle_ad_hoc_chat(text, llm=llm, session_key=session_key)
    return {"mode": mode}
