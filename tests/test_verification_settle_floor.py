#!/usr/bin/env python3
"""A rollout must not be able to grade itself.

`verify_alert_cleared` polled patiently while an alert was still firing but
believed a *clear* reading the instant it saw one, and `verify_live`'s default
settle is 0 — so the first sample landed as soon as the write returned.

For the alerts this platform actually remediates that is not a weak signal,
it is a guaranteed false positive. `ALERTS` carries the `pod` label, so
deleting the old pod removes the firing series immediately, while the
replacement has not had time to fail yet. Live on 2026-09-14, incident
d2fb7c5d was recorded "RESOLVED (alert PodCrashLooping is no longer firing
after 0s)" — a verdict measured before the new pod existed, which any rollout
would have earned whether or not the fix worked. The skill store learns from
these verdicts, so a false RESOLVED teaches the wrong remediation.
"""

from __future__ import annotations

import pytest

from sre_agent.verification import verify_alert_cleared

# The shape the Prometheus MCP really returns — it wraps the vector to cap
# series count (see parse_prom_result).
_FIRING = {
    "result": [{"metric": {"alertname": "PodCrashLooping"}, "value": [0, "1"]}],
    "series_total": 1,
}
_CLEAR = {"result": [], "series_total": 0}


class _Clock:
    """A tool caller reading a scripted timeline, with sleeps that don't sleep."""

    def __init__(self, samples):
        self._samples = list(samples)
        self.elapsed = 0.0
        self.calls = 0

    async def caller(self, _tool, _args):
        self.calls += 1
        return self._samples[min(self.calls - 1, len(self._samples) - 1)]

    async def sleep(self, seconds):
        self.elapsed += seconds


@pytest.mark.asyncio
async def test_a_pod_that_just_vanished_is_not_a_fix():
    """The exact d2fb7c5d shape: clear at t=0, firing again once it restarts."""
    clock = _Clock([_CLEAR, _FIRING, _FIRING, _FIRING, _FIRING, _FIRING, _FIRING])

    outcome = await verify_alert_cleared(
        "PodCrashLooping",
        "ocr-extractor",
        clock.caller,
        timeout_seconds=180,
        poll_seconds=30,
        min_clear_seconds=180,
        sleep=clock.sleep,
    )

    assert outcome.status == "FAILED"
    assert "still firing" in outcome.detail


@pytest.mark.asyncio
async def test_a_fix_that_holds_through_the_floor_is_resolved():
    clock = _Clock([_CLEAR])

    outcome = await verify_alert_cleared(
        "PodCrashLooping",
        "ocr-extractor",
        clock.caller,
        timeout_seconds=600,
        poll_seconds=30,
        min_clear_seconds=180,
        sleep=clock.sleep,
    )

    assert outcome.status == "RESOLVED"
    # Believed only after the floor, not on the first sample.
    assert clock.elapsed >= 180
    assert "after 180s" in outcome.detail


@pytest.mark.asyncio
async def test_an_alert_that_clears_late_is_still_resolved():
    """The floor must not cost us the false-negative protection it replaced."""
    clock = _Clock([_FIRING] * 8 + [_CLEAR])

    outcome = await verify_alert_cleared(
        "PodCrashLooping",
        "ocr-extractor",
        clock.caller,
        timeout_seconds=600,
        poll_seconds=30,
        min_clear_seconds=180,
        sleep=clock.sleep,
    )

    assert outcome.status == "RESOLVED"


@pytest.mark.asyncio
async def test_the_floor_never_outlives_the_budget():
    """A short budget must not turn a genuinely clear alert into a failure.

    Clamped, not honoured literally: otherwise the loop polls to the deadline
    with the floor never reached and reports FAILED on an alert that was
    clear at every single sample.
    """
    clock = _Clock([_CLEAR])

    outcome = await verify_alert_cleared(
        "PodCrashLooping",
        "ocr-extractor",
        clock.caller,
        timeout_seconds=60,
        poll_seconds=30,
        min_clear_seconds=600,
        sleep=clock.sleep,
    )

    assert outcome.status == "RESOLVED"
    assert clock.elapsed <= 60


@pytest.mark.asyncio
async def test_an_unreachable_prometheus_is_still_not_a_verdict():
    async def broken(_tool, _args):
        raise RuntimeError("connection refused")

    outcome = await verify_alert_cleared(
        "PodCrashLooping", "ocr-extractor", broken, min_clear_seconds=180
    )

    assert outcome.status == "UNKNOWN"
