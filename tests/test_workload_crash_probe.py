#!/usr/bin/env python3
"""Tests for the crash probe that stops an OOMKill from reading as recovery."""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sre_agent.workload_crash_probe import probe_workload_crash  # noqa: E402

OPENED = datetime(2026, 9, 29, 21, 17, 0, tzinfo=timezone.utc)


def _fake(stamps, reasons, calls=None):
    async def query(promql):
        if calls is not None:
            calls.append(promql)
        if "last_terminated_timestamp" in promql:
            return stamps
        return reasons

    return query


def _stamp(pod, at):
    return {"metric": {"pod": pod}, "value": [0, str(at.timestamp())]}


def _run(**kwargs):
    base = {"prometheus_url": "http://prom", "service": "checkout-service", "since": OPENED}
    base.update(kwargs)
    return asyncio.run(
        probe_workload_crash(
            base.pop("prometheus_url"), base.pop("service"), base.pop("since"), **base
        )
    )


def test_an_oomkill_after_the_incident_opened_is_a_crash():
    killed = OPENED + timedelta(minutes=6)
    calls = []
    probe = _run(
        namespace="meridian",
        query=_fake(
            [_stamp("checkout-a", killed), _stamp("checkout-b", OPENED - timedelta(hours=3))],
            [{"metric": {"pod": "checkout-a", "reason": "OOMKilled"}}],
            calls,
        ),
    )
    assert probe.state == "crashed"
    assert probe.crash.pod == "checkout-a"
    assert probe.crash.reason == "OOMKilled"
    assert probe.crash.terminated_at == killed
    assert all('container="checkout-service",namespace="meridian"' in c for c in calls)


def test_a_termination_from_before_the_incident_is_not_this_crash():
    probe = _run(
        query=_fake([_stamp("checkout-a", OPENED - timedelta(minutes=30))], []),
    )
    assert probe.state == "none"
    assert probe.crash is None


def test_a_replaced_pod_has_no_termination_and_reads_as_no_crash():
    # A rollout restart replaces pods; the new ones carry no termination series.
    probe = _run(query=_fake([], []))
    assert probe.state == "none"


def test_a_failing_prometheus_is_unavailable_not_a_verdict():
    async def broken(promql):
        raise ConnectionError("down")

    assert _run(query=broken).state == "unavailable"


def test_label_values_that_are_not_kubernetes_names_are_never_interpolated():
    calls = []
    probe = _run(service='x"} or vector(1) #', query=_fake([], [], calls))
    assert probe.state == "unavailable"
    assert calls == []


def test_without_a_start_time_or_prometheus_there_is_no_verdict():
    assert _run(since=None, query=_fake([], [])).state == "unavailable"
    assert _run(prometheus_url=None).state == "unavailable"
