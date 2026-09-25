"""The Notion round trip must not eat a runbook's tail.

Phase B rewrote the Meridian runbooks so they prescribe one branch per fault
instead of leaving the model to reason its way to a fix. All of that is worth
nothing if the publish or the read-back silently drops part of the page, and
both ends of this pipe were doing exactly that:

- the writer capped at 95 blocks (`_markdown_to_blocks(limit=95)`) against
  shipped runbooks of 174 and 153 blocks, dropping the rest with no error;
- every reader asked for 100 results and ignored Notion's `has_more`, so even
  a correctly written long page came back as its first 100 blocks.

What falls off the end of a runbook is Verification — the agent keeps every
remediation option and loses the probe that says whether the one it chose
worked. That is the same defect the runbook-brief budget fix closed at the
other end of the pipe, so these tests are the guard against reintroducing it
at this one.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

from sre_agent.notion_runbooks import (
    NOTION_PAGE_SIZE,
    _blocks_to_markdown,
    _markdown_to_blocks,
    _rich_text,
    get_notion_runbook,
    list_notion_runbooks,
    upsert_notion_runbook,
)

REPO = Path(__file__).resolve().parent.parent
RUNBOOK_DIR = REPO / "runbooks" / "meridian"


def notion_echo(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The same blocks as Notion hands them back: with `plain_text` filled in.

    Blocks we build carry `text.content`; the API response carries both that
    and `plain_text`, which is what the reader uses. Simulating the echo is
    what makes a local round-trip assertion mean something about the real one.
    """
    out = []
    for block in blocks:
        kind = block["type"]
        inner = dict(block[kind])
        inner["rich_text"] = [
            {**rt, "plain_text": rt["text"]["content"]} for rt in inner.get("rich_text", [])
        ]
        out.append({**block, kind: inner})
    return out


def round_trip(markdown: str) -> str:
    return _blocks_to_markdown(notion_echo(_markdown_to_blocks(markdown)))


# ── Fidelity ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", sorted(RUNBOOK_DIR.glob("*.md")), ids=lambda p: p.name)
def test_shipped_runbook_survives_the_notion_round_trip_byte_for_byte(path: Path):
    """Publishing then reading back must return the runbook that was audited.

    `scripts/tools/audit_runbook_coverage.py` scores the local file. That score only
    transfers to production if what Notion gives the agent is the same text.
    Exact equality is the right bar here: anything weaker would have passed
    while the writer was dropping every line past the 95th.
    """
    source = path.read_text()
    assert round_trip(source).strip() == source.strip()


def test_the_writer_has_no_block_limit():
    """A long page must not be silently shortened.

    The removed `limit=95` was below two of the four shipped runbooks. This
    fails if any cap comes back, whatever its value.
    """
    markdown = "\n".join(f"paragraph {i}" for i in range(400))
    blocks = _markdown_to_blocks(markdown)
    assert len(blocks) == 400
    assert round_trip(markdown).strip().endswith("paragraph 399")


def test_numbered_steps_keep_their_order_when_read_back():
    """Notion stores no number on a numbered_list_item; it renders position.

    Emitting a literal "1." for each one turned a five-step remediation
    procedure into "1. 1. 1. 1. 1." by the time the agent read it. In a
    procedure, the order is the instruction.
    """
    blocks = [
        {
            "type": "numbered_list_item",
            "numbered_list_item": {"rich_text": [{"plain_text": step}]},
        }
        for step in ("scale down", "roll back", "verify")
    ]
    assert _blocks_to_markdown(blocks) == "1. scale down\n2. roll back\n3. verify"


def test_numbering_restarts_after_an_interrupting_block():
    blocks = [
        {"type": "numbered_list_item", "numbered_list_item": {"rich_text": [{"plain_text": "a"}]}},
        {"type": "heading_2", "heading_2": {"rich_text": [{"plain_text": "Verification"}]}},
        {"type": "numbered_list_item", "numbered_list_item": {"rich_text": [{"plain_text": "b"}]}},
    ]
    assert _blocks_to_markdown(blocks) == "1. a\n## Verification\n1. b"


def test_a_long_line_is_split_across_rich_text_items_not_truncated():
    """Notion caps one rich_text item at 2000 characters.

    The old `text[:2000]` silently dropped the remainder of a long line.
    """
    line = "x" * 5000
    items = _rich_text(line)
    assert len(items) == 3
    assert "".join(i["text"]["content"] for i in items) == line


def test_an_unknown_code_fence_language_falls_back_to_plain_text():
    """Notion rejects a language outside its enum, and one rejected block
    fails the whole publish. A runbook's PromQL renders the same either way."""
    blocks = _markdown_to_blocks("```promql\nup == 0\n```")
    assert blocks[0]["code"]["language"] == "plain text"
    assert blocks[0]["code"]["rich_text"][0]["text"]["content"] == "up == 0"


