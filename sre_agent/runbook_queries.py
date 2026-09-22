"""Pull the PromQL a runbook names out of the runbook, with no model call.

An alert's runbook is the operator's own answer, and for a latency alert that
answer is usually a specific expression over a specific histogram. The brief
already carries the runbook text; nothing turned the query inside it into a
query the agent ran. In the graded ``inventory_slow_queries`` trial the
Prometheus specialist opened with ``get_golden_signals``, got the cluster's one
configured latency histogram (``http_request_duration_seconds_bucket``), and
never touched ``db_query_duration_seconds_bucket`` — the metric the runbook
names in three places and the metric the recovery oracle probes.

That gap is structural, not a prompting failure: ``metrics_profile`` stores
exactly one ``latency_histogram`` per cluster and both golden-signal latency
queries interpolate it, so ``get_golden_signals`` *cannot* return the
alert-specific histogram. The fix is to quote the runbook's own expressions
back to the specialist as things to run.

This module is deliberately pure text matching. Asking a model to extract the
query would cost a model call per specialist lane; scanning the markdown costs
nothing and cannot invent a metric that is not written down.

Everything here treats the runbook as untrusted content — callers still wrap
the output with ``prompt_guard.wrap_untrusted`` before it reaches a model.
"""

from __future__ import annotations

import re
from typing import List

# Where a runbook that shows a query at all shows it.
_FENCE = re.compile(r"```[a-zA-Z0-9_+.-]*\n(.*?)```", re.DOTALL)
# And the table form: "| inventory-service db p90 |
# `db_query_duration_seconds_bucket` | **1.0 s** |".
_INLINE = re.compile(r"`([^`\n]{4,400})`")
_BLANK_LINE = re.compile(r"\n\s*\n")
_WHITESPACE = re.compile(r"\s+")

# An expression worth handing back names a metric and does something with it.
# Requiring a call keeps prose, thresholds and bare metric names out of the
# query list; bare names are reported separately by extract_metric_names.
_PROMQL_CALL = re.compile(
    r"\b(?:histogram_quantile|rate|irate|increase|sum|avg|min|max|count"
    r"|count_over_time|avg_over_time|max_over_time|min_over_time|sum_over_time"
    r"|topk|bottomk|clamp_min|clamp_max|quantile|stddev|stdvar|absent"
    r"|absent_over_time|delta|idelta|deriv|predict_linear)\s*\("
)
# A metric name carries underscores and is not itself being called.
_IDENTIFIER = re.compile(r"\b([a-zA-Z_:][a-zA-Z0-9_:]*_[a-zA-Z0-9_:]+)\b(?!\s*\()")
# Runbooks name tools as often as they name metrics, and a tool name is
# underscore-separated too. Tools are verbs; metrics are nouns.
_TOOL_VERB = re.compile(
    r"^(?:get|list|query|search|analyze|run|create|apply|set|check|fetch"
    r"|describe|scale|restart|rollback|propose|record)_",
    re.IGNORECASE,
)
# Conventional Prometheus suffixes, for names with only one underscore.
_METRIC_SUFFIX = re.compile(
    r"_(?:total|count|sum|bucket|seconds|bytes|ratio|percent|info|errors"
    r"|requests|connections|size|usage|utilization|fds|threads|celsius"
    r"|milliseconds|microseconds)$"
)
# LogQL reads enough like PromQL to be picked up by accident, and a LogQL
# string handed to get_metric is a wasted turn.
_LOGQL = re.compile(
    r"\|=|\|~|\|\s*(?:json|logfmt|pattern|unwrap|line_format|label_format)\b"
)
# A templated query run verbatim returns nothing, and "no data for the
# runbook's own metric" is exactly the finding this block tells the
# specialist to trust. Never hand over an unfilled placeholder.
_PLACEHOLDER = re.compile(r"<[A-Za-z_][A-Za-z0-9_.-]*>|\$\{[^}]*\}")
# Shell, SQL and kubectl are the other things runbooks fence.
_NOT_A_QUERY = re.compile(
    # The sigils sit outside the \b group on purpose: a shell prompt is
    # followed by a space, and "$ " is not a word boundary.
    r"^\s*(?:[$#>]|(?:kubectl|curl|psql|helm|docker|promtool|awk|grep|git"
    r"|SELECT|EXPLAIN|WITH)\b)",
    re.IGNORECASE,
)

