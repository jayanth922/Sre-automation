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