def test_a_known_code_fence_language_is_kept():
    assert _markdown_to_blocks("```sql\nSELECT 1\n```")[0]["code"]["language"] == "sql"


# ── Pagination ──────────────────────────────────────────────────────────────


@pytest.fixture
def notion(monkeypatch):
    """A Notion stub that paginates, and records every request it served."""

    class Stub:
        def __init__(self):
            self.children: List[Dict[str, Any]] = []
            self.pages: List[Dict[str, Any]] = []
            self.requests: List[tuple] = []
            self.appended: List[List[Dict[str, Any]]] = []
            self.created_with: List[Dict[str, Any]] = []
            self.archived: List[str] = []
            self.deleted: List[str] = []
            self.fail_appends = False
            self.rate_limit_deletes = 0

        def _page_of(self, items, cursor):
            start = int(cursor or 0)
            window = items[start : start + NOTION_PAGE_SIZE]
            nxt = start + NOTION_PAGE_SIZE
            more = nxt < len(items)
            return {
                "results": window,
                "has_more": more,
                "next_cursor": str(nxt) if more else None,
            }

        def handle(self, request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else {}
            self.requests.append((request.method, request.url.path, body))
            path = request.url.path

            if path.endswith("/query"):
                return httpx.Response(
                    200, json=self._page_of(self.pages, body.get("start_cursor"))
                )
            if path.startswith("/v1/blocks/") and request.method == "GET":
                cursor = request.url.params.get("start_cursor")
                return httpx.Response(200, json=self._page_of(self.children, cursor))
            if path.startswith("/v1/blocks/") and request.method == "PATCH":
                if self.fail_appends:
                    return httpx.Response(502, json={"message": "nope"})
                self.appended.append(body["children"])
                return httpx.Response(200, json={"results": []})
            if path.startswith("/v1/blocks/") and request.method == "DELETE":
                if self.rate_limit_deletes > 0:
                    self.rate_limit_deletes -= 1
                    return httpx.Response(429, headers={"Retry-After": "0"}, json={})
                self.deleted.append(path.rsplit("/", 1)[-1])
                return httpx.Response(200, json={})
            if path.startswith("/v1/databases/"):
                return httpx.Response(200, json={"properties": {"Name": {"type": "title"}}})
            if path.startswith("/v1/pages/") and request.method == "PATCH":
                self.archived.append(path.rsplit("/", 1)[-1])
                return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1]})
            if path == "/v1/pages" and request.method == "POST":
                self.created_with.append(body)
                return httpx.Response(200, json={"id": "new-page", "properties": {}})
            if path.startswith("/v1/pages/"):
                return httpx.Response(200, json={"id": "p1", "properties": {}})
            raise AssertionError(f"unstubbed Notion call: {request.method} {path}")

    stub = Stub()
    transport = httpx.MockTransport(stub.handle)
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        kwargs.setdefault("base_url", "https://api.notion.com")
        return real(*args, **kwargs)

    monkeypatch.setattr("sre_agent.notion_runbooks.httpx.AsyncClient", factory)
    return stub


