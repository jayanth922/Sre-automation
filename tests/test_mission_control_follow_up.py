from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest

from backend import models, schemas
from sre_agent.api.v1 import mission_control


class FakeScalarResult:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return FakeScalarResult(self.value)


class FakeDb:
    def __init__(self, incident):
        self.incident = incident

    async def execute(self, stmt):
        return FakeResult(self.incident)


@pytest.mark.asyncio
async def test_closed_incident_follow_up_queues_same_thread(monkeypatch):
    incident_id = uuid.uuid4()
    cluster_id = uuid.uuid4()
    org_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), org_id=org_id)
    incident = SimpleNamespace(
        id=incident_id,
        cluster_id=cluster_id,
        status=models.IncidentStatus.RESOLVED,
        summary="Service recovered after a deploy rollback.",
        title="Checkout latency spike",
        description="Latency spike during checkout",
        resolved_at=None,
    )
    fake_db = FakeDb(incident)
    created_events = []
    scheduled = {}

    async def fake_create_event(
        db,
        created_incident_id,
        event_type,
        speaker_role,
        content,
        title=None,
        payload=None,
        pending_supervisor=False,
        handled_at=None,
    ):
        event = SimpleNamespace(id=uuid.uuid4())
        created_events.append(
            {
                "incident_id": created_incident_id,
                "event_type": event_type,
                "speaker_role": speaker_role,
                "content": content,
                "title": title,
                "payload": payload,
                "pending_supervisor": pending_supervisor,
            }
        )
        return event

    async def fake_get_cluster_by_id(db, requested_cluster_id):
        return SimpleNamespace(id=requested_cluster_id, org_id=org_id, name="cluster-a")

    def fake_create_task(coro):
        scheduled["coroutine"] = coro
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(mission_control.crud, "create_incident_timeline_event", fake_create_event)
    monkeypatch.setattr(mission_control.crud, "get_cluster_by_id", fake_get_cluster_by_id)
    monkeypatch.setattr(mission_control.asyncio, "create_task", fake_create_task)

    response = await mission_control.send_incident_message(
        str(incident_id),
        schemas.IncidentMessageRequest(message="What changed recently after the deploy?"),
        user=user,
        db=fake_db,
    )

    assert response["status"] == "FOLLOW_UP_QUEUED"
    assert response["conversation_mode"] == "assistant"
    assert created_events[0]["event_type"] == "human_message"
    assert created_events[0]["payload"]["mode"] == "post_summary_follow_up"
    assert "coroutine" in scheduled


@pytest.mark.asyncio
async def test_closed_incident_chat_only_follow_up_also_queues_same_thread(monkeypatch):
    incident_id = uuid.uuid4()
    cluster_id = uuid.uuid4()
    org_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), org_id=org_id)
    incident = SimpleNamespace(
        id=incident_id,
        cluster_id=cluster_id,
        status=models.IncidentStatus.RESOLVED,
        summary="Service recovered after a deploy rollback.",
        title="Checkout latency spike",
        description="Latency spike during checkout",
        resolved_at=None,
    )
    fake_db = FakeDb(incident)
    created_events = []
    scheduled = {}

    async def fake_create_event(
        db,
        created_incident_id,
        event_type,
        speaker_role,
        content,
        title=None,
        payload=None,
        pending_supervisor=False,
        handled_at=None,
    ):
        event = SimpleNamespace(id=uuid.uuid4())
        created_events.append(
            {
                "incident_id": created_incident_id,
                "event_type": event_type,
                "speaker_role": speaker_role,
                "content": content,
                "title": title,
                "payload": payload,
                "pending_supervisor": pending_supervisor,
            }
        )
        return event

    async def fake_get_cluster_by_id(db, requested_cluster_id):
        return SimpleNamespace(id=requested_cluster_id, org_id=org_id, name="cluster-a")

    def fake_create_task(coro):
        scheduled["coroutine"] = coro
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(mission_control.crud, "create_incident_timeline_event", fake_create_event)
    monkeypatch.setattr(mission_control.crud, "get_cluster_by_id", fake_get_cluster_by_id)
    monkeypatch.setattr(mission_control.asyncio, "create_task", fake_create_task)

    response = await mission_control.send_incident_message(
        str(incident_id),
        schemas.IncidentMessageRequest(message="Thanks"),
        user=user,
        db=fake_db,
    )

    assert response["status"] == "FOLLOW_UP_QUEUED"
    assert response["conversation_mode"] == "assistant"
    assert created_events[0]["event_type"] == "human_message"
    assert created_events[0]["payload"]["mode"] == "post_summary_follow_up"
    assert "coroutine" in scheduled


@pytest.mark.asyncio
async def test_post_summary_follow_up_reuses_same_thread_and_resets_turn_state(monkeypatch):
    incident_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), org_id=uuid.uuid4())
    captured = {}

    class FakeGraph:
        async def aget_state(self, config):
            return SimpleNamespace(
                values={
                    "metadata": {
                        "conversation_mode": "assistant",
                        "final_response": "## Summary\n- root cause: bad deploy",
                    },
                    "final_response": "## Summary\n- root cause: bad deploy",
                    "agents_invoked": ["metrics_agent"],
                    "agent_results": {"metrics_agent": "previous"},
                }
            )

        async def ainvoke(self, state, config):
            captured["state"] = state
            captured["config"] = config
            return {"final_response": "ok"}

    monkeypatch.setattr(mission_control, "get_agent_graph", lambda: FakeGraph())

    await mission_control._run_post_summary_follow_up(
        incident_id,
        "What changed recently after the deploy?",
        user,
    )

    assert captured["config"]["configurable"]["thread_id"] == str(incident_id)
    assert captured["state"]["current_query"] == "What changed recently after the deploy?"
    assert captured["state"]["metadata"]["conversation_mode"] == "assistant"
    assert captured["state"]["agent_results"] == {}
    assert captured["state"]["agents_invoked"] == []
    assert captured["state"]["current_specialist"] is None
    assert captured["state"]["final_response"] is None


def test_timeline_event_to_response_includes_pending_state():
    now = datetime.now(timezone.utc)
    event = SimpleNamespace(
        id=uuid.uuid4(),
        incident_id=uuid.uuid4(),
        sequence=5,
        event_type="human_message",
        speaker_role="user",
        title="You",
        content="What changed?",
        payload_json='{"source": "dashboard_chat", "mode": "post_summary_follow_up"}',
        pending_supervisor=True,
        handled_at=now,
        created_at=now,
    )

    response = mission_control._timeline_event_to_response(event)

    assert isinstance(response, schemas.IncidentTimelineEventResponse)
    assert response.payload == {"source": "dashboard_chat", "mode": "post_summary_follow_up"}
    assert response.pending_supervisor is True
    assert response.handled_at == now