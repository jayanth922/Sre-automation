"""Notion-backed runbook source.

Production runbooks usually live in Notion, per team. When a cluster is
configured with Notion credentials, the runbook catalog and content are read
from the client's Notion database instead of the local markdown corpus — no
schema assumptions beyond "there is a title property"; service/incident-type/
severity are pulled from same-named properties when present.

Uses the Notion REST API directly (httpx) — no extra SDK dependency.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

NOTION_VERSION = "2022-06-28"
_BASE = "https://api.notion.com/v1"

# Notion returns at most 100 results per call and accepts at most 100 children
# per create/append. Both limits are silent: the API reports success and sets
# `has_more`. A reader that ignores it drops the tail of a long page — and a
# runbook's tail is where Verification lives, so the agent would get every
# remediation branch and lose the probe that says whether the one it chose
# worked. That is the same defect the runbook-brief budget fix closed at the
# other end of the pipe.
NOTION_PAGE_SIZE = 100

# One rich_text item caps at 2000 characters. Split, never truncate.
_MAX_RICH_TEXT = 2000


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


async def _all_database_pages(
    client: httpx.AsyncClient, api_key: str, database_id: str
) -> List[Dict[str, Any]]:
    """Every page in the database, following Notion's cursor to the end."""
    pages: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    while True:
        body: Dict[str, Any] = {"page_size": NOTION_PAGE_SIZE}
        if cursor:
            body["start_cursor"] = cursor
        resp = await client.post(
            f"{_BASE}/databases/{database_id}/query", headers=_headers(api_key), json=body
        )
        resp.raise_for_status()
        payload = resp.json()
        pages.extend(payload.get("results", []))
        cursor = payload.get("next_cursor")
        if not payload.get("has_more") or not cursor:
            return pages


async def _all_block_children(
    client: httpx.AsyncClient, api_key: str, block_id: str
) -> List[Dict[str, Any]]:
    """Every child block of a page, following Notion's cursor to the end."""
    blocks: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    while True:
        params: Dict[str, Any] = {"page_size": NOTION_PAGE_SIZE}
        if cursor:
            params["start_cursor"] = cursor
        resp = await client.get(
            f"{_BASE}/blocks/{block_id}/children", headers=_headers(api_key), params=params
        )
        resp.raise_for_status()
        payload = resp.json()
        blocks.extend(payload.get("results", []))
        cursor = payload.get("next_cursor")
        if not payload.get("has_more") or not cursor:
            return blocks


async def list_notion_runbooks(api_key: str, database_id: str) -> List[Dict[str, Any]]:
    async with httpx.AsyncClient(timeout=12.0) as client:
        pages = await _all_database_pages(client, api_key, database_id)
    return [_page_to_runbook(p) for p in pages]


