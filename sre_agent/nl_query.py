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

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

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


def validate_promql(query: str) -> Tuple[bool, str]:
    """The 'verify' step: only run queries we can vouch for."""
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
    unknown = set(_identifiers_to_check(query)) - _ALLOWED_METRICS - _ALLOWED_FUNCS
    if unknown:
        return False, f"references non-allow-listed identifiers: {sorted(unknown)}"

    return True, "ok"


def plan_and_generate(question: str) -> QueryPlan:
    """Produce the full plan: parse → generate → validate (no execution)."""
    steps = ["parse intent", "generate PromQL", "validate query", "execute"]
    intent = parse_intent(question)
    if intent is None:
        return QueryPlan(question, steps, None, "", False, "could not map question to a known metric intent")
    promql = build_promql(intent)
    valid, reason = validate_promql(promql)
    return QueryPlan(question, steps, intent, promql, valid, reason)


async def run_nl_query(
    question: str,
    tool_caller: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    execute: bool = True,
    metric_tool: str = "get_metric",
) -> NLQueryResult:
    """Plan → generate → verify → (execute). Only executes a validated query."""
    plan = plan_and_generate(question)
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
    """End-to-end: run an NL query against the live Prometheus MCP server."""
    from .executor import build_metrics_tool_caller  # lazy (MCP adapter)

    caller = await build_metrics_tool_caller(uri=metrics_uri)
    return await run_nl_query(question, tool_caller=caller)


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
