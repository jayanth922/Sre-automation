import contextlib
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
async def test_closed_incident_chat_only_message_gets_immediate_reply(monkeypatch):
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

    async def fake_build_chat_reply(message, incident, cluster, org_langfuse=None):
        return "You're welcome!"

    async def fake_tracing_context(cluster_id):
        # This path is traced; resolving a real runtime here would open a DB
        # session whose cleanup shows up as a scheduled task below.
        return None

    monkeypatch.setattr(mission_control.crud, "create_incident_timeline_event", fake_create_event)
    monkeypatch.setattr(mission_control.crud, "get_cluster_by_id", fake_get_cluster_by_id)
    monkeypatch.setattr(mission_control.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(mission_control, "_build_chat_reply", fake_build_chat_reply)
    monkeypatch.setattr(mission_control, "_tracing_context_for_cluster", fake_tracing_context)

    response = await mission_control.send_incident_message(
        str(incident_id),
        schemas.IncidentMessageRequest(message="Thanks"),
        user=user,
        db=fake_db,
    )

    # A chat-only message ("Thanks") is answered immediately from existing
    # context instead of queuing a full re-investigation turn.
    assert response["status"] == "RESPONDED"
    assert response["response"] == "You're welcome!"
    assert created_events[0]["event_type"] == "human_message"
    assert created_events[1]["event_type"] == "assistant_message"
    assert created_events[1]["payload"]["mode"] == "direct_reply"
    assert "coroutine" not in scheduled


@pytest.mark.asyncio
async def test_direct_reply_is_traced_under_the_incident_session(monkeypatch):
    # Over Slack this is the common Q&A path. It answers without invoking the
    # graph, so nothing else would open a trace — leaving it untraced hid most
    # human-in-the-loop turns from Langfuse.
    incident_id = uuid.uuid4()
    cluster_id = uuid.uuid4()
    org_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), org_id=org_id)
    incident = SimpleNamespace(
        id=incident_id,
        cluster_id=cluster_id,
        status=models.IncidentStatus.RESOLVED,
        summary="Recovered.",
        title="Checkout latency spike",
        description="Latency spike during checkout",
        resolved_at=None,
    )
    fake_db = FakeDb(incident)
    traced = {}

    async def fake_create_event(db, created_incident_id, event_type, speaker_role,
                                content, title=None, payload=None,
                                pending_supervisor=False, handled_at=None):
        return SimpleNamespace(id=uuid.uuid4())

    async def fake_get_cluster_by_id(db, requested_cluster_id):
        return SimpleNamespace(id=requested_cluster_id, org_id=org_id, name="cluster-a")

    async def fake_build_chat_reply(message, incident, cluster, org_langfuse=None):
        return "It is still remediating."

    async def fake_tracing_context(cluster_id_arg):
        return None

    @contextlib.asynccontextmanager
    async def fake_trace_run(name, **kwargs):
        traced["name"] = name
        traced.update(kwargs)
        handle = SimpleNamespace(set_output=lambda output: traced.__setitem__("output", output))
        yield handle

    from sre_agent import tracing

    monkeypatch.setattr(mission_control.crud, "create_incident_timeline_event", fake_create_event)
    monkeypatch.setattr(mission_control.crud, "get_cluster_by_id", fake_get_cluster_by_id)
    monkeypatch.setattr(mission_control, "_build_chat_reply", fake_build_chat_reply)
    monkeypatch.setattr(mission_control, "_tracing_context_for_cluster", fake_tracing_context)
    monkeypatch.setattr(tracing, "trace_run", fake_trace_run)

    response = await mission_control.send_incident_message(
        str(incident_id),
        schemas.IncidentMessageRequest(message="What is the status?"),
        user=user,
        db=fake_db,
    )

    assert response["status"] == "RESPONDED"
    assert traced["name"] == "answer-incident-follow-up"
    # Same session as investigate-incident and resume-remediation, so the whole
    # human-in-the-loop workflow reads in order.
    assert traced["session_id"] == str(incident_id)
    assert traced["user_id"] == str(user.id)
    assert "mode:direct-reply" in traced["tags"]
    assert traced["output"] == {"answer": "It is still remediating."}


