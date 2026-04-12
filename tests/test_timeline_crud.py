from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest

from backend import crud, models
from sre_agent.incident_timeline import build_specialist_finding_content, build_supervisor_summary_content


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