def _blocks_to_markdown(blocks: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    ordinal = 0
    for b in blocks:
        t = b.get("type", "")
        rich = (b.get(t) or {}).get("rich_text", []) if isinstance(b.get(t), dict) else []
        text = "".join(x.get("plain_text", "") for x in rich)
        if t == "numbered_list_item":
            # Notion does not store the number; it renders position. Emitting a
            # literal "1." for every item turned a five-step procedure into
            # "1. 1. 1. 1. 1." by the time the agent read it, throwing away the
            # ordering — which in a remediation procedure is the instruction.
            ordinal += 1
            lines.append(f"{ordinal}. {text}")
            continue
        ordinal = 0
        if t == "heading_1":
            lines.append(f"# {text}")
        elif t == "heading_2":
            lines.append(f"## {text}")
        elif t == "heading_3":
            lines.append(f"### {text}")
        elif t == "bulleted_list_item":
            lines.append(f"- {text}")
        elif t == "quote":
            lines.append(f"> {text}")
        elif t == "code":
            lines.append(f"```\n{text}\n```")
        elif t == "paragraph":
            # Including the empty ones: a blank paragraph is how a blank line
            # survives the round trip, and blank lines separate sections.
            lines.append(text)
        elif text:
            lines.append(text)
    return "\n".join(lines)


async def get_notion_runbook(api_key: str, page_id: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=12.0) as client:
        page_resp = await client.get(f"{_BASE}/pages/{page_id}", headers=_headers(api_key))
        page_resp.raise_for_status()
        page = page_resp.json()
        blocks = await _all_block_children(client, api_key, page_id)
    rb = _page_to_runbook(page)
    rb["content"] = _blocks_to_markdown(blocks) or "(empty runbook)"
    return rb


# Notion's `code` block rejects a language outside its own enum, and a
# rejected block fails the whole call. Anything unrecognised becomes plain
# text: a runbook's PromQL renders the same either way, and a failed publish
# for the sake of syntax highlighting is a bad trade.
_CODE_LANGUAGES = {
    "bash", "c", "c++", "c#", "css", "diff", "docker", "go", "graphql", "html",
    "java", "javascript", "json", "kotlin", "makefile", "markdown", "mermaid",
    "php", "plain text", "powershell", "protobuf", "python", "ruby", "rust",
    "scala", "shell", "sql", "swift", "typescript", "xml", "yaml",
}
_DEFAULT_CODE_LANGUAGE = "plain text"


def _code_language(fence_info: str) -> str:
    candidate = fence_info.strip().lower()
    if candidate in _CODE_LANGUAGES:
        return candidate
    return {"sh": "shell", "js": "javascript", "ts": "typescript", "yml": "yaml"}.get(
        candidate, _DEFAULT_CODE_LANGUAGE
    )


def _markdown_to_blocks(markdown: str) -> List[Dict[str, Any]]:
    """Inverse of ``_blocks_to_markdown``, faithful enough to round-trip.

    There is deliberately no block limit. The previous ``limit=95`` silently
    dropped every line past the 95th, which is below two of the four shipped
    Meridian runbooks (137 and 117 blocks) — and what fell off the end was
    Verification. Long pages are written in chunks by the caller instead; see
    ``NOTION_PAGE_SIZE``.

    Two things are preserved as literal text rather than converted:

    - **Numbered steps.** ``2. rollback_deployment`` stays a paragraph, so the
      number survives. Notion's ``numbered_list_item`` stores no number, and
      a remediation procedure whose steps all read "1." has lost its order.
    - **Inline emphasis.** ``**0.45**`` keeps its asterisks. Parsing inline
      markdown into rich_text annotations would make the page prettier and the
      round trip lossy; the threshold matters more than the bold.
    """
    blocks: List[Dict[str, Any]] = []
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if line.startswith("```"):
            language = _code_language(line[3:])
            i += 1
            body: List[str] = []
            while i < len(lines) and not lines[i].rstrip().startswith("```"):
                body.append(lines[i].rstrip())
                i += 1
            i += 1  # the closing fence, or one past the end if it is missing
            blocks.append(_code_block("\n".join(body), language))
            continue
        i += 1
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
            # Blank lines included: an empty paragraph is how a blank line
            # survives, and blank lines are what separate sections once the
            # page is read back as markdown.
            blocks.append(_rich_block("paragraph", line))
    return blocks


def _rich_text(text: str) -> List[Dict[str, Any]]:
    """Notion caps one rich_text item at 2000 characters — split, never truncate."""
    if not text:
        return []
    return [
        {"type": "text", "text": {"content": text[i : i + _MAX_RICH_TEXT]}}
        for i in range(0, len(text), _MAX_RICH_TEXT)
    ]


def _rich_block(block_type: str, text: str) -> Dict[str, Any]:
    return {
        "object": "block",
        "type": block_type,
        block_type: {"rich_text": _rich_text(text)},
    }


def _code_block(code: str, language: str = _DEFAULT_CODE_LANGUAGE) -> Dict[str, Any]:
    return {
        "object": "block",
        "type": "code",
        "code": {"rich_text": _rich_text(code), "language": language},
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


async def _append_children(
    client: httpx.AsyncClient,
    api_key: str,
    page_id: str,
    blocks: List[Dict[str, Any]],
) -> None:
    """Append blocks in Notion-sized chunks, checking every call.

    A partial page is a runbook missing its tail, so each chunk must fail
    loudly rather than leave a page that looks published.
    """
    for start in range(0, len(blocks), NOTION_PAGE_SIZE):
        resp = await client.patch(
            f"{_BASE}/blocks/{page_id}/children",
            headers=_headers(api_key),
            json={"children": blocks[start : start + NOTION_PAGE_SIZE]},
        )
        resp.raise_for_status()


async def replace_notion_page_body(
    api_key: str,
    page_id: str,
    markdown_body: str,
    *,
    pace_seconds: float = 0.34,
) -> int:
    """Swap a page's content, keeping the page itself.

    ``upsert_notion_runbook`` archives the old page and creates a new one,
    because that is the only way to place a *generated* runbook whose page may
    not exist yet. For a curated page that already exists that trade is wrong:
    a new page means a new id and, worse, only the properties the caller
    happened to pass. Retrieval scores against those properties — tags,
    alert_name, owner_team — so recreating a page silently degrades the
    ranking that was measured at 22/22.

    Notion has no replace-body call, so this deletes the existing children and
    appends the new ones. ``pace_seconds`` keeps the delete loop under
    Notion's ~3 requests/second average; 429s are retried on Retry-After.

    Returns the number of blocks written.
    """
    blocks = _markdown_to_blocks(markdown_body)
    async with httpx.AsyncClient(timeout=30.0) as client:
        existing = await _all_block_children(client, api_key, page_id)
        for block in existing:
            await _delete_block(client, api_key, block["id"])
            if pace_seconds:
                await asyncio.sleep(pace_seconds)
        await _append_children(client, api_key, page_id, blocks)
    return len(blocks)


async def _delete_block(
    client: httpx.AsyncClient, api_key: str, block_id: str, attempts: int = 4
) -> None:
    for attempt in range(attempts):
        resp = await client.delete(f"{_BASE}/blocks/{block_id}", headers=_headers(api_key))
        if resp.status_code != 429:
            resp.raise_for_status()
            return
        # Notion's own backoff is authoritative; guessing one is how a bulk
        # delete half-finishes and leaves a page with two runbooks in it.
        wait = float(resp.headers.get("Retry-After", "1"))
        logger.warning("Notion rate-limited a block delete; retrying in %.1fs", wait)
        await asyncio.sleep(wait)
    raise RuntimeError(f"Notion kept rate-limiting the delete of block {block_id}")


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

        existing_pages = await _all_database_pages(client, api_key, database_id)
        existing_id = next(
            (
                p.get("id")
                for p in existing_pages
                if _page_to_runbook(p).get("title") == title
            ),
            None,
        )
        if existing_id:
            archive_resp = await client.patch(
                f"{_BASE}/pages/{existing_id}", headers=_headers(api_key), json={"archived": True}
            )
            archive_resp.raise_for_status()

        blocks = _markdown_to_blocks(body)
        create_resp = await client.post(
            f"{_BASE}/pages",
            headers=_headers(api_key),
            json={
                "parent": {"database_id": database_id},
                "properties": properties,
                "children": blocks[:NOTION_PAGE_SIZE],
            },
        )
        create_resp.raise_for_status()
        page = create_resp.json()

        # Notion accepts at most 100 children per call, so the rest follow.
        await _append_children(client, api_key, page.get("id", ""), blocks[NOTION_PAGE_SIZE:])

    return _page_to_runbook(page)