@pytest.mark.asyncio
async def test_chat_reply_on_a_running_incident_is_traced_too(monkeypatch):
    # The other direct-reply branch: the incident has no summary yet, so a
    # question answered mid-investigation used to return without ever opening
    # a trace. Over Slack that silently dropped a whole class of turns.
    incident_id = uuid.uuid4()
    cluster_id = uuid.uuid4()
    org_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), org_id=org_id)
    incident = SimpleNamespace(
        id=incident_id,
        cluster_id=cluster_id,
        status=models.IncidentStatus.INVESTIGATING,
        summary=None,
        title="Checkout latency spike",
        description="Latency spike during checkout",
        resolved_at=None,
    )
    fake_db = FakeDb(incident)
    traced = {}

    async def fake_create_event(db, created_incident_id, event_type, speaker_role,
                                content, title=None, payload=None,
                                pending_supervisor=False, handled_at=None):
        return SimpleNamespace(id=uuid.uuid4())

    async def fake_get_cluster_by_id(db, requested_cluster_id):
        return SimpleNamespace(id=requested_cluster_id, org_id=org_id, name="cluster-a")

    async def fake_build_chat_reply(message, incident, cluster, org_langfuse=None):
        return "Still gathering evidence."

    async def fake_tracing_context(cluster_id_arg):
        return None

    @contextlib.asynccontextmanager
    async def fake_trace_run(name, **kwargs):
        traced["name"] = name
        traced.update(kwargs)
        yield SimpleNamespace(
            set_output=lambda output: traced.__setitem__("output", output)
        )

    from sre_agent import tracing

    monkeypatch.setattr(mission_control.crud, "create_incident_timeline_event", fake_create_event)
    monkeypatch.setattr(mission_control.crud, "get_cluster_by_id", fake_get_cluster_by_id)
    monkeypatch.setattr(mission_control, "_build_chat_reply", fake_build_chat_reply)
    monkeypatch.setattr(mission_control, "_tracing_context_for_cluster", fake_tracing_context)
    monkeypatch.setattr(tracing, "trace_run", fake_trace_run)

    response = await mission_control.send_incident_message(
        str(incident_id),
        schemas.IncidentMessageRequest(message="What is the status?"),
        user=user,
        db=fake_db,
    )

    assert response["status"] == "RESPONDED"
    assert traced["name"] == "answer-incident-follow-up"
    assert traced["session_id"] == str(incident_id)
    assert traced["user_id"] == str(user.id)
    # The branch that answered is worth knowing: the same question reads
    # differently mid-investigation than after a summary exists.
    assert "INVESTIGATING" in traced["metadata"]["incident_status"]


@pytest.mark.asyncio
async def test_narrator_call_carries_the_langfuse_handler(monkeypatch):
    # Without this the follow-up trace is a root observation and nothing
    # under it — no model, no token usage, no cost on the most common
    # human-in-the-loop turn.
    class FakeLLM:
        def __init__(self, config=None):
            self.config = config

        def with_config(self, config):
            return FakeLLM(config)

    from sre_agent import incident_timeline, model_router, narrative, redis_state_store, tracing

    handler = object()
    seen = {}

    async def fake_context(incident_id):
        return {"objective": "keep checkout healthy", "recent_turns": []}

    async def fake_followup(llm, **kwargs):
        seen["llm"] = llm
        return "Still gathering evidence."

    monkeypatch.setattr(incident_timeline, "load_incident_chat_context", fake_context)
    monkeypatch.setattr(model_router, "route_llm", lambda *a, **k: FakeLLM())
    monkeypatch.setattr(narrative, "narrate_followup_answer", fake_followup)
    monkeypatch.setattr(redis_state_store, "get_state_store", lambda: SimpleNamespace(get=lambda _id: None))
    monkeypatch.setattr(tracing, "get_langfuse_callback", lambda org_langfuse=None: handler)

    incident = SimpleNamespace(
        id=uuid.uuid4(),
        cluster_id=uuid.uuid4(),
        status=models.IncidentStatus.INVESTIGATING,
        summary=None,
        title="Checkout latency spike",
    )
    reply = await mission_control._build_chat_reply(
        "why does this need approval?",
        incident,
        SimpleNamespace(id=uuid.uuid4(), name="cluster-a"),
        org_langfuse={"public_key": "pk", "secret_key": "sk"},
    )

    assert reply == "Still gathering evidence."
    assert seen["llm"].config == {"callbacks": [handler]}


