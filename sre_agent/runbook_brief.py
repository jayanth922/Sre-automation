#!/usr/bin/env python3
"""Turn a runbook search hit into a procedure the specialist can execute.

The runbook is the only operator-authored statement of what to do about an
alert, and until this module existed it reached the investigating agent as
``str(runbook_result)[:500]`` — 500 bytes sliced out of a minified JSON
envelope, which for the Notion MCP server means the alert's runbook arrived as
a fragment that stopped mid-key:

    {"query":"performance checkout latency","tool":"search_runbooks","count":2,
     "results":[{"runbook_id":"1f2a...","title":"Checkout p99 latency","serv

Two things were wrong with that, and only one of them is the truncation.

The second is that ``search_runbooks`` never returns a procedure at all. Each
hit carries a 320-character *excerpt* built by keyword proximity
(``_build_excerpt``); the numbered steps live in the page body, behind a
separate ``get_runbook_content`` call. So even an untruncated search response
would have told the agent a matching runbook exists without telling it what the
runbook says — which is why investigations opened with open-ended log
discovery and pulled megabytes of evidence to re-derive a fix somebody had
already written down.

This module closes both gaps: parse the envelope, pick the best hit, and render
the *fetched page body* into a budgeted brief that leads with remediation and
verification. Sections are dropped by priority rather than by position, because
a byte budget spent head-first spends it all on "Summary" and "Background" and
runs out exactly where the steps begin. Whatever is dropped is named, with the
call that retrieves it, so the omission stays recoverable by the agent instead
of being silently lossy.

The rendered text is data, not instruction: callers put it in front of a model
through ``prompt_guard.wrap_untrusted``. Nothing here knows about prompts.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# A runbook brief is an *instruction sheet*, not evidence: it is re-sent on
# every iteration of the specialist's ReAct loop, so its cost is multiplied by
# the loop length.
#
# 6000 was sized for a flat ~2.5KB page with one Remediation section. A
# branching procedure does not fit: every "Branch X — Action: ..." heading
# scores priority 0, five of them consume the whole budget, and Verification
# (priority 1) is dropped -- handing the agent every remediation option and
# then withholding the probe and threshold that say whether the one it chose
# worked. Measured on the shipped Meridian set: high-error-rate.md rendered
# 5966/6000 chars with Verification omitted.
#
# 9000 (~2.2k tokens) holds the largest shipped runbook whole. The extra ~750
# tokens sit in the cached prompt prefix, against a measured tail of 1.29M
# tokens across 8 calls from re-sent transcripts -- and this is still three
# orders of magnitude below the megabyte-scale log dumps it exists to prevent.
# tests/test_runbook_context.py fails if a shipped runbook outgrows it again.
DEFAULT_RUNBOOK_BRIEF_MAX_CHARS = 9000

# Below this a brief cannot hold a header plus one procedure step, and a
# fragment of a procedure is worse than none: it reads as complete.
_MIN_BRIEF_CHARS = 800

# Room held back for the trailing "sections omitted / this is an excerpt"
# notice. That notice is part of the brief, so a budget that excludes it is
# not a budget; reserving a fixed allowance keeps the arithmetic honest
# without having to render the notice before deciding what to drop.
_NOTICE_ALLOWANCE = 180

# Priority 0 is kept first and dropped last. The ordering is the answer to
# "if the agent reads only one section, which one stops the incident?"
_SECTION_PRIORITY: Tuple[Tuple[int, Tuple[str, ...]], ...] = (
    (
        0,
        (
            "remediation", "resolution", "resolve", "fix", "mitigation",
            "mitigate", "recovery", "step-by-step", "steps", "procedure",
            "action", "what to do", "runbook",
        ),
    ),
    (
        1,
        (
            "verification", "verify", "validate", "validation", "confirm",
            "success criteria", "exit criteria", "post-check",
        ),
    ),
    (
        2,
        (
            "diagnosis", "diagnostic", "triage", "troubleshooting",
            "investigation", "investigate", "detection", "symptom", "check",
            "query", "queries", "dashboard",
        ),
    ),
    (
        3,
        (
            "rollback", "roll back", "escalation", "escalate", "fallback",
            "if this fails", "on-call", "paging",
        ),
    ),
    (
        4,
        ("summary", "overview", "description", "impact", "scope", "root cause"),
    ),
)

# Background, prevention, references, post-mortem links: real content, but the
# first thing to go when the budget binds.
_DEFAULT_PRIORITY = 5

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")

# Property fields worth putting in the header. Ordered; blanks and the Notion
# placeholder "—" are dropped by the renderer.
_HEADER_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("service", "service"),
    ("incident_type", "incident type"),
    ("severity", "severity"),
    ("owner_team", "owner"),
    ("escalation_channel", "escalation"),
)

_EMPTY_PROPERTY_VALUES = {"", "—", "-", "n/a", "none", "null"}


def brief_max_chars() -> int:
    """Character budget for one rendered brief, overridable per deployment."""
    raw = os.getenv("RUNBOOK_BRIEF_MAX_CHARS", "").strip()
    if not raw:
        return DEFAULT_RUNBOOK_BRIEF_MAX_CHARS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "RUNBOOK_BRIEF_MAX_CHARS=%r is not an integer; using %d",
            raw,
            DEFAULT_RUNBOOK_BRIEF_MAX_CHARS,
        )
        return DEFAULT_RUNBOOK_BRIEF_MAX_CHARS
    return max(value, _MIN_BRIEF_CHARS)


# ---------------------------------------------------------------------------
# Parsing the MCP envelope
# ---------------------------------------------------------------------------


def _coerce_json(raw: Any) -> Any:
    """Decode an MCP tool result that may arrive as text, bytes, or objects."""
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", errors="replace")
        except Exception:
            return None
    if not isinstance(raw, str):
        raw = str(raw)
    text = raw.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def parse_search_results(raw: Any) -> List[Dict[str, Any]]:
    """Extract the hit list from a ``search_runbooks`` response.

    Accepts the ``{"query","tool","count","results":[...]}`` envelope, a bare
    list of hits, or a single hit object. Returns ``[]`` for anything else —
    including an error envelope — because a caller that cannot tell "no
    runbook" from "malformed response" will present one as the other.
    """
    payload = _coerce_json(raw)
    if payload is None:
        if raw:
            logger.debug("runbook search result was not JSON; ignoring")
        return []
    if isinstance(payload, dict):
        if payload.get("error"):
            logger.warning("runbook search returned an error: %s", payload["error"])
            return []
        results = payload.get("results")
        if results is None:
            # A single runbook object (get_runbook_content shape).
            return [payload] if payload.get("runbook_id") or payload.get("title") else []
        payload = results
    if not isinstance(payload, list):
        return []
    return [hit for hit in payload if isinstance(hit, dict)]


def _normalize(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


# One runbook routinely covers several alerts and several services, and the
# corpus records that in a single property: `Alert Name` reads
# "CheckoutHighErrorRate, PaymentServiceHighErrorRate, PaymentFailureSpike"
# and `Service` reads "checkout-service / payment-service / inventory-service"
# or "payment-service → checkout-service". Comparing those strings whole never
# matches anything, so a property that says "this runbook is for your alert"
# has to be read as the list it is.
_MULTI_VALUE_SEPARATORS = re.compile(r"[,;/|\n]|→|->|&|\band\b")


def _multi_values(value: Any) -> set:
    """Normalized members of a possibly multi-valued property."""
    return {
        member
        for member in (
            _normalize(part) for part in _MULTI_VALUE_SEPARATORS.split(str(value or ""))
        )
        if member
    }


def select_runbook(
    results: Sequence[Dict[str, Any]],
    *,
    alert_name: str = "",
    service: str = "",
) -> Optional[Dict[str, Any]]:
    """Pick the hit to brief on.

    The server already sorts by its own keyword score, so this overrides that
    ordering on one piece of evidence the server does not weigh: a runbook
    whose ``alert_name`` property names *this* alert was deliberately bound to
    it by whoever wrote it, which beats any amount of keyword overlap.

    Everything else defers to the score. Service match is only a tie-break
    within an equal score — never a promotion over a better one. Against the
    live corpus the stronger rule inverted the ranking: every curated runbook
    covers several services and so matches none of them exactly, while the
    auto-generated per-service pages match one exactly, which handed 18 of 22
    scenarios to an auto-generated page over the curated procedure that
    outscored it 8-to-0.
    """
    candidates = [hit for hit in results if isinstance(hit, dict)]
    if not candidates:
        return None

    wanted_alert = _normalize(alert_name)
    wanted_service = _normalize(service)

    def rank(item: Tuple[int, Dict[str, Any]]) -> Tuple[int, float, int, int]:
        index, hit = item
        alert_match = int(bool(wanted_alert) and wanted_alert in _multi_values(hit.get("alert_name")))
        service_match = int(
            bool(wanted_service) and wanted_service in _multi_values(hit.get("service"))
        )
        try:
            score = float(hit.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        # Index last preserves the server's own order as the final tie-break.
        return (-alert_match, -score, -service_match, index)

    return min(enumerate(candidates), key=rank)[1]


# ---------------------------------------------------------------------------
# Sectioning
# ---------------------------------------------------------------------------


def split_sections(content: str) -> List[Tuple[str, str]]:
    """Split markdown into ``(heading, body)`` pairs in document order.

    Text before the first heading is returned under the empty heading, so a
    runbook written as one unheaded block still survives budgeting.
    """
    lines = (content or "").splitlines()
    sections: List[Tuple[str, List[str]]] = []
    current_heading = ""
    current_body: List[str] = []
    for line in lines:
        match = _HEADING_RE.match(line.strip())
        if match:
            if current_heading or any(l.strip() for l in current_body):
                sections.append((current_heading, current_body))
            current_heading = match.group(2).strip()
            current_body = []
        else:
            current_body.append(line)
    if current_heading or any(l.strip() for l in current_body):
        sections.append((current_heading, current_body))
    return [(heading, "\n".join(body).strip()) for heading, body in sections]


def section_priority(heading: str) -> int:
    """Lower is kept longer. Unrecognised headings sort last but are not lost."""
    normalized = _normalize(heading)
    if not normalized:
        # Preamble before any heading — usually a one-line statement of what
        # the runbook covers. Cheap and orienting, so treat it as summary-tier.
        return 4
    for priority, keywords in _SECTION_PRIORITY:
        for keyword in keywords:
            if _normalize(keyword) in normalized:
                return priority
    return _DEFAULT_PRIORITY


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _property_line(runbook: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key, label in _HEADER_FIELDS:
        value = str(runbook.get(key) or "").strip()
        if value.lower() in _EMPTY_PROPERTY_VALUES:
            continue
        parts.append(f"{label}={value}")
    return ", ".join(parts)


def _fetch_hint(runbook: Dict[str, Any]) -> str:
    runbook_id = str(runbook.get("runbook_id") or "").strip()
    if runbook_id:
        return f'get_runbook_content("{runbook_id}")'
    title = str(runbook.get("title") or "").strip()
    if title:
        return f'get_runbook_content("{title}")'
    return "get_runbook_content"


def render_runbook_brief(
    runbook: Dict[str, Any],
    content: str = "",
    *,
    max_chars: Optional[int] = None,
) -> str:
    """Render one runbook as a budgeted, remediation-first brief.

    ``content`` is the full page body from ``get_runbook_content``. When it is
    empty the search excerpt is used instead and the brief says so, because an
    agent that mistakes a 320-character keyword excerpt for the whole procedure
    will report a partial fix as a complete one.
    """
    if not isinstance(runbook, dict):
        return ""

    budget = max_chars if max_chars is not None else brief_max_chars()
    budget = max(budget, _MIN_BRIEF_CHARS)

    title = str(runbook.get("title") or "Untitled runbook").strip()
    header: List[str] = [f"RUNBOOK: {title}"]
    properties = _property_line(runbook)
    if properties:
        header.append(properties)
    path = str(runbook.get("path") or "").strip()
    if path and path != "notion":
        header.append(f"source: {path}")
    header.append(
        "This is the operator-authored procedure for this alert. Work it first "
        "and in order; use open-ended search only for what it does not cover. "
        "Check each step against live tool output — if a precondition does not "
        "hold, report that instead of skipping the step silently."
    )

    body = (content or "").strip()
    excerpt_only = False
    if not body:
        body = str(runbook.get("excerpt") or "").strip()
        excerpt_only = bool(body)

    if not body:
        header.append(
            f"No runbook body was retrieved. Call {_fetch_hint(runbook)} to read it."
        )
        return "\n".join(header)

    header_text = "\n".join(header)
    body_budget = budget - len(header_text) - 2
    if excerpt_only or len(body) > body_budget:
        # A notice is going to be appended; pay for it out of the same budget.
        body_budget -= _NOTICE_ALLOWANCE
    # The floor wins over the budget: a brief too small to hold one procedure
    # step is not worth emitting, so a very tight budget overshoots rather
    # than degrading to a header and an ellipsis.
    body_budget = max(body_budget, _MIN_BRIEF_CHARS // 2)

    kept_body, dropped = _fit_sections(body, body_budget)

    parts = [header_text, "", kept_body]
    if excerpt_only:
        parts.append(
            f"\n[This is a keyword excerpt, not the full runbook. Call "
            f"{_fetch_hint(runbook)} before concluding the procedure is complete.]"
        )
    elif dropped:
        named = ", ".join(dropped[:6]) + ("…" if len(dropped) > 6 else "")
        parts.append(
            f"\n[{len(dropped)} lower-priority section(s) omitted to fit the "
            f"context budget: {named}. Call {_fetch_hint(runbook)} for the full "
            "document.]"
        )
    return "\n".join(parts).strip()


def _fit_sections(body: str, budget: int) -> Tuple[str, List[str]]:
    """Keep as much of ``body`` as fits, dropping whole low-priority sections.

    Returns the kept text in document order plus the headings dropped. Dropping
    is by priority and never by position: budgeting head-first spends the whole
    allowance on "Summary" and "Background" and truncates exactly where the
    remediation steps start.
    """
    if len(body) <= budget:
        return body, []

    sections = split_sections(body)
    if not sections:
        return _elide_middle(body, budget), []

    rendered = [
        (index, heading, _render_section(heading, text))
        for index, (heading, text) in enumerate(sections)
    ]
    order = sorted(
        rendered,
        key=lambda item: (section_priority(item[1]), item[0]),
    )

    keep: Dict[int, str] = {}
    dropped: List[str] = []
    used = 0
    for index, heading, text in order:
        cost = len(text) + 1
        if used + cost <= budget:
            keep[index] = text
            used += cost
            continue
        # The highest-priority section is the procedure itself. If it alone
        # overflows, shrink it rather than dropping it — a trimmed set of steps
        # with an explicit elision marker still beats no steps at all.
        remaining = budget - used
        if not keep and remaining >= _MIN_BRIEF_CHARS // 2:
            keep[index] = _elide_middle(text, remaining)
            used = budget
            continue
        dropped.append(heading or "(preamble)")

    kept_text = "\n\n".join(keep[index] for index in sorted(keep))
    return kept_text.strip(), dropped


def _render_section(heading: str, text: str) -> str:
    if heading and text:
        return f"## {heading}\n{text}"
    if heading:
        return f"## {heading}"
    return text


def _elide_middle(text: str, budget: int) -> str:
    """Keep both ends of an oversized section.

    A procedure's tail routinely holds the verification step and the escalation
    path, so a plain head slice drops the half that says whether the fix
    worked.
    """
    if len(text) <= budget:
        return text
    marker = "\n… [section trimmed to fit the context budget] …\n"
    room = max(budget - len(marker), 200)
    head = room // 2
    tail = room - head
    return text[:head] + marker + (text[-tail:] if tail else "")


# ---------------------------------------------------------------------------
# Fallback when no page body is reachable
# ---------------------------------------------------------------------------


def summarize_search_results(
    results: Sequence[Dict[str, Any]],
    *,
    limit: int = 3,
    max_chars: Optional[int] = None,
) -> str:
    """List candidate runbooks when none of their bodies could be fetched.

    Names them and says how to read them, so a content-fetch failure degrades
    to "here is where the answer is" rather than to silence.
    """
    hits = [hit for hit in results if isinstance(hit, dict)][:limit]
    if not hits:
        return ""
    budget = max_chars if max_chars is not None else brief_max_chars()
    lines = [
        f"{len(hits)} candidate runbook(s) matched this alert; none of their "
        "bodies could be retrieved. Read the most relevant one before starting "
        "open-ended discovery:"
    ]
    for hit in hits:
        title = str(hit.get("title") or "Untitled runbook").strip()
        properties = _property_line(hit)
        suffix = f" ({properties})" if properties else ""
        lines.append(f"- {title}{suffix} — {_fetch_hint(hit)}")
        excerpt = str(hit.get("excerpt") or "").strip()
        if excerpt:
            lines.append(f"    excerpt: {excerpt}")
    text = "\n".join(lines)
    return text if len(text) <= budget else _elide_middle(text, budget)
