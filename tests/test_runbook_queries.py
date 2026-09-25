#!/usr/bin/env python3
"""The runbook's own PromQL has to reach the Prometheus specialist.

The graded ``inventory_slow_queries`` trial is the whole reason this module
exists. The specialist opened with ``get_golden_signals``, which is assembled
from the cluster's single configured ``latency_histogram``, so it returned
``http_request_duration_seconds_bucket`` — a healthy series for a database
incident. ``db_query_duration_seconds_bucket``, named three times by the
runbook and probed by the recovery oracle, was never queried, and the trial
recorded a non-recovery for want of one tool argument.

Nothing in the pipeline converted a runbook-named metric into an executed
query: ``metrics_profile`` holds exactly one histogram per cluster, and the
metrics prompt's promised "metric hint from the task brief" was produced by
no code at all.

These tests hold the extractor that closes that gap, and the last one holds
the real Meridian runbook end to end through the brief the agent is handed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sre_agent.runbook_brief import render_runbook_brief
from sre_agent.runbook_queries import (
    extract_metric_names,
    extract_promql,
    is_promql,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

FENCED = """
## Step 2 — Is the database the source?

For InventorySlowQueries use the database histogram instead:

```
histogram_quantile(0.90, sum by (le) (rate(db_query_duration_seconds_bucket{job="inventory-service"}[5m])))
```

If it is above **1.0 s** the queries are the cause.
"""


def test_the_fenced_query_is_returned_verbatim():
    assert extract_promql(FENCED) == [
        "histogram_quantile(0.90, sum by (le) "
        '(rate(db_query_duration_seconds_bucket{job="inventory-service"}[5m])))'
    ]


def test_a_fence_with_one_query_per_line_yields_two_queries():
    # Joining them produces a string with balanced parentheses that looks
    # runnable and is not — the failure mode that made this rule necessary.
    text = """
```
histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{service="payment-service"}[5m])))
sum(rate(http_errors_total{service="payment-service"}[5m]))
```
"""
    found = extract_promql(text)

    assert len(found) == 2
    assert found[0].startswith("histogram_quantile(0.95")
    assert found[1] == 'sum(rate(http_errors_total{service="payment-service"}[5m]))'


def test_a_query_wrapped_across_lines_stays_one_query():
    text = """
```
histogram_quantile(0.95,
  sum by (le) (rate(http_request_duration_seconds_bucket{service="checkout-service"}[5m]))
)
```
"""
    found = extract_promql(text)

    assert len(found) == 1
    assert found[0].count("histogram_quantile") == 1
    assert found[0].endswith(")")


def test_an_inline_table_cell_is_a_query_too():
    text = (
        "| E (inventory queries) | `histogram_quantile(0.90, sum by (le) "
        '(rate(db_query_duration_seconds_bucket{job="inventory-service"}[5m])))`'
        " | `< 1.0` |"
    )

    assert extract_promql(text) == [
        "histogram_quantile(0.90, sum by (le) "
        '(rate(db_query_duration_seconds_bucket{job="inventory-service"}[5m])))'
    ]


def test_a_templated_query_is_never_handed_over():
    # An unfilled placeholder returns no data, and this block tells the
    # specialist that no data for the runbook's metric is itself a finding.
    # Handing it a query guaranteed to return nothing manufactures that
    # finding.
    text = (
        "```\n"
        "histogram_quantile(0.95, sum by (le) "
        '(rate(http_request_duration_seconds_bucket{service="<service>"}[5m])))\n'
        "```"
    )

    assert extract_promql(text) == []


@pytest.mark.parametrize(
    "candidate",
    [
        '{app="checkout-service"} |= "db_pool_exhausted"',
        'sum by (level) (count_over_time({app="checkout"} | json [5m]))',
    ],
)
def test_logql_is_not_promql(candidate):
    assert is_promql(candidate) is False


@pytest.mark.parametrize(
    "candidate",
    [
        "kubectl scale deployment/checkout-service --replicas=4",
        "$ promtool query instant http://prom:9090 'rate(http_requests_total[5m])'",
        "SELECT count(*) FROM orders",
    ],
)
def test_shell_and_sql_are_not_promql(candidate):
    assert is_promql(candidate) is False


@pytest.mark.parametrize(
    "candidate",
    [
        "histogram_quantile(0.9, sum by (le) (rate(db_query_duration_seconds_bucket[5m]",
        "p95 latency stays under 1.5 s",
        "`< 1.0`",
        "sum(rate(up[5m]))",
    ],
)
def test_fragments_thresholds_and_metricless_expressions_are_rejected(candidate):
    assert is_promql(candidate) is False


def test_tool_names_are_not_mistaken_for_metrics():
    text = (
        "Call `get_golden_signals` first, then `query_logs`, and check "
        "`analyze_log_patterns` output."
    )

    assert extract_metric_names(text) == []


def test_a_named_metric_is_found_without_a_query_around_it():
    text = "| inventory-service db p90 | `db_query_duration_seconds_bucket` | **1.0 s** |"

    assert extract_metric_names(text) == ["db_query_duration_seconds_bucket"]


def test_limits_bound_what_a_brief_can_grow_by():
    text = "\n".join(
        f"```\nsum(rate(metric_{i}_total{{job=\"svc\"}}[5m]))\n```" for i in range(20)
    )

    assert len(extract_promql(text)) == 4
    assert len(extract_promql(text, limit=2)) == 2
    assert len(extract_metric_names(text)) == 6


def test_the_real_meridian_runbook_offers_the_histogram_the_oracle_probes():
    """The regression that produced 0 of 12 recoveries, held end to end.

    Not the raw file: the budgeted, remediation-first brief that
    ``ContextBuilder`` actually renders and puts in front of the specialist.
    """
    content = (REPO_ROOT / "examples" / "meridian" / "runbooks" / "high-latency.md").read_text(
        encoding="utf-8"
    )
    brief = render_runbook_brief(
        {
            "title": "High Latency",
            "alert_name": "InventorySlowQueries",
            "service": "inventory-service",
        },
        content,
    )

    queries = extract_promql(brief)

    assert queries, "the rendered brief must still carry a runnable query"
    assert any("db_query_duration_seconds_bucket" in query for query in queries)
    # And it comes first: the specialist reads top-down under a turn budget.
    assert "db_query_duration_seconds_bucket" in queries[0]
    assert all("<" not in query for query in queries)