DEFAULT_QUERY_LIMIT = 4
DEFAULT_METRIC_LIMIT = 6
MAX_QUERY_CHARS = 400


def _is_self_contained(line: str) -> bool:
    """A line that is a whole expression rather than part of one."""
    return (
        bool(_PROMQL_CALL.search(line))
        and line.count("(") == line.count(")")
        and line.count("{") == line.count("}")
    )


def _fence_candidates(block: str) -> List[str]:
    """Split a fenced block the way its author meant it.

    Blank lines separate statements. So does a plain newline, but only when
    every line in the group is a whole expression on its own: the Meridian
    high-latency runbook fences the payment p95 and the payment error ratio
    on consecutive lines, and joining them yields a string that is balanced,
    looks runnable, and is not a query. A genuinely wrapped expression has
    unbalanced lines, so it stays joined.
    """
    found: List[str] = []
    for chunk in _BLANK_LINE.split(block):
        if not chunk.strip():
            continue
        lines = [line.strip() for line in chunk.splitlines() if line.strip()]
        if len(lines) > 1 and all(_is_self_contained(line) for line in lines):
            found.extend(lines)
        else:
            found.append(chunk)
    return found


def _candidate_strings(text: str) -> List[str]:
    """Every fenced statement and inline span, whitespace collapsed."""
    body = text or ""
    found: List[str] = []
    for block in _FENCE.findall(body):
        found.extend(_fence_candidates(block))
    # Strip the fences before scanning inline spans, so a fenced query is not
    # re-found in pieces.
    for span in _INLINE.findall(_FENCE.sub("\n", body)):
        found.append(span)
    return [_WHITESPACE.sub(" ", chunk).strip() for chunk in found]


def _metric_tokens(text: str) -> List[str]:
    """Identifiers in this string that are shaped like a Prometheus metric."""
    tokens: List[str] = []
    for token in _IDENTIFIER.findall(text):
        if len(token) < 5 or _TOOL_VERB.match(token):
            continue
        # Two underscores, or one plus a conventional unit suffix. Conservative
        # on purpose: a missed hint costs nothing, a wrong one costs a turn.
        if token.count("_") >= 2 or _METRIC_SUFFIX.search(token):
            tokens.append(token)
    return tokens


def is_promql(text: str, *, max_chars: int = MAX_QUERY_CHARS) -> bool:
    """Whether this string is safe to quote back to a specialist as runnable."""
    candidate = (text or "").strip()
    if not candidate or len(candidate) > max_chars:
        return False
    if _NOT_A_QUERY.match(candidate) or _LOGQL.search(candidate):
        return False
    if _PLACEHOLDER.search(candidate):
        return False
    if not _PROMQL_CALL.search(candidate):
        return False
    if not _metric_tokens(candidate):
        return False
    # A fragment of an expression reads like a whole one and then fails at the
    # Prometheus API, costing the turn it was supposed to save.
    return candidate.count("(") == candidate.count(")") and candidate.count(
        "{"
    ) == candidate.count("}")


def extract_promql(text: str, *, limit: int = DEFAULT_QUERY_LIMIT) -> List[str]:
    """Runnable PromQL the runbook states, de-duplicated, in document order."""
    seen = set()
    found: List[str] = []
    for candidate in _candidate_strings(text):
        if candidate in seen or not is_promql(candidate):
            continue
        seen.add(candidate)
        found.append(candidate)
        if len(found) >= max(int(limit), 0):
            break
    return found


def extract_metric_names(text: str, *, limit: int = DEFAULT_METRIC_LIMIT) -> List[str]:
    """Metric names the runbook states, whether or not wrapped in a query.

    Threshold tables name the histogram on its own, and for an alert whose
    metric is not the cluster's configured golden signal that table row is
    often the only place the right metric appears.
    """
    seen = set()
    found: List[str] = []
    for candidate in _candidate_strings(text):
        if _NOT_A_QUERY.match(candidate) or _LOGQL.search(candidate):
            continue
        for token in _metric_tokens(candidate):
            if token in seen:
                continue
            seen.add(token)
            found.append(token)
            if len(found) >= max(int(limit), 0):
                return found
    return found
