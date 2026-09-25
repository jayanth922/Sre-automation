#!/usr/bin/env python3
"""Durable evidence blobs referenced by compact LangGraph state.

Specialist tool transcripts are useful for audit and replay diagnosis, but they
are the largest values in an incident checkpoint.  This module stores those
transcripts as deterministic gzip-compressed JSON in PostgreSQL.  Graph state
keeps only the returned content-addressed reference and the small measured
values needed by policy.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import uuid
from typing import Any, Dict, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend import database, models


SCHEMA_VERSION = 1
SPECIALIST_TRACE_KIND = "specialist_tool_trace"
CONTENT_ENCODING = "gzip+application/json"


class EvidenceArtifactError(RuntimeError):
    """Raised when durable evidence cannot be written or verified."""


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_safe(model_dump(mode="json"))
        except TypeError:
            return _json_safe(model_dump())
    return str(value)


def _message_record(message: Any) -> Dict[str, Any]:
    if isinstance(message, dict):
        return {"type": str(message.get("type") or "dict"), "data": _json_safe(message)}
    return {
        "type": message.__class__.__name__,
        "data": _json_safe(message),
    }


def encode_specialist_trace(
    *,
    agent_name: str,
    messages: Iterable[Any],
    raw_response: str,
    tool_failures: Iterable[Dict[str, str]],
) -> tuple[bytes, bytes, str, int]:
    """Return canonical JSON, compressed bytes, digest, and message count."""
    records = [_message_record(message) for message in messages]
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "kind": SPECIALIST_TRACE_KIND,
        "source": str(agent_name),
        "messages": records,
        "raw_response": str(raw_response or ""),
        "tool_failures": _json_safe(list(tool_failures)),
    }
    canonical = json.dumps(
        envelope,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return (
        canonical,
        gzip.compress(canonical, compresslevel=6, mtime=0),
        hashlib.sha256(canonical).hexdigest(),
        len(records),
    )


def _reference(
    artifact: models.EvidenceArtifact,
    *,
    message_count: int,
    raw_response_chars: int,
) -> Dict[str, Any]:
    return {
        "artifact_id": str(artifact.id),
        "kind": artifact.kind,
        "source": artifact.source,
        "schema_version": artifact.schema_version,
        "sha256": artifact.content_sha256,
        "byte_count": artifact.byte_count,
        "stored_byte_count": artifact.stored_byte_count,
        "message_count": int(message_count),
        "raw_response_chars": int(raw_response_chars),
        "root_trace_id": artifact.root_trace_id,
    }


async def persist_specialist_trace(
    *,
    incident_id: Optional[str],
    root_trace_id: Optional[str],
    agent_name: str,
    messages: Iterable[Any],
    raw_response: str,
    tool_failures: Iterable[Dict[str, str]],
) -> Optional[Dict[str, Any]]:
    """Persist one specialist transcript and return its compact reference.

    ``None`` means there is no durable incident identity (the CLI/ad-hoc path),
    so callers should retain the legacy in-state transcript.  Storage failures
    raise, allowing the caller to preserve the same fallback instead of losing
    evidence silently.
    """
    if not incident_id:
        return None
    try:
        incident_uuid = uuid.UUID(str(incident_id))
    except (TypeError, ValueError) as exc:
        raise EvidenceArtifactError("invalid incident id for evidence artifact") from exc

    canonical, compressed, digest, message_count = encode_specialist_trace(
        agent_name=agent_name,
        messages=messages,
        raw_response=raw_response,
        tool_failures=tool_failures,
    )

    async with database.AsyncSessionLocal() as db:
        query = select(models.EvidenceArtifact).where(
            models.EvidenceArtifact.incident_id == incident_uuid,
            models.EvidenceArtifact.kind == SPECIALIST_TRACE_KIND,
            models.EvidenceArtifact.source == agent_name,
            models.EvidenceArtifact.content_sha256 == digest,
        )
        existing = (await db.execute(query)).scalars().first()
        if existing is not None:
            return _reference(
                existing,
                message_count=message_count,
                raw_response_chars=len(str(raw_response or "")),
            )

        artifact = models.EvidenceArtifact(
            incident_id=incident_uuid,
            root_trace_id=str(root_trace_id or "") or None,
            kind=SPECIALIST_TRACE_KIND,
            source=agent_name,
            schema_version=SCHEMA_VERSION,
            content_sha256=digest,
            byte_count=len(canonical),
            stored_byte_count=len(compressed),
            content_encoding=CONTENT_ENCODING,
            payload=compressed,
        )
        db.add(artifact)
        try:
            await db.commit()
            await db.refresh(artifact)
        except IntegrityError:
            # A replay or concurrent worker may have inserted the same content
            # after our initial lookup. The unique content key makes that a
            # reference lookup, not a second artifact.
            await db.rollback()
            existing = (await db.execute(query)).scalars().first()
            if existing is None:
                raise EvidenceArtifactError("artifact conflict could not be resolved")
            artifact = existing
        return _reference(
            artifact,
            message_count=message_count,
            raw_response_chars=len(str(raw_response or "")),
        )


async def load_evidence_artifact(
    *, incident_id: str, artifact_id: str
) -> Dict[str, Any]:
    """Load and integrity-check an artifact within its incident boundary."""
    try:
        incident_uuid = uuid.UUID(str(incident_id))
        artifact_uuid = uuid.UUID(str(artifact_id))
    except (TypeError, ValueError) as exc:
        raise EvidenceArtifactError("invalid evidence artifact identity") from exc

    async with database.AsyncSessionLocal() as db:
        artifact = (
            await db.execute(
                select(models.EvidenceArtifact).where(
                    models.EvidenceArtifact.id == artifact_uuid,
                    models.EvidenceArtifact.incident_id == incident_uuid,
                )
            )
        ).scalars().first()
    if artifact is None:
        raise EvidenceArtifactError("evidence artifact not found")
    if artifact.content_encoding != CONTENT_ENCODING:
        raise EvidenceArtifactError("unsupported evidence artifact encoding")
    try:
        canonical = gzip.decompress(bytes(artifact.payload))
    except (OSError, EOFError) as exc:
        raise EvidenceArtifactError("evidence artifact decompression failed") from exc
    if hashlib.sha256(canonical).hexdigest() != artifact.content_sha256:
        raise EvidenceArtifactError("evidence artifact digest mismatch")
    try:
        payload = json.loads(canonical)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceArtifactError("evidence artifact payload is invalid") from exc
    if not isinstance(payload, dict):
        raise EvidenceArtifactError("evidence artifact payload has the wrong shape")
    return payload
