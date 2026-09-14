from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest

from backend import crud, models
from sre_agent.incident_timeline import (
    build_specialist_finding_content,
    build_supervisor_summary_content,
    truncate_for_timeline,
)


def test_truncate_for_timeline_leaves_short_text_untouched():
    assert truncate_for_timeline("short diff", limit=4000) == "short diff"


def test_truncate_for_timeline_caps_long_text():
    text = "x" * 5000
    result = truncate_for_timeline(text, limit=4000)
    assert result.startswith("x" * 4000)
    assert "truncated, 1000 more characters" in result


class FakeDb:
    def __init__(self, incident=None, timeline_event=None):
        self.incident = incident or SimpleNamespace(summary=None)
        self.timeline_event = timeline_event
        self.added = []
        self.committed = 0
        self.refreshed = []

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed += 1

    async def refresh(self, obj):
        self.refreshed.append(obj)

    async def get(self, model, obj_id):
        if model is models.Incident:
            return self.incident
        if model is models.IncidentTimelineEvent:
            return self.timeline_event
        return None


@pytest.mark.asyncio
async def test_create_incident_timeline_event_persists_pending_fields(monkeypatch):
    async def fake_sequence(db, incident_id):
        return 7

    monkeypatch.setattr(crud, "_get_next_timeline_sequence", fake_sequence)

    fake_db = FakeDb()
    incident_id = uuid.uuid4()
    event = await crud.create_incident_timeline_event(
        fake_db,
        incident_id,
        event_type="human_message",
        speaker_role="user",
        title="You",
        content="What changed?",
        payload={"source": "dashboard_chat", "mode": "post_summary_follow_up"},
        pending_supervisor=True,
    )

    assert event.sequence == 7
    assert event.pending_supervisor is True
    assert event.handled_at is None
    assert fake_db.added == [event]
    assert fake_db.committed == 1


@pytest.mark.asyncio
async def test_create_incident_timeline_event_updates_incident_summary(monkeypatch):
    async def fake_sequence(db, incident_id):
        return 1

    monkeypatch.setattr(crud, "_get_next_timeline_sequence", fake_sequence)

    incident = SimpleNamespace(summary=None)
    fake_db = FakeDb(incident=incident)

    event = await crud.create_incident_timeline_event(
        fake_db,
        uuid.uuid4(),
        event_type="summary",
        speaker_role="supervisor",
        title="Supervisor",
        content="Incident resolved.",
        payload={"source": "test"},
    )

    assert event.sequence == 1
    assert incident.summary == "Incident resolved."


@pytest.mark.asyncio
async def test_mark_incident_timeline_event_handled_clears_pending_flag():
    event = models.IncidentTimelineEvent(
        incident_id=uuid.uuid4(),
        sequence=1,
        event_type="human_message",
        speaker_role="user",
        title="You",
        content="Please check logs",
        pending_supervisor=True,
        handled_at=None,
    )
    fake_db = FakeDb(timeline_event=event)
    handled_at = datetime.now(timezone.utc)

    await crud.mark_incident_timeline_event_handled(fake_db, event.id, handled_at=handled_at)

    assert event.pending_supervisor is False
    assert event.handled_at == handled_at


def test_specialist_finding_normalization_rejects_placeholder_response():
    content, payload = build_specialist_finding_content(
        "logs_agent",
        "As the logs_agent, investigate: error rate spike",
        "Okay.",
    )

    assert payload["objective"] == "error rate spike"
    assert payload["evidence"] == "No concrete evidence was provided."
    assert payload["conclusion"] == "The specialist did not provide a concrete conclusion."
    assert payload["confidence"] == "low"
    assert "Loki Specialist finding" in content
    assert "objective: error rate spike" in content


def test_supervisor_summary_flags_conflicting_numeric_facts():
    alert_context = SimpleNamespace(
        alert_name="Checkout errors",
        severity="critical",
        annotations={
            "summary": "43.9% error rate",
            "description": "During the last 5 minutes the service stayed under load.",
        },
    )
    agent_results = {
        "metrics_agent": "Prometheus showed a 12.5% error rate in the same window.",
        "logs_agent": "Loki evidence pointed to 0.8% errors in that interval.",
    }

    content, payload = build_supervisor_summary_content(
        "Raw draft summary",
        agent_results,
        query="Investigate checkout errors",
        alert_context=alert_context,
    )

    assert "available facts are inconsistent" in content.lower()
    assert "43.9%" in content
    assert "12.5%" in content
    assert "0.8%" in content
    assert payload["conflicting_numeric_facts"]
    # The caveat is appended to the synthesis, never substituted for it.
    assert "Raw draft summary" in content


def test_a_conflict_caveat_does_not_delete_the_conclusion():
    """A wrap-up that says only "reconcile the data" throws away the root
    cause the specialists agreed on and contradicts the thread above it."""
    content, payload = build_supervisor_summary_content(
        "ignored when a narrative exists",
        {"metrics_agent": "error rate hit 12.5%", "logs_agent": "errors at 0.8%"},
        query="Investigate checkout errors",
        narrative="## TL;DR\nFault injection was left enabled on checkout-service.",
    )

    assert "Fault injection was left enabled on checkout-service." in content
    assert payload["conflicting_numeric_facts"]["error rate"]
    assert "Unreconciled figures" in content