@pytest.mark.asyncio
async def test_narrator_still_answers_when_tracing_is_off(monkeypatch):
    # Tracing must never be what breaks a Slack answer: no handler means an
    # unbound model, not a swallowed reply.
    class FakeLLM:
        def with_config(self, config):  # pragma: no cover - must not be called
            raise AssertionError("no handler means no binding")

    from sre_agent import incident_timeline, model_router, narrative, redis_state_store, tracing

    seen = {}

    async def fake_context(incident_id):
        return {"objective": "keep checkout healthy", "recent_turns": []}

    async def fake_followup(llm, **kwargs):
        seen["llm"] = llm
        return "Still gathering evidence."

    monkeypatch.setattr(incident_timeline, "load_incident_chat_context", fake_context)
    monkeypatch.setattr(model_router, "route_llm", lambda *a, **k: FakeLLM())
    monkeypatch.setattr(narrative, "narrate_followup_answer", fake_followup)
    monkeypatch.setattr(redis_state_store, "get_state_store", lambda: SimpleNamespace(get=lambda _id: None))
    monkeypatch.setattr(tracing, "get_langfuse_callback", lambda org_langfuse=None: None)

    incident = SimpleNamespace(
        id=uuid.uuid4(),
        cluster_id=uuid.uuid4(),
        status=models.IncidentStatus.INVESTIGATING,
        summary=None,
        title="Checkout latency spike",
    )
    reply = await mission_control._build_chat_reply(
        "why does this need approval?",
        incident,
        SimpleNamespace(id=uuid.uuid4(), name="cluster-a"),
    )

    assert reply == "Still gathering evidence."
    assert isinstance(seen["llm"], FakeLLM)


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

    async def fake_get_agent_graph(cluster_id):
        return FakeGraph()

    monkeypatch.setattr(mission_control, "get_agent_graph", fake_get_agent_graph)

    await mission_control._run_post_summary_follow_up(
        incident_id,
        "What changed recently after the deploy?",
        user,
        uuid.uuid4(),
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


@pytest.mark.asyncio
async def test_investigated_follow_up_uses_durable_investigation_queue(monkeypatch):
    from sre_agent import incident_timeline, job_worker, redis_state_store

    incident_id = uuid.uuid4()
    cluster_id = uuid.uuid4()
    org_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), org_id=org_id)
    incident = SimpleNamespace(
        id=incident_id,
        cluster_id=cluster_id,
        status=models.IncidentStatus.INVESTIGATED,
        summary="",
        title="Checkout latency spike",
        description="Latency spike during checkout",
        resolved_at=None,
    )
    captured = {}

    async def fake_get_cluster_by_id(db, requested_cluster_id):
        return SimpleNamespace(id=requested_cluster_id, org_id=org_id, name="cluster-a")

    async def fake_create_event(*args, **kwargs):
        return SimpleNamespace(id=uuid.uuid4())

    async def fake_load_context(requested_incident_id):
        return {
            "alert_context": {
                "labels": {"service": "checkout"},
                "summary": "Latency elevated",
                "description": "p99 above SLO",
                "severity": "critical",
            }
        }

    async def fake_enqueue(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    def forbidden_create_task(coro):
        coro.close()
        raise AssertionError("follow-up must not create an in-process graph task")

    monkeypatch.setattr(mission_control.crud, "get_cluster_by_id", fake_get_cluster_by_id)
    monkeypatch.setattr(mission_control.crud, "create_incident_timeline_event", fake_create_event)
    monkeypatch.setattr(incident_timeline, "load_incident_chat_context", fake_load_context)
    monkeypatch.setattr(job_worker, "enqueue_and_kick", fake_enqueue)
    monkeypatch.setattr(mission_control.asyncio, "create_task", forbidden_create_task)
    monkeypatch.setattr(
        redis_state_store,
        "get_state_store",
        lambda: type("Store", (), {"append_log": lambda *args: None})(),
    )

    response = await mission_control.send_incident_message(
        str(incident_id),
        schemas.IncidentMessageRequest(message="Investigate the remaining errors"),
        user=user,
        db=FakeDb(incident),
        owned_incident=incident,
    )

    assert response["status"] == "QUEUED"
    assert captured["organization_id"] == org_id
    assert captured["cluster_id"] == cluster_id
    assert captured["incident_id"] == incident_id
    assert captured["alert_labels"] == {"service": "checkout"}
    assert captured["triggered_by"] == "dashboard_chat"
    assert captured["idempotency_key"].startswith(f"follow-up:{incident_id}:")


# ---------------------------------------------------------------------------
# Narration grounding on the chat-only reply path
# ---------------------------------------------------------------------------

async def _grounded_chat_reply(monkeypatch, *, status, reply):
    """Run one chat-only turn through the real `_traced_chat_reply` seam."""
    incident = SimpleNamespace(
        id=uuid.uuid4(),
        cluster_id=uuid.uuid4(),
        status=status,
        summary="Recovered.",
        title="ocr-extractor crash-looping",
        description="",
        resolved_at=None,
    )
    cluster = SimpleNamespace(id=incident.cluster_id, org_id=uuid.uuid4(), name="cluster-a")

    async def fake_build_chat_reply(message, inc, clus, org_langfuse=None):
        return reply

    async def fake_tracing_context(cluster_id_arg):
        return None

    traced = {}

    @contextlib.asynccontextmanager
    async def fake_trace_run(name, **kwargs):
        yield SimpleNamespace(set_output=lambda output: traced.__setitem__("output", output))

    from sre_agent import tracing

    monkeypatch.setattr(mission_control, "_build_chat_reply", fake_build_chat_reply)
    monkeypatch.setattr(mission_control, "_tracing_context_for_cluster", fake_tracing_context)
    monkeypatch.setattr(tracing, "trace_run", fake_trace_run)

    answer = await mission_control._traced_chat_reply(
        "what's happening?", incident, cluster, source="slack", user_id=None
    )
    return answer, traced


@pytest.mark.asyncio
async def test_a_chat_reply_that_contradicts_the_status_is_corrected(monkeypatch):
    """`555a3acb` seq 15, the real thing.

    This is the path that produced it — `_is_chat_only_message` sent the
    question here instead of the graph, and the narrator answered with three
    claims the status column contradicts and no mention of `approve fix`.
    """
    answer, traced = await _grounded_chat_reply(
        monkeypatch,
        status=models.IncidentStatus.AWAITING_APPROVAL,
        reply=(
            "We're still in the investigation phase right now — the incident "
            "is marked `awaiting_approval`, and the execution graph just "
            "started."
        ),
    )

    assert "an investigation is still running" in answer
    assert "a remediation is executing" in answer
    assert "reply `approve fix`" in answer
    # The trace has to record what Slack was actually sent, not the draft.
    assert traced["output"] == {"answer": answer}


@pytest.mark.asyncio
async def test_a_chat_reply_that_agrees_with_the_status_is_untouched(monkeypatch):
    answer, _ = await _grounded_chat_reply(
        monkeypatch,
        status=models.IncidentStatus.INVESTIGATING,
        reply="Still digging — the logs specialist is running now.",
    )

    assert answer == "Still digging — the logs specialist is running now."
