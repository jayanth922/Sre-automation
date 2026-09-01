#!/usr/bin/env python3
"""Notion-backed runbooks MCP server.

Runbooks are hosted exclusively in each tenant's Notion database — there is
no local markdown corpus (replaces the former ``runbooks_local`` server,
which indexed files baked into the image). This server queries Notion's
REST API directly (httpx; no SDK), same call shapes as
``sre_agent/notion_runbooks.py``, deliberately duplicated rather than
imported: ``edge_mcp_servers`` must never import ``sre_agent`` (see
``sre_agent/multitenant/relay_auth.py``'s docstring).

Credentials: prefers this connection's relayed per-cluster
``notion_api_key``/``notion_database_id`` (one control plane can manage many
``Cluster`` rows — see ``edge_mcp_servers/relay_credentials.py``), falling
back to this process's static ``NOTION_API_KEY``/``NOTION_DATABASE_ID`` env
vars for a self-hosted, single-tenant deployment that never relays
credentials — same convention as ``github_real/server.py``'s
``_active_repo()``.

No schema assumptions beyond "there is a title property" (per-team Notion
databases vary); service/incident_type/severity/tags/etc. are pulled from
same-named properties when present.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

NOTION_VERSION = "2022-06-28"
_BASE = "https://api.notion.com/v1"

_STATIC_API_KEY = os.getenv("NOTION_API_KEY")
_STATIC_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")

if not _STATIC_API_KEY or not _STATIC_DATABASE_ID:
    logger.warning(
        "⚠️ NOTION_API_KEY/NOTION_DATABASE_ID not set; server relies entirely "
        "on relayed per-cluster credentials"
    )


def _active_notion_creds() -> Tuple[Optional[str], Optional[str]]:
    """(api_key, database_id) for the in-flight request.

    Prefers a per-request relayed credential over this process's static
    single-tenant env vars, which remain the fallback for a self-hosted
    deployment that never relays per-cluster credentials.
    """
    try:
        from relay_credentials import get_relay_credential
    except ImportError:
        return _STATIC_API_KEY, _STATIC_DATABASE_ID

    api_key = get_relay_credential("notion_api_key") or _STATIC_API_KEY
    database_id = get_relay_credential("notion_database_id") or _STATIC_DATABASE_ID
    return api_key, database_id


def _headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _prop_text(prop: Optional[Dict[str, Any]]) -> str:
    """Extract plain text from a Notion property of any common type."""
    if not prop:
        return ""
    t = prop.get("type")
    if t == "title":
        return "".join(x.get("plain_text", "") for x in prop.get("title", []))
    if t == "rich_text":
        return "".join(x.get("plain_text", "") for x in prop.get("rich_text", []))
    if t == "select":
        return (prop.get("select") or {}).get("name", "")
    if t == "status":
        return (prop.get("status") or {}).get("name", "")
    if t == "multi_select":
        return ", ".join(x.get("name", "") for x in prop.get("multi_select", []))
    return ""


def _find(props: Dict[str, Any], *names: str) -> Optional[Dict[str, Any]]:
    lower = {k.lower(): v for k, v in props.items()}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    return None


def _page_to_runbook(page: Dict[str, Any]) -> Dict[str, Any]:
    props = page.get("properties", {}) or {}
    title_prop = next((p for p in props.values() if p.get("type") == "title"), None)
    title = _prop_text(title_prop) or "Untitled"
    return {
        "runbook_id": page.get("id", ""),
        "title": title,
        "service": _prop_text(_find(props, "service")) or "—",
        "incident_type": _prop_text(_find(props, "incident type", "incident_type", "type")) or "—",
        "severity": _prop_text(_find(props, "severity")) or "—",
        "status": _prop_text(_find(props, "status")) or "",
        "owner_team": _prop_text(_find(props, "owner_team", "owner team", "owner")) or "",
        "tags": _prop_text(_find(props, "tags")) or "",
        "alert_name": _prop_text(_find(props, "alert_name", "alert name")) or "",
        "impacted_environment": _prop_text(_find(props, "impacted_environment", "environment")) or "",
        "escalation_channel": _prop_text(_find(props, "escalation_channel", "escalation channel")) or "",
        "path": page.get("url") or "notion",
    }


def _blocks_to_markdown(blocks: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for b in blocks:
        t = b.get("type", "")
        rich = (b.get(t) or {}).get("rich_text", []) if isinstance(b.get(t), dict) else []
        text = "".join(x.get("plain_text", "") for x in rich)
        if t == "heading_1":
            lines.append(f"# {text}")
        elif t == "heading_2":
            lines.append(f"## {text}")
        elif t == "heading_3":
            lines.append(f"### {text}")
        elif t == "bulleted_list_item":
            lines.append(f"- {text}")
        elif t == "numbered_list_item":
            lines.append(f"1. {text}")
        elif t == "code":
            lines.append(f"```\n{text}\n```")
        elif text:
            lines.append(text)
    return "\n".join(lines)


# Bounded, short-lived cache of one database's page list, keyed by (api_key,
# database_id), so one investigation's several runbook tool calls (search,
# playbook, troubleshooting, escalation) don't each re-query Notion from
# scratch. Kept short (not correctness-critical) since Notion is the source
# of truth and this is a read-mostly catalog.
_DB_CACHE: Dict[Tuple[str, str], Tuple[float, List[Dict[str, Any]]]] = {}
_DB_CACHE_MAX = 8
_DB_CACHE_TTL_SECONDS = 20.0


async def _query_database(api_key: str, database_id: str) -> List[Dict[str, Any]]:
    cache_key = (api_key, database_id)
    cached = _DB_CACHE.get(cache_key)
    if cached and (time.monotonic() - cached[0]) < _DB_CACHE_TTL_SECONDS:
        return cached[1]

    async with httpx.AsyncClient(timeout=12.0) as client:
        resp = await client.post(
            f"{_BASE}/databases/{database_id}/query",
            headers=_headers(api_key),
            json={"page_size": 100},
        )
        resp.raise_for_status()
        pages = resp.json().get("results", [])

    if len(_DB_CACHE) >= _DB_CACHE_MAX and cache_key not in _DB_CACHE:
        _DB_CACHE.pop(next(iter(_DB_CACHE)))
    _DB_CACHE[cache_key] = (time.monotonic(), pages)
    return pages


async def _fetch_page_content(api_key: str, page_id: str) -> Tuple[Dict[str, Any], str]:
    async with httpx.AsyncClient(timeout=12.0) as client:
        page_resp = await client.get(f"{_BASE}/pages/{page_id}", headers=_headers(api_key))
        page_resp.raise_for_status()
        page = page_resp.json()
        blocks_resp = await client.get(
            f"{_BASE}/blocks/{page_id}/children", headers=_headers(api_key), params={"page_size": 100}
        )
        blocks_resp.raise_for_status()
        blocks = blocks_resp.json().get("results", [])
    return page, _blocks_to_markdown(blocks) or "(empty runbook)"


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _tokenize(query: str) -> List[str]:
    return [token for token in re.findall(r"[a-z0-9_\-]+", query.lower()) if token]


def _runbook_search_blob(rb: Dict[str, Any]) -> str:
    fields = [
        rb.get("title", ""),
        rb.get("service", ""),
        rb.get("incident_type", ""),
        rb.get("severity", ""),
        rb.get("status", ""),
        rb.get("owner_team", ""),
        rb.get("tags", ""),
        rb.get("alert_name", ""),
        rb.get("impacted_environment", ""),
    ]
    return _normalize(" ".join(str(f) for f in fields))


def _score_record(rb: Dict[str, Any], query: str) -> float:
    normalized_query = _normalize(query)
    blob = _runbook_search_blob(rb)
    tokens = _tokenize(query)

    score = 0.0
    if not normalized_query:
        score += 1.0
    if normalized_query and normalized_query in _normalize(rb.get("title", "")):
        score += 8.0
    if normalized_query and normalized_query in _normalize(rb.get("service", "")):
        score += 4.0
    if normalized_query and normalized_query in _normalize(rb.get("incident_type", "")):
        score += 4.0
    for token in tokens:
        if token in blob:
            score += 1.0
    return score


def _build_excerpt(content: str, tokens: List[str], max_len: int = 320) -> str:
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


def _extract_section(content: str, heading_candidates: List[str]) -> str:
    lines = content.splitlines()
    heading_indexes: List[Tuple[int, str]] = []
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


def _looks_like_notion_id(identifier: str) -> bool:
    stripped = identifier.replace("-", "")
    return len(stripped) == 32 and all(c in "0123456789abcdefABCDEF" for c in stripped)


async def _resolve_page_id(identifier: str, api_key: str, database_id: str) -> Optional[str]:
    """A Notion page id, either passed directly or resolved by title match."""
    if not identifier:
        return None
    if _looks_like_notion_id(identifier):
        return identifier

    pages = await _query_database(api_key, database_id)
    query = _normalize(identifier)
    for page in pages:
        rb = _page_to_runbook(page)
        if _normalize(rb["title"]) == query or _normalize(rb["runbook_id"]) == query:
            return rb["runbook_id"]
    for page in pages:
        rb = _page_to_runbook(page)
        if query in _runbook_search_blob(rb):
            return rb["runbook_id"]
    return None


async def _search_runbooks_impl(
    query: str = "",
    incident_type: str = "",
    keyword: str = "",
    severity: str = "",
    service: str = "",
    runbook_id: str = "",
    alert_name: str = "",
    limit: int = 5,
) -> List[Dict[str, Any]]:
    api_key, database_id = _active_notion_creds()
    if not api_key or not database_id:
        logger.warning("No Notion credentials available for this request (relayed or static)")
        return []

    composed_query = _compose_query(query, incident_type, keyword, severity, service, runbook_id, alert_name)
    try:
        pages = await _query_database(api_key, database_id)
    except Exception as exc:
        logger.warning("Notion database query failed: %s", exc)
        return []

    tokens = _tokenize(composed_query)
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for page in pages:
        rb = _page_to_runbook(page)
        score = _score_record(rb, composed_query)
        if score > 0:
            scored.append((score, rb))
    scored.sort(key=lambda item: (-item[0], item[1]["title"]))

    results: List[Dict[str, Any]] = []
    for score, rb in scored[:limit]:
        excerpt = ""
        try:
            _, content = await _fetch_page_content(api_key, rb["runbook_id"])
            excerpt = _build_excerpt(content, tokens)
        except Exception as exc:
            logger.warning("Failed to fetch excerpt for %s: %s", rb["runbook_id"], exc)
        results.append({**rb, "score": round(score, 3), "excerpt": excerpt})
    return results


def _pack_response(tool_name: str, query: str, results: List[Dict[str, Any]]) -> str:
    payload = {"query": query, "tool": tool_name, "count": len(results), "results": results}
    return json.dumps(payload, separators=(",", ":"))


port = int(os.getenv("HTTP_PORT", "3000"))
host = os.getenv("HOST", "0.0.0.0")
mcp = FastMCP("Notion Runbooks", host=host, port=port)


@mcp.tool()
async def search_runbooks(
    query: str = "",
    incident_type: str = "",
    keyword: str = "",
    severity: str = "",
    service: str = "",
    runbook_id: str = "",
    alert_name: str = "",
) -> str:
    """Search the cluster's Notion runbook database by title, properties, and content."""
    composed_query = _compose_query(query, incident_type, keyword, severity, service, runbook_id, alert_name)
    logger.info("Searching Notion runbooks: %s", composed_query)
    results = await _search_runbooks_impl(
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
async def get_runbook_content(page_id: str) -> str:
    """Get the full Markdown content of a runbook by Notion page id, title, or slug."""
    logger.info("Getting Notion runbook content: %s", page_id)
    api_key, database_id = _active_notion_creds()
    if not api_key or not database_id:
        return json.dumps({"error": "No Notion credentials configured for this cluster"}, separators=(",", ":"))

    resolved_id = await _resolve_page_id(page_id, api_key, database_id)
    if not resolved_id:
        return json.dumps({"error": f"Runbook not found: {page_id}"}, separators=(",", ":"))

    try:
        page, content = await _fetch_page_content(api_key, resolved_id)
    except Exception as exc:
        return json.dumps({"error": f"Failed to fetch runbook {page_id}: {exc}"}, separators=(",", ":"))

    rb = _page_to_runbook(page)
    rb["content"] = content
    return json.dumps(rb, separators=(",", ":"))


@mcp.tool()
async def get_incident_playbook(incident_type: str) -> str:
    """Return the most relevant runbook for a given incident type."""
    logger.info("Getting incident playbook: %s", incident_type)
    results = await _search_runbooks_impl(query=incident_type, incident_type=incident_type)
    if not results:
        return json.dumps(
            {"incident_type": incident_type, "message": "No playbook found for this incident type", "results": []},
            separators=(",", ":"),
        )
    return await get_runbook_content(results[0]["runbook_id"])


async def _top_result_content(
    query: str, incident_type: str, service: str, keyword: str
) -> Optional[Tuple[Dict[str, Any], str]]:
    results = await _search_runbooks_impl(query=query, incident_type=incident_type, keyword=keyword, service=service)
    if not results:
        return None
    api_key, database_id = _active_notion_creds()
    if not api_key or not database_id:
        return None
    try:
        page, content = await _fetch_page_content(api_key, results[0]["runbook_id"])
    except Exception as exc:
        logger.warning("Failed to fetch top result content: %s", exc)
        return None
    return _page_to_runbook(page), content


@mcp.tool()
async def get_troubleshooting_guide(
    query: str = "",
    incident_type: str = "",
    service: str = "",
    keyword: str = "",
) -> str:
    """Return the most relevant troubleshooting section or full runbook for a query."""
    composed_query = _compose_query(query, incident_type, keyword, service)
    logger.info("Getting troubleshooting guide: %s", composed_query)
    top = await _top_result_content(query, incident_type, service, keyword)
    if not top:
        return json.dumps({"query": composed_query, "message": "No troubleshooting guide found", "results": []}, separators=(",", ":"))
    rb, content = top
    section = _extract_section(content, ["troubleshooting", "step-by-step resolution", "resolution"])
    return json.dumps(
        {
            "query": composed_query,
            "runbook_id": rb["runbook_id"],
            "title": rb["title"],
            "service": rb["service"],
            "section": section or content,
            "path": rb["path"],
        },
        separators=(",", ":"),
    )


@mcp.tool()
async def get_escalation_procedures(
    query: str = "",
    incident_type: str = "",
    service: str = "",
    keyword: str = "",
) -> str:
    """Return escalation guidance extracted from the best matching runbook."""
    composed_query = _compose_query(query, incident_type, keyword, service)
    logger.info("Getting escalation procedures: %s", composed_query)
    top = await _top_result_content(query, incident_type, service, keyword)
    if not top:
        return json.dumps({"query": composed_query, "message": "No escalation procedures found", "results": []}, separators=(",", ":"))
    rb, content = top
    section = _extract_section(content, ["escalation", "escalation path", "contacts"])
    return json.dumps(
        {
            "query": composed_query,
            "runbook_id": rb["runbook_id"],
            "title": rb["title"],
            "service": rb["service"],
            "section": section or rb.get("escalation_channel", ""),
            "escalation_channel": rb.get("escalation_channel", ""),
            "path": rb["path"],
        },
        separators=(",", ":"),
    )


@mcp.tool()
async def get_common_resolutions(
    query: str = "",
    incident_type: str = "",
    service: str = "",
    keyword: str = "",
) -> str:
    """Return likely common resolutions from the best matching runbook."""
    composed_query = _compose_query(query, incident_type, keyword, service)
    logger.info("Getting common resolutions: %s", composed_query)
    top = await _top_result_content(query, incident_type, service, keyword)
    if not top:
        return json.dumps({"query": composed_query, "message": "No common resolutions found", "results": []}, separators=(",", ":"))
    rb, content = top
    section = _extract_section(content, ["rollback or recovery", "rollback", "verification", "common resolution", "remediation"])
    return json.dumps(
        {
            "query": composed_query,
            "runbook_id": rb["runbook_id"],
            "title": rb["title"],
            "service": rb["service"],
            "section": section or content,
            "path": rb["path"],
        },
        separators=(",", ":"),
    )


if __name__ == "__main__":
    logger.info("Starting Notion Runbooks MCP Server on %s:%s", host, port)
    from mcp_auth import run_authenticated_sse
    run_authenticated_sse(mcp, host=host, port=port)