def test_a_query_window_is_not_a_conflicting_latency_reading():
    """"p90 latency over the last 10 min" quotes one measurement and one
    window; counting the window as a second reading buried a correct root
    cause under "the available facts are inconsistent"."""
    content, payload = build_supervisor_summary_content(
        "",
        {
            "metrics_agent": "p90 query latency is 2.179s over the last 10 min",
            "logs_agent": "Slow DB query warnings throughout the 10 min window",
        },
        query="Investigate InventorySlowQueries",
        narrative="## TL;DR\nFault injection is enabled on inventory-service.",
    )

    assert payload["conflicting_numeric_facts"] == {}
    assert "Unreconciled figures" not in content
    assert "Fault injection is enabled on inventory-service." in content


def test_the_same_latency_spelled_two_ways_is_one_measurement():
    _content, payload = build_supervisor_summary_content(
        "",
        {
            "metrics_agent": "p99 latency reached 2000ms",
            "logs_agent": "response time of 2s on the same requests",
        },
        query="Investigate latency",
    )

    assert payload["conflicting_numeric_facts"] == {}


def test_genuinely_different_latency_readings_still_get_flagged():
    _content, payload = build_supervisor_summary_content(
        "",
        {
            "metrics_agent": "p99 latency reached 2000ms",
            "logs_agent": "response time of 9.4s on the same requests",
        },
        query="Investigate latency",
    )

    assert payload["conflicting_numeric_facts"]["latency"]


# --- Claims the alert asserts and a specialist only repeats ----------------
#
# d3ca5138, live: the rule's description says "(pod limit is 256Mi) — an
# OOMKill is imminent". The real limit is 768Mi and the container has never
# been OOMKilled. The Prometheus Specialist was handed that description and
# wrote it back as "well past the 256Mi pod limit ... lines up with an
# OOMKill", at which point it reached the narrator inside a block headed
# with a specialist's name and became, to the on-call, a measurement.
#
# Eight replays of that exact evidence through the deployed prompt — four
# before the narrator was warned about echoes and four after — asserted it
# every time. So the caveat is appended here, below the narration, where no
# model gets a vote.

_MEMORY_ALERT = SimpleNamespace(
    alert_name="CheckoutMemoryApproachingLimit",
    severity="critical",
    annotations={
        "summary": "checkout-service memory approaching pod limit",
        "description": (
            "checkout-service simulated heap is 226.1MiB, above 200MB "
            "(pod limit is 256Mi) — an OOMKill is imminent unless the leak "
            "is reverted."
        ),
    },
)

_ECHOING_FINDING = (
    "Confirmed the memory leak — process_resident_memory_bytes climbed from "
    "~101.8 MiB to a ~305.2 MiB plateau, well past the 256Mi pod limit, then "
    "froze flat, which lines up with an OOMKill."
)


def test_an_echoed_limit_and_consequence_are_flagged_as_unmeasured():
    content, _payload = build_supervisor_summary_content(
        "",
        {"metrics_agent": _ECHOING_FINDING},
        query="Investigate memory",
        alert_context=_MEMORY_ALERT,
        narrative="## TL;DR\nappears to have hit an OOMKill past the 256 Mi limit.",
    )

    assert "Carried over from the alert text, not measured" in content
    assert "256Mi" in content
    assert "OOMKill" in content
    assert "kubectl describe pod" in content


def test_the_echo_caveat_keeps_the_narration_above_it():
    """Same rule as the conflict caveat: a caveat annotates the conclusion,
    it never replaces it."""
    content, _payload = build_supervisor_summary_content(
        "",
        {"metrics_agent": _ECHOING_FINDING},
        query="Investigate memory",
        alert_context=_MEMORY_ALERT,
        narrative="## TL;DR\nA ValueError at app.py:147 is leaking memory.",
    )

    assert "A ValueError at app.py:147 is leaking memory." in content
    assert content.index("ValueError") < content.index("Carried over")


def test_nothing_is_flagged_when_the_specialists_measured_it_themselves():
    """The caveat must be rare enough to mean something. A specialist that
    never repeats the alert's figures gets no footer at all."""
    content, _payload = build_supervisor_summary_content(
        "",
        {
            "metrics_agent": (
                "process_resident_memory_bytes plateaued at 305.2 MiB; the "
                "deployment's limit is 768Mi, so there is plenty of headroom."
            )
        },
        query="Investigate memory",
        alert_context=_MEMORY_ALERT,
        narrative="## TL;DR\nMemory is elevated but nowhere near the limit.",
    )

    assert "Carried over from the alert text" not in content


def test_an_alert_with_no_findings_yet_gets_no_echo_caveat():
    content, _payload = build_supervisor_summary_content(
        "",
        {},
        query="Investigate memory",
        alert_context=_MEMORY_ALERT,
        narrative="## TL;DR\nNo specialist evidence came back.",
    )

    assert "Carried over from the alert text" not in content