def _block(text: str) -> Dict[str, Any]:
    return {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": text}]}}


@pytest.mark.asyncio
async def test_reading_a_page_follows_the_cursor_past_the_first_hundred_blocks(notion):
    """The defect this closes: 137-block runbooks read back as 100 blocks,
    losing Verification, with the API reporting success."""
    notion.children = [_block(f"line {i}") for i in range(250)]

    runbook = await get_notion_runbook("key", "page-id")

    assert runbook["content"].splitlines()[-1] == "line 249"
    assert len(runbook["content"].splitlines()) == 250


@pytest.mark.asyncio
async def test_listing_the_database_follows_the_cursor(notion):
    notion.pages = [
        {"id": f"p{i}", "properties": {"Name": {"type": "title", "title": [{"plain_text": f"rb{i}"}]}}}
        for i in range(150)
    ]

    listed = await list_notion_runbooks("key", "db")

    assert len(listed) == 150
    assert listed[-1]["title"] == "rb149"


# ── Writing ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_appends_every_block_beyond_the_first_hundred(notion):
    """Notion takes at most 100 children per call, so a long runbook is a
    create plus appends. Dropping the appends would publish a page whose
    remediation branches are present and whose Verification is not."""
    markdown = "\n".join(f"line {i}" for i in range(250))

    await upsert_notion_runbook("key", "db", title="Long", markdown_body=markdown)

    created = notion.created_with[0]["children"]
    assert len(created) == NOTION_PAGE_SIZE
    assert [len(chunk) for chunk in notion.appended] == [100, 50]

    published = created + [b for chunk in notion.appended for b in chunk]
    assert _blocks_to_markdown(notion_echo(published)) == markdown


@pytest.mark.asyncio
async def test_upsert_archives_the_page_it_replaces(notion):
    """Notion has no replace-body call, so the old page is archived (into the
    trash, recoverable) rather than deleted. Matched on exact title."""
    notion.pages = [
        {
            "id": "old-page",
            "properties": {"Name": {"type": "title", "title": [{"plain_text": "Long"}]}},
        }
    ]

    await upsert_notion_runbook("key", "db", title="Long", markdown_body="body")

    assert notion.archived == ["old-page"]


@pytest.mark.asyncio
async def test_a_failed_append_fails_the_publish(notion):
    """A partial page is a runbook missing its tail. It must raise, not
    return a page that looks published."""
    notion.fail_appends = True

    with pytest.raises(httpx.HTTPStatusError):
        await upsert_notion_runbook(
            "key",
            "db",
            title="Long",
            markdown_body="\n".join(f"line {i}" for i in range(250)),
        )


# ── The MCP server carries its own copy ─────────────────────────────────────


def _load_mcp_server():
    path = REPO / "services" / "edge_mcp_servers" / "mcp_servers" / "runbooks_notion" / "server.py"
    spec = importlib.util.spec_from_file_location("runbooks_notion_server", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_mcp_server_renders_blocks_the_same_way_sre_agent_does():
    """`edge_mcp_servers` must never import `sre_agent`, so this logic is
    duplicated on purpose. Duplicated logic drifts, and the copy that drifts
    is the one the live agent actually reads through — so pin them together.
    """
    server = _load_mcp_server()
    for path in sorted(RUNBOOK_DIR.glob("*.md")):
        blocks = notion_echo(_markdown_to_blocks(path.read_text()))
        assert server._blocks_to_markdown(blocks) == _blocks_to_markdown(blocks), path.name


def test_the_mcp_server_agrees_on_the_page_size():
    assert _load_mcp_server().NOTION_PAGE_SIZE == NOTION_PAGE_SIZE


# ── Replacing a curated page in place ───────────────────────────────────────


@pytest.mark.asyncio
async def test_replacing_a_body_keeps_the_page_and_clears_the_old_blocks(notion):
    """`upsert` archives and recreates, which is right for a generated
    runbook and wrong for a curated one: the new page keeps only the
    properties the caller passed, and retrieval scores against properties
    this caller does not know about (tags, alert_name, owner_team). Replacing
    the body keeps the page id, the URL and every property."""
    from sre_agent.notion_runbooks import replace_notion_page_body

    notion.children = [{"id": f"b{i}", **_block(f"old {i}")} for i in range(120)]

    written = await replace_notion_page_body("key", "page-1", "new line", pace_seconds=0)

    assert notion.deleted == [f"b{i}" for i in range(120)], "every old block must go"
    assert notion.archived == [], "the page itself must survive"
    assert written == 1
    assert [len(chunk) for chunk in notion.appended] == [1]


@pytest.mark.asyncio
async def test_a_rate_limited_delete_is_retried_on_retry_after(notion):
    """Guessing a backoff is how a bulk delete half-finishes and leaves a page
    holding two runbooks at once."""
    from sre_agent.notion_runbooks import replace_notion_page_body

    notion.children = [{"id": "b0", **_block("old")}]
    notion.rate_limit_deletes = 2

    await replace_notion_page_body("key", "page-1", "new", pace_seconds=0)

    assert notion.deleted == ["b0"], "the delete must be retried, not skipped"


@pytest.mark.asyncio
async def test_a_delete_that_keeps_being_rate_limited_fails_the_replace(notion):
    """Better a loud failure than a page left holding half of two runbooks."""
    from sre_agent.notion_runbooks import replace_notion_page_body

    notion.children = [{"id": "b0", **_block("old")}]
    notion.rate_limit_deletes = 99

    with pytest.raises(RuntimeError, match="rate-limiting"):
        await replace_notion_page_body("key", "page-1", "new", pace_seconds=0)

    assert notion.appended == [], "nothing may be written onto a half-cleared page"


# ── The publish script's preflight ──────────────────────────────────────────


def _load_publisher():
    path = REPO / "scripts" / "tools" / "publish_meridian_runbooks.py"
    spec = importlib.util.spec_from_file_location("publish_meridian_runbooks", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_publisher_reads_the_title_from_the_h1():
    publisher = _load_publisher()
    for path in sorted(RUNBOOK_DIR.glob("*.md")):
        assert publisher._title_of(path.read_text()), f"{path.name} has no H1"


def test_the_publisher_round_trip_gate_rejects_what_the_encoding_would_change():
    """The gate that decides whether it is safe to write. It must reject a
    body the block encoding would alter, or the agent reads back something
    other than what the audit scored."""
    publisher = _load_publisher()
    assert publisher._round_trips("# Title\n\n- a bullet\n\n```\nup == 0\n```")
    # `*` bullets come back as `-`, and an unclosed fence comes back closed.
    # Both are harmless to read and both mean the published page is not the
    # file the audit scored, which is the thing this gate exists to catch.
    assert not publisher._round_trips("# Title\n\n* a star bullet")
    assert not publisher._round_trips("# Title\n\n```\nunclosed")
