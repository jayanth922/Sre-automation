#!/usr/bin/env python3
"""Local Markdown runbooks MCP server.

This server replaces the Notion-backed runbooks path with a file-backed source of
truth stored in the repository root `runbooks/` directory.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml
from mcp.server.fastmcp import FastMCP

try:
    from fastembed import TextEmbedding
    FASTEMBED_AVAILABLE = True
except ImportError:
    TextEmbedding = None
    FASTEMBED_AVAILABLE = False


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class RunbookRecord:
    path: Path
    metadata: dict[str, Any]
    content: str

    @property
    def runbook_id(self) -> str:
        return str(self.metadata.get("runbook_id") or self.metadata.get("Runbook ID") or self.path.stem)

    @property
    def title(self) -> str:
        return str(self.metadata.get("title") or self.metadata.get("Title") or self.path.stem)

    @property
    def service(self) -> str:
        return str(self.metadata.get("service") or self.metadata.get("Service") or "")

    @property
    def incident_type(self) -> str:
        return str(self.metadata.get("incident_type") or self.metadata.get("Incident Type") or "")

    @property
    def searchable_text(self) -> str:
        metadata = self.metadata
        tags = metadata.get("tags", [])
        related_systems = metadata.get("related_systems", [])
        if not isinstance(tags, list):
            tags = [str(tags)]
        if not isinstance(related_systems, list):
            related_systems = [str(related_systems)]

        parts = [
            self.title,
            self.runbook_id,
            self.service,
            self.incident_type,
            str(metadata.get("severity", "")),
            str(metadata.get("status", "")),
            str(metadata.get("owner_team", "")),
            str(metadata.get("primary_owner", "")),
            str(metadata.get("alert_name", "")),
            str(metadata.get("impacted_environment", "")),
            str(metadata.get("service_tier", "")),
            " ".join(map(str, tags)),
            " ".join(map(str, related_systems)),
            self.content,
        ]
        return " ".join(part for part in parts if part)


@dataclass
class RunbookSearchEntry:
    record: RunbookRecord
    embedding: Optional[list[float]]
    mtime_ns: int


def _default_runbooks_dir() -> Path:
    env_path = os.getenv("RUNBOOKS_DIR")
    if env_path:
        return Path(env_path).expanduser().resolve()
    return Path(__file__).resolve().parent / "runbooks"


RUNBOOKS_DIR = _default_runbooks_dir()
RUNBOOKS_INDEX: list[RunbookSearchEntry] = []
RUNBOOKS_INDEX_SOURCE: Optional[Path] = None
RUNBOOKS_EMBEDDING_MODEL = os.getenv("RUNBOOKS_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
RUNBOOKS_INDEX_LIMIT = int(os.getenv("RUNBOOKS_INDEX_LIMIT", "200"))
_embedding_model = None
 


def _get_embedding_model():
    global _embedding_model
    if _embedding_model is not None:
        return _embedding_model
    if not FASTEMBED_AVAILABLE:
        return None
    try:
        _embedding_model = TextEmbedding(model_name=RUNBOOKS_EMBEDDING_MODEL)
        logger.info("Initialized runbook embedding model: %s", RUNBOOKS_EMBEDDING_MODEL)
    except Exception as exc:
        logger.warning("Failed to initialize runbook embedding model: %s", exc)
        _embedding_model = None
    return _embedding_model


def _embed_texts(texts: list[str]) -> list[list[float]]:
    model = _get_embedding_model()
    if not model:
        return []
    vectors: list[list[float]] = []
    try:
        for embedding in model.embed(texts):
            vectors.append(list(embedding.tolist()))
    except Exception as exc:
        logger.warning("Runbook embedding failed: %s", exc)
        return []
    return vectors


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0

    dot = sum(x * y for x, y in zip(left, right))
    left_norm = sum(x * x for x in left) ** 0.5
    right_norm = sum(y * y for y in right) ** 0.5
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _current_runbook_mtimes() -> dict[Path, int]:
    if not RUNBOOKS_DIR.exists():
        return {}
    return {
        file_path: file_path.stat().st_mtime_ns
        for file_path in RUNBOOKS_DIR.glob("*.md")
    }


def _ensure_runbook_index() -> None:
    global RUNBOOKS_INDEX, RUNBOOKS_INDEX_SOURCE

    current_source = RUNBOOKS_DIR.resolve()
    current_mtimes = _current_runbook_mtimes()
    if RUNBOOKS_INDEX_SOURCE == current_source and RUNBOOKS_INDEX:
        cached = {entry.record.path: entry.mtime_ns for entry in RUNBOOKS_INDEX}
        if cached == current_mtimes:
            return

    records = _load_runbooks()
    if not records:
        RUNBOOKS_INDEX = []
        RUNBOOKS_INDEX_SOURCE = current_source
        return

    texts = [record.searchable_text[:12000] for record in records[:RUNBOOKS_INDEX_LIMIT]]
    embeddings = _embed_texts(texts)
    if embeddings and len(embeddings) != len(texts):
        logger.warning("Runbook embedding count mismatch; expected %s got %s", len(texts), len(embeddings))

    embedding_by_path = {
        record.path: embedding
        for record, embedding in zip(records[:len(embeddings)], embeddings)
    }

    RUNBOOKS_INDEX = [
        RunbookSearchEntry(
            record=record,
            embedding=embedding_by_path.get(record.path),
            mtime_ns=current_mtimes.get(record.path, 0),
        )
        for record in records
    ]
    RUNBOOKS_INDEX_SOURCE = current_source
    logger.info("Built runbook semantic index with %s records", len(RUNBOOKS_INDEX))


def _read_markdown_file(file_path: Path) -> RunbookRecord:
    raw_text = file_path.read_text(encoding="utf-8")
    metadata: dict[str, Any] = {}
    content = raw_text.strip()

    if raw_text.startswith("---"):
        parts = raw_text.split("---", 2)
        if len(parts) >= 3:
            _, frontmatter_text, body_text = parts[0], parts[1], parts[2]
            try:
                parsed = yaml.safe_load(frontmatter_text) or {}
                if isinstance(parsed, dict):
                    metadata = parsed
            except Exception as exc:
                logger.warning("Failed to parse frontmatter for %s: %s", file_path.name, exc)
            content = body_text.strip()

    return RunbookRecord(path=file_path, metadata=metadata, content=content)


def _load_runbooks() -> list[RunbookRecord]:
    if not RUNBOOKS_DIR.exists():
        logger.warning("Runbooks directory does not exist: %s", RUNBOOKS_DIR)
        return []

    records: list[RunbookRecord] = []
    for file_path in sorted(RUNBOOKS_DIR.glob("*.md")):
        try:
            records.append(_read_markdown_file(file_path))
        except Exception as exc:
            logger.warning("Skipping unreadable runbook %s: %s", file_path, exc)
    return records


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _runbook_search_blob(record: RunbookRecord) -> str:
    metadata = record.metadata
    fields = [
        record.title,
        record.runbook_id,
        record.service,
        record.incident_type,
        str(metadata.get("severity", "")),
        str(metadata.get("status", "")),
        str(metadata.get("tags", "")),
        str(metadata.get("owner_team", "")),
        str(metadata.get("primary_owner", "")),
        str(metadata.get("alert_name", "")),
        str(metadata.get("impacted_environment", "")),
        str(metadata.get("service_tier", "")),
        record.content,
    ]
    return _normalize(" ".join(fields))


def _tokenize(query: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9_\-]+", query.lower()) if token]


def _build_excerpt(content: str, tokens: list[str], max_len: int = 320) -> str:
    if not content:
        return ""

    lower = content.lower()
    hit_index = -1
    for token in tokens:
        if token:
            hit_index = lower.find(token.lower())
            if hit_index != -1:
                break

    if hit_index == -1:
        return content[:max_len].strip()

    start = max(0, hit_index - max_len // 3)
    end = min(len(content), start + max_len)
    excerpt = content[start:end].strip()
    if start > 0:
        excerpt = f"...{excerpt}"
    if end < len(content):
        excerpt = f"{excerpt}..."
    return excerpt


def _score_record(record: RunbookRecord, query: str) -> tuple[float, str]:
    normalized_query = _normalize(query)
    blob = _runbook_search_blob(record)
    tokens = _tokenize(query)

    score = 0.0
    if not normalized_query:
        score += 1.0

    if normalized_query and normalized_query in _normalize(record.title):
        score += 8.0
    if normalized_query and normalized_query in _normalize(record.runbook_id):
        score += 8.0
    if normalized_query and normalized_query in _normalize(record.service):
        score += 4.0
    if normalized_query and normalized_query in _normalize(record.incident_type):
        score += 4.0

    for token in tokens:
        if token in blob:
            score += 1.0

    excerpt = _build_excerpt(record.content, tokens or ([normalized_query] if normalized_query else []))
    return score, excerpt


def _semantic_search_runbooks(query: str, limit: int = 5) -> list[dict[str, Any]]:
    _ensure_runbook_index()
    if not RUNBOOKS_INDEX:
        return []

    query_embeddings = _embed_texts([query or " "])
    candidates: list[tuple[float, RunbookRecord, str]] = []
    query_tokens = _tokenize(query)
    query_embedding = query_embeddings[0] if query_embeddings else None

    for entry in RUNBOOKS_INDEX:
        score = 0.0
        if query_embedding and entry.embedding:
            score = _cosine_similarity(query_embedding, entry.embedding) * 10.0
        lexical_bonus, excerpt = _score_record(entry.record, query)
        combined_score = score + lexical_bonus
        if combined_score > 0:
            candidates.append((combined_score, entry.record, excerpt))

    candidates.sort(key=lambda item: (-item[0], item[1].runbook_id, item[1].title))
    results = [_record_to_result(record, score, excerpt) for score, record, excerpt in candidates[:limit]]

    for result in results:
        if not result.get("excerpt") and query_tokens:
            record = _find_record(result["runbook_id"])
            if record:
                result["excerpt"] = _build_excerpt(record.content, query_tokens)

    return results


def _find_record(identifier: str) -> Optional[RunbookRecord]:
    query = _normalize(identifier)
    if not query:
        return None

    records = _load_runbooks()
    for record in records:
        candidates = {
            _normalize(record.runbook_id),
            _normalize(record.title),
            _normalize(record.path.stem),
            _normalize(record.path.name),
            _normalize(str(record.metadata.get("slug", ""))),
        }
        if query in candidates:
            return record

    for record in records:
        if query in _runbook_search_blob(record):
            return record
    return None


def _record_to_result(record: RunbookRecord, score: float, excerpt: str) -> dict[str, Any]:
    metadata = record.metadata
    return {
        "runbook_id": record.runbook_id,
        "title": record.title,
        "service": record.service,
        "incident_type": record.incident_type,
        "severity": metadata.get("severity", ""),
        "status": metadata.get("status", ""),
        "owner_team": metadata.get("owner_team", ""),
        "primary_owner": metadata.get("primary_owner", ""),
        "tags": metadata.get("tags", []),
        "alert_name": metadata.get("alert_name", ""),
        "impacted_environment": metadata.get("impacted_environment", ""),
        "service_tier": metadata.get("service_tier", ""),
        "escalation_channel": metadata.get("escalation_channel", ""),
        "related_systems": metadata.get("related_systems", []),
        "version": metadata.get("version", ""),
        "last_reviewed": metadata.get("last_reviewed", ""),
        "path": str(record.path),
        "score": round(score, 3),
        "excerpt": excerpt,
    }


def _extract_section(content: str, heading_candidates: list[str]) -> str:
    lines = content.splitlines()
    heading_indexes: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        match = re.match(r"^(#{2,6})\s+(.*)$", line.strip())
        if match:
            heading_indexes.append((idx, match.group(2).strip()))

    for idx, heading in heading_indexes:
        normalized_heading = _normalize(heading)
        if any(candidate in normalized_heading for candidate in heading_candidates):
            end_idx = len(lines)
            for next_idx, _ in heading_indexes:
                if next_idx > idx:
                    end_idx = next_idx
                    break
            section_lines = lines[idx + 1:end_idx]
            return "\n".join(section_lines).strip()

    return ""


def _compose_query(
    query: str = "",
    incident_type: str = "",
    keyword: str = "",
    severity: str = "",
    service: str = "",
    runbook_id: str = "",
    alert_name: str = "",
) -> str:
    parts = [query, incident_type, keyword, severity, service, runbook_id, alert_name]
    return " ".join(part for part in parts if part).strip()


def _search_runbooks_impl(
    query: str = "",
    incident_type: str = "",
    keyword: str = "",
    severity: str = "",
    service: str = "",
    runbook_id: str = "",
    alert_name: str = "",
    limit: int = 5,
) -> list[dict[str, Any]]:
    query_text = _compose_query(query, incident_type, keyword, severity, service, runbook_id, alert_name)

    return _semantic_search_runbooks(query=query_text, limit=limit)


def _pack_response(tool_name: str, query: str, results: list[dict[str, Any]]) -> str:
    payload = {
        "query": query,
        "tool": tool_name,
        "count": len(results),
        "results": results,
    }
    return json.dumps(payload, indent=2)


port = int(os.getenv("HTTP_PORT", "3000"))
host = os.getenv("HOST", "0.0.0.0")
mcp = FastMCP("Local Runbooks", host=host, port=port)


@mcp.tool()
def search_runbooks(
    query: str = "",
    incident_type: str = "",
    keyword: str = "",
    severity: str = "",
    service: str = "",
    runbook_id: str = "",
    alert_name: str = "",
) -> str:
    """Search local Markdown runbooks by title, metadata, and content."""
    composed_query = _compose_query(query, incident_type, keyword, severity, service, runbook_id, alert_name)
    logger.info("Searching runbooks: %s", composed_query)
    results = _search_runbooks_impl(
        query=query,
        incident_type=incident_type,
        keyword=keyword,
        severity=severity,
        service=service,
        runbook_id=runbook_id,
        alert_name=alert_name,
    )
    return _pack_response("search_runbooks", composed_query, results)


@mcp.tool()
def get_runbook_content(page_id: str) -> str:
    """Get the full Markdown content of a runbook by runbook ID, title, slug, or file name."""
    logger.info("Getting runbook content: %s", page_id)
    record = _find_record(page_id)
    if not record:
        return json.dumps({"error": f"Runbook not found: {page_id}"}, indent=2)

    payload = {
        "runbook_id": record.runbook_id,
        "title": record.title,
        "service": record.service,
        "incident_type": record.incident_type,
        "path": str(record.path),
        "metadata": record.metadata,
        "content": record.content,
    }
    return json.dumps(payload, indent=2)


@mcp.tool()
def get_incident_playbook(incident_type: str) -> str:
    """Return the most relevant runbook for a given incident type."""
    logger.info("Getting incident playbook: %s", incident_type)
    results = _search_runbooks_impl(query=incident_type, incident_type=incident_type)
    if not results:
        return json.dumps(
            {
                "incident_type": incident_type,
                "message": "No playbook found for this incident type",
                "results": [],
            },
            indent=2,
        )
    return get_runbook_content(results[0]["runbook_id"])


@mcp.tool()
def get_troubleshooting_guide(
    query: str = "",
    incident_type: str = "",
    service: str = "",
    keyword: str = "",
) -> str:
    """Return the most relevant troubleshooting section or full runbook for a query."""
    composed_query = _compose_query(query, incident_type, keyword, service)
    logger.info("Getting troubleshooting guide: %s", composed_query)
    results = _search_runbooks_impl(query=query, incident_type=incident_type, keyword=keyword, service=service)
    if not results:
        return json.dumps({"query": composed_query, "message": "No troubleshooting guide found", "results": []}, indent=2)

    top_record = _find_record(results[0]["runbook_id"])
    if not top_record:
        return json.dumps({"query": composed_query, "message": "No troubleshooting guide found", "results": []}, indent=2)

    section = _extract_section(top_record.content, ["troubleshooting", "step-by-step resolution", "resolution"])
    payload = {
        "query": composed_query,
        "runbook_id": top_record.runbook_id,
        "title": top_record.title,
        "service": top_record.service,
        "section": section or top_record.content,
        "path": str(top_record.path),
    }
    return json.dumps(payload, indent=2)


@mcp.tool()
def get_escalation_procedures(
    query: str = "",
    incident_type: str = "",
    service: str = "",
    keyword: str = "",
) -> str:
    """Return escalation guidance extracted from the best matching runbook."""
    composed_query = _compose_query(query, incident_type, keyword, service)
    logger.info("Getting escalation procedures: %s", composed_query)
    results = _search_runbooks_impl(query=query, incident_type=incident_type, keyword=keyword, service=service)
    if not results:
        return json.dumps({"query": composed_query, "message": "No escalation procedures found", "results": []}, indent=2)

    top_record = _find_record(results[0]["runbook_id"])
    if not top_record:
        return json.dumps({"query": composed_query, "message": "No escalation procedures found", "results": []}, indent=2)

    section = _extract_section(top_record.content, ["escalation", "escalation path", "contacts"])
    payload = {
        "query": composed_query,
        "runbook_id": top_record.runbook_id,
        "title": top_record.title,
        "service": top_record.service,
        "section": section or str(top_record.metadata.get("escalation_channel", "")),
        "escalation_channel": top_record.metadata.get("escalation_channel", ""),
        "path": str(top_record.path),
    }
    return json.dumps(payload, indent=2)


@mcp.tool()
def get_common_resolutions(
    query: str = "",
    incident_type: str = "",
    service: str = "",
    keyword: str = "",
) -> str:
    """Return likely common resolutions from the best matching runbook."""
    composed_query = _compose_query(query, incident_type, keyword, service)
    logger.info("Getting common resolutions: %s", composed_query)
    results = _search_runbooks_impl(query=query, incident_type=incident_type, keyword=keyword, service=service)
    if not results:
        return json.dumps({"query": composed_query, "message": "No common resolutions found", "results": []}, indent=2)

    top_record = _find_record(results[0]["runbook_id"])
    if not top_record:
        return json.dumps({"query": composed_query, "message": "No common resolutions found", "results": []}, indent=2)

    section = _extract_section(top_record.content, ["rollback or recovery", "rollback", "verification", "common resolution", "remediation"])
    payload = {
        "query": composed_query,
        "runbook_id": top_record.runbook_id,
        "title": top_record.title,
        "service": top_record.service,
        "section": section or top_record.content,
        "path": str(top_record.path),
    }
    return json.dumps(payload, indent=2)


if __name__ == "__main__":
    logger.info("Starting Local Runbooks MCP Server on %s:%s", host, port)
    logger.info("Using runbooks directory: %s", RUNBOOKS_DIR)
    _ensure_runbook_index()
    mcp.run(transport="sse")