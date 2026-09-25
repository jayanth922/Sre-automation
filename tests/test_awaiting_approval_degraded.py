#!/usr/bin/env python3
"""#75 -- "nothing is waiting" and "I could not tell" are different answers.

`get_awaiting_approval` wrapped its whole body in a bare `except` and returned
a count of zero on any failure. The checkpointer is *where* "paused for
approval" is stored, so when it is unreachable the honest answer is that the
count is unknown -- but zero is what a healthy, idle cluster reports too, and
the console cannot tell the two apart. The rail then drops the approval cue on
incidents that really are paused, which is the one state that cannot wait to be
noticed: nothing moves until a human approves, and the platform has just
stopped saying so.

This is the same fail-open shape as #35 and #39: an exception swallowed into a
value that reads as a clean result.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from backend import models
from sre_agent.api.v1 import clusters


class _Incident:
    def __init__(self, status=models.IncidentStatus.INVESTIGATING):
        self.id = uuid.uuid4()
        self.status = status


class _State:
    def __init__(self, interrupted: bool):
        task = type("T", (), {"interrupts": ("approve?",) if interrupted else ()})()
        self.tasks = (task,)


class _Graph:
    """A checkpointer whose per-thread reads can be told to fail."""

    def __init__(self, interrupted=(), failing=()):
        self._interrupted = {str(i) for i in interrupted}
        self._failing = {str(i) for i in failing}

    async def aget_state(self, config):
        thread = config["configurable"]["thread_id"]
        if thread in self._failing:
            raise RuntimeError("checkpointer connection lost")
        return _State(thread in self._interrupted)


def _call(monkeypatch, incidents, graph_factory):
    monkeypatch.setattr(
        clusters.crud,
        "get_incidents_for_cluster",
        lambda db, cluster_id: _async(incidents),
    )
    from sre_agent.api.v1 import mission_control

    monkeypatch.setattr(
        mission_control, "get_agent_graph", lambda cluster_id: graph_factory()
    )
    return asyncio.run(
        clusters.get_awaiting_approval(
            cluster_id=uuid.uuid4(), user=object(), db=object(), owned_cluster=object()
        )
    )


async def _async(value):
    return value


def test_a_paused_incident_is_reported_and_the_answer_is_not_degraded(monkeypatch):
    incidents = [_Incident(), _Incident()]
    paused = incidents[1].id

    result = _call(monkeypatch, incidents, lambda: _async_graph(interrupted=[paused]))

    assert result["incident_ids"] == [str(paused)]
    assert result["count"] == 1
    assert result["degraded"] is False
    assert result["unchecked"] == 0
    assert result["checked"] == 2


def test_an_unreachable_checkpointer_reports_unknown_not_zero(monkeypatch):
    """The defect: this used to be indistinguishable from an idle cluster."""
    incidents = [_Incident(), _Incident()]

    async def _boom(cluster_id):
        raise RuntimeError("no checkpointer configured")

    result = _call(monkeypatch, incidents, lambda: _boom(None))

    assert result["count"] == 0
    assert result["degraded"] is True
    assert result["unchecked"] == 2
    assert result["checked"] == 0


def test_one_unreadable_thread_does_not_pass_as_a_clean_zero(monkeypatch):
    """A partial answer is still partial, and has to say so.

    The endpoint keeps working -- one bad thread must not cost the others --
    but the count it returns is a floor, not the answer.
    """
    incidents = [_Incident(), _Incident(), _Incident()]

    result = _call(
        monkeypatch,
        incidents,
        lambda: _async_graph(
            interrupted=[incidents[0].id], failing=[incidents[2].id]
        ),
    )

    assert result["incident_ids"] == [str(incidents[0].id)]
    assert result["count"] == 1
    assert result["unchecked"] == 1
    assert result["checked"] == 2
    assert result["degraded"] is True


def test_resolved_incidents_are_never_scanned(monkeypatch):
    """Only a non-resolved incident can be paused, and the scan is capped."""
    incidents = [_Incident(models.IncidentStatus.RESOLVED) for _ in range(3)]

    result = _call(monkeypatch, incidents, lambda: _async_graph())

    assert result["count"] == 0
    assert result["checked"] == 0
    assert result["degraded"] is False


async def _async_graph(interrupted=(), failing=()):
    return _Graph(interrupted=interrupted, failing=failing)
