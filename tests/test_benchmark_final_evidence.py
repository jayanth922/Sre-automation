"""The transcript is graded only after the investigation has finished.

E2E Run 5 (2026-09-30): the alert cleared by itself mid-investigation, the
incident became `resolved`, and the harness read the transcript before the
summary and ACT events were written.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from test_statistical_recording import _load_runner


class _Response:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class _Jwt:
    async def headers(self):
        return {}


class _Investigation:
    """An API whose summary lands only once the root span is finalized."""

    def __init__(self, polls_until_finished: int):
        self.remaining = polls_until_finished
        self.finished = False
        self.metric_polls = 0

    async def get(self, url, headers=None):
        if url.endswith("/agent-metrics"):
            self.metric_polls += 1
            if self.remaining > 0:
                self.remaining -= 1
                reasons = ["root_span_not_finalized"]
            else:
                self.finished = True
                reasons = []
            return _Response(
                {
                    "trace_completeness": {
                        "root_trace_id": "r1",
                        "complete": not reasons,
                        "completeness_reasons": reasons,
                    }
                }
            )
        events = [{"event_type": "plan"}]
        if self.finished:
            events += [{"event_type": "summary"}, {"event_type": "act"}]
        return _Response({"events": events})


def _runner(monkeypatch, tmp_path, **env):
    runner = _load_runner(monkeypatch, tmp_path, **env)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(runner.asyncio, "sleep", _no_sleep)
    return runner


def _collect(runner, api):
    return asyncio.run(
        runner._collect_final_evidence(
            api, _Jwt(), "inc-1", SimpleNamespace(base_url="http://api")
        )
    )


def test_the_transcript_is_read_after_the_root_span_finishes(monkeypatch, tmp_path):
    # A zero accounting wait proves the running-root bound is its own setting.
    runner = _runner(monkeypatch, tmp_path, BENCH_ACCOUNTING_WAIT_SECONDS="0")
    api = _Investigation(polls_until_finished=3)

    trace, transcript = _collect(runner, api)

    assert trace["complete"] is True
    assert [e["event_type"] for e in transcript["events"]] == ["plan", "summary", "act"]


def test_a_root_that_never_finishes_is_bounded(monkeypatch, tmp_path):
    runner = _runner(monkeypatch, tmp_path, BENCH_INVESTIGATION_SETTLE_SEC="0")
    api = _Investigation(polls_until_finished=10**6)

    trace, _ = _collect(runner, api)

    assert api.metric_polls == 1
    assert trace["completeness_reasons"] == ["root_span_not_finalized"]
