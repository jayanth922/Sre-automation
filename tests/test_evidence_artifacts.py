import gzip
import json
import uuid

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from sre_agent import agent_nodes, evidence_artifacts
from sre_agent.act_phase import extract_incident_signals


class _Result:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return self

    def first(self):
        return self.value


class _ArtifactStore:
    artifact = None


class _Session:
    def __init__(self, store):
        self.store = store
        self.pending = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, query):
        artifact = self.store.artifact
        if artifact is None:
            return _Result(None)
        values = set(query.compile().params.values())
        if artifact.incident_id not in values:
            return _Result(None)
        # Artifact loads are scoped by both IDs; content-dedupe lookups use
        # incident/kind/source/digest instead.
        if len(values) == 2 and artifact.id not in values:
            return _Result(None)
        return _Result(artifact)

    def add(self, artifact):
        self.pending = artifact

    async def commit(self):
        if self.pending is not None:
            self.pending.id = self.pending.id or uuid.uuid4()
            self.store.artifact = self.pending

    async def refresh(self, _artifact):
        return None

    async def rollback(self):
        self.pending = None


def _tool_message(payload, *, size=0):
    body = {**payload, "raw_series": "x" * size}
    return ToolMessage(
        content=json.dumps(body),
        name="prometheus_query",
        tool_call_id="tc-1",
    )


def test_specialist_artifact_encoding_is_deterministic_and_lossless():
    kwargs = {
        "agent_name": "metrics_agent",
        "messages": [AIMessage(content="checking"), _tool_message({"error_rate": 0.2})],
        "raw_response": "Error rate is elevated.",
        "tool_failures": [],
    }

    canonical, compressed, digest, message_count = (
        evidence_artifacts.encode_specialist_trace(**kwargs)
    )
    second = evidence_artifacts.encode_specialist_trace(**kwargs)

    assert gzip.decompress(compressed) == canonical
    assert (canonical, compressed, digest, message_count) == second
    assert message_count == 2
    assert json.loads(canonical)["raw_response"] == "Error rate is elevated."


@pytest.mark.asyncio
async def test_artifact_survives_a_new_session_and_is_integrity_checked(monkeypatch):
    store = _ArtifactStore()
    monkeypatch.setattr(
        evidence_artifacts.database,
        "AsyncSessionLocal",
        lambda: _Session(store),
    )
    incident_id = str(uuid.uuid4())

    reference = await evidence_artifacts.persist_specialist_trace(
        incident_id=incident_id,
        root_trace_id="trace-1",
        agent_name="metrics_agent",
        messages=[_tool_message({"error_rate": 0.24})],
        raw_response="Measured 24% errors.",
        tool_failures=[],
    )
    loaded = await evidence_artifacts.load_evidence_artifact(
        incident_id=incident_id,
        artifact_id=reference["artifact_id"],
    )

    assert loaded["source"] == "metrics_agent"
    assert loaded["raw_response"] == "Measured 24% errors."
    assert reference["sha256"] == store.artifact.content_sha256
    assert reference["raw_response_chars"] == len("Measured 24% errors.")

    with pytest.raises(
        evidence_artifacts.EvidenceArtifactError, match="artifact not found"
    ):
        await evidence_artifacts.load_evidence_artifact(
            incident_id=str(uuid.uuid4()),
            artifact_id=reference["artifact_id"],
        )

    store.artifact.payload = store.artifact.payload[:-1] + b"0"
    with pytest.raises(
        evidence_artifacts.EvidenceArtifactError,
        match="decompression failed|digest mismatch",
    ):
        await evidence_artifacts.load_evidence_artifact(
            incident_id=incident_id,
            artifact_id=reference["artifact_id"],
        )


@pytest.mark.asyncio
async def test_checkpoint_keeps_reference_and_measured_projection_not_raw_trace(
    monkeypatch,
):
    reference = {
        "artifact_id": str(uuid.uuid4()),
        "kind": "specialist_tool_trace",
        "source": "metrics_agent",
        "schema_version": 1,
        "sha256": "a" * 64,
        "byte_count": 50000,
        "stored_byte_count": 500,
        "message_count": 1,
        "root_trace_id": "trace-1",
    }

    async def _persist(**_kwargs):
        return reference

    monkeypatch.setattr(agent_nodes, "persist_specialist_trace", _persist)
    huge = _tool_message({"error_rate": 0.24, "affected_pods": 3}, size=50000)
    metadata, returned = await agent_nodes._artifact_backed_trace_metadata(
        {"metadata": {"root_trace_id": "trace-1"}},
        incident_id=str(uuid.uuid4()),
        agent_key="metrics_agent",
        messages=[huge],
        raw_response="Measured elevated errors.",
        tool_failures=[],
    )

    assert returned == reference
    assert "metrics_agent_trace" not in metadata
    assert metadata["evidence_artifact_refs"]["metrics_agent"] == [reference]
    assert metadata["measured_evidence"]["metrics_agent"]["error_rate"] == {
        "value": 0.24,
        "source": "metrics_agent:prometheus_query:error_rate",
    }
    assert "raw_series" not in json.dumps(metadata)

    signals = extract_incident_signals(
        {"metadata": metadata, "agent_results": {}, "alert_context": {"labels": {}}}
    )
    assert signals.error_rate == 0.24
    assert signals.affected_pods == 3
    assert [
        link.source for link in signals.evidence if link.field == "error_rate"
    ] == ["tool:metrics_agent:prometheus_query:error_rate"]


@pytest.mark.asyncio
async def test_artifact_failure_retains_legacy_trace_and_compact_evidence(monkeypatch):
    async def _fail(**_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(agent_nodes, "persist_specialist_trace", _fail)
    message = _tool_message({"slo_burn_rate": 12.5})

    metadata, reference = await agent_nodes._artifact_backed_trace_metadata(
        {"metadata": {}},
        incident_id=str(uuid.uuid4()),
        agent_key="metrics_agent",
        messages=[message],
        raw_response="Burning budget.",
        tool_failures=[],
    )

    assert reference is None
    assert metadata["metrics_agent_trace"] == [message]
    assert metadata["evidence_artifact_errors"] == {
        "metrics_agent": "RuntimeError"
    }
    assert metadata["measured_evidence"]["metrics_agent"]["slo_burn_rate"][
        "value"
    ] == 12.5


def test_active_agent_result_is_bounded_at_both_ends():
    response = "begin:" + ("x" * 5000) + ":conclusion"

    bounded = agent_nodes._bounded_agent_result(response, max_chars=2000)

    assert len(bounded) == 2000
    assert bounded.startswith("begin:")
    assert bounded.endswith(":conclusion")
    assert "full response stored in evidence artifact" in bounded
