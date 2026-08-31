"""Notion-backed runbook source.

Production runbooks usually live in Notion, per team. When a cluster is
configured with Notion credentials, the runbook catalog and content are read
from the client's Notion database instead of the local markdown corpus — no
schema assumptions beyond "there is a title property"; service/incident-type/
severity are pulled from same-named properties when present.

Uses the Notion REST API directly (httpx) — no extra SDK dependency.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

NOTION_VERSION = "2022-06-28"
_BASE = "https://api.notion.com/v1"


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
        "id": page.get("id", ""),
        "title": title,
        "service": _prop_text(_find(props, "service")) or "—",
        "incident_type": _prop_text(_find(props, "incident type", "incident_type", "type")) or "—",
        "severity": _prop_text(_find(props, "severity")) or "—",
        "path": (page.get("url") or "notion"),
    }


async def list_notion_runbooks(api_key: str, database_id: str) -> List[Dict[str, Any]]:
    async with httpx.AsyncClient(timeout=12.0) as client:
        resp = await client.post(
            f"{_BASE}/databases/{database_id}/query",
            headers=_headers(api_key),
            json={"page_size": 100},
        )
        resp.raise_for_status()
        data = resp.json()
    return [_page_to_runbook(p) for p in data.get("results", [])]


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


async def get_notion_runbook(api_key: str, page_id: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=12.0) as client:
        page_resp = await client.get(f"{_BASE}/pages/{page_id}", headers=_headers(api_key))
        page_resp.raise_for_status()
        page = page_resp.json()
        blocks_resp = await client.get(
            f"{_BASE}/blocks/{page_id}/children", headers=_headers(api_key), params={"page_size": 100}
        )
        blocks_resp.raise_for_status()
        blocks = blocks_resp.json().get("results", [])
    rb = _page_to_runbook(page)
    rb["content"] = _blocks_to_markdown(blocks) or "(empty runbook)"
    return rb


def _markdown_to_blocks(markdown: str, limit: int = 95) -> List[Dict[str, Any]]:
    """Best-effort inverse of ``_blocks_to_markdown`` for writing generated
    runbooks into Notion. ``limit`` stays under the API's 100-children-per-call cap.
    """
    blocks: List[Dict[str, Any]] = []
    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        if not line or len(blocks) >= limit:
            continue
        if line.startswith("### "):
            blocks.append(_rich_block("heading_3", line[4:]))
        elif line.startswith("## "):
            blocks.append(_rich_block("heading_2", line[3:]))
        elif line.startswith("# "):
            blocks.append(_rich_block("heading_1", line[2:]))
        elif line.startswith("- ") or line.startswith("* "):
            blocks.append(_rich_block("bulleted_list_item", line[2:]))
        elif line.startswith("> "):
            blocks.append(_rich_block("quote", line[2:]))
        else:
            blocks.append(_rich_block("paragraph", line))
    return blocks


def _rich_block(block_type: str, text: str) -> Dict[str, Any]:
    return {
        "object": "block",
        "type": block_type,
        block_type: {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]},
    }


def _find_key(schema: Dict[str, Any], *names: str) -> Optional[str]:
    lower = {k.lower(): k for k in schema.keys()}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    return None


def _property_value(prop_type: str, value: str) -> Dict[str, Any]:
    if prop_type == "select":
        return {"select": {"name": value[:100]}}
    if prop_type == "status":
        return {"status": {"name": value[:100]}}
    if prop_type == "multi_select":
        return {"multi_select": [{"name": v.strip()[:100]} for v in value.split(",") if v.strip()]}
    return {"rich_text": [{"type": "text", "text": {"content": value[:2000]}}]}


async def _database_schema(client: httpx.AsyncClient, api_key: str, database_id: str) -> Dict[str, Any]:
    resp = await client.get(f"{_BASE}/databases/{database_id}", headers=_headers(api_key))
    resp.raise_for_status()
    return resp.json().get("properties", {}) or {}


async def upsert_notion_runbook(
    api_key: str,
    database_id: str,
    *,
    title: str,
    markdown_body: str,
    service: str = "",
    incident_type: str = "",
    severity: str = "",
) -> Dict[str, Any]:
    """Create or replace this cluster's Notion page for an auto-generated runbook.

    Matches an existing page by exact title — auto-generated titles are
    deterministic per (failure_class, service), mirroring the old local
    corpus's one-file-per-signature overwrite. Notion has no single-call
    "replace this page's content" operation, so a matching page is archived
    before the replacement is created (recoverable from Notion's trash, not
    deleted outright).

    No schema assumptions beyond "there is a title property" — service/
    incident_type/severity are only set when the database actually has a
    same-named property (of whatever type it was configured as).
    """
    body = markdown_body.split("---", 2)[2].strip() if markdown_body.startswith("---") else markdown_body

    async with httpx.AsyncClient(timeout=12.0) as client:
        schema = await _database_schema(client, api_key, database_id)
        title_key = _find_key(schema, "title") or next(
            (k for k, v in schema.items() if v.get("type") == "title"), "Name"
        )
        properties: Dict[str, Any] = {
            title_key: {"title": [{"type": "text", "text": {"content": title[:2000]}}]}
        }
        for label, value in (("service", service), ("incident type", incident_type), ("severity", severity)):
            if not value:
                continue
            key = _find_key(schema, label, label.replace(" ", "_"))
            if key:
                properties[key] = _property_value(schema[key].get("type", "rich_text"), value)

        existing = await list_notion_runbooks(api_key, database_id)
        existing_id = next((rb["id"] for rb in existing if rb.get("title") == title), None)
        if existing_id:
            archive_resp = await client.patch(
                f"{_BASE}/pages/{existing_id}", headers=_headers(api_key), json={"archived": True}
            )
            archive_resp.raise_for_status()

        create_resp = await client.post(
            f"{_BASE}/pages",
            headers=_headers(api_key),
            json={
                "parent": {"database_id": database_id},
                "properties": properties,
                "children": _markdown_to_blocks(body),
            },
        )
        create_resp.raise_for_status()
        page = create_resp.json()

    return _page_to_runbook(page)
