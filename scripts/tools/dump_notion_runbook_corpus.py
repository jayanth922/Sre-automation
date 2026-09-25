#!/usr/bin/env python3
"""Dump a tenant's live Notion runbook corpus to the JSON the audit reads.

`scripts/tools/audit_runbook_coverage.py` grades a corpus dump, not Notion itself,
so the dump has to come from the same read path the agent uses — otherwise
the score describes a corpus nobody reads. This uses
`sre_agent.notion_runbooks`, which is the paginated reader the MCP server
mirrors.

Its main use is the read-back after a publish: publish, dump, re-audit. A
22/22 on local files says the drafts are right; a 22/22 on a fresh dump says
Notion is actually serving them.

Credentials, in order of preference:
  --api-key / --database-id
  NOTION_API_KEY / NOTION_DATABASE_ID in the environment
  --cluster-id, read from the platform database (decrypted via
  CREDENTIAL_ENCRYPTION_KEY) — the Codespace path, since that is where the
  Meridian cluster's credentials live.

Usage:
    python scripts/tools/dump_notion_runbook_corpus.py --out /tmp/corpus.json
    python scripts/tools/dump_notion_runbook_corpus.py --cluster-id <uuid> --out /tmp/corpus.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from sre_agent.notion_runbooks import (  # noqa: E402
    _all_block_children,
    _all_database_pages,
    _blocks_to_markdown,
    _headers,
    _prop_text,
)


def _creds_from_cluster(cluster_id: str) -> Tuple[str, str]:
    """Read this cluster's Notion credentials out of the platform database.

    `notion_api_key` is an `EncryptedString` column, so this only works where
    `CREDENTIAL_ENCRYPTION_KEY` and `DATABASE_URL` are set — the Codespace.
    """
    from backend.database import SessionLocal  # noqa: PLC0415
    from backend.models import Cluster  # noqa: PLC0415

    with SessionLocal() as session:
        cluster = session.get(Cluster, cluster_id)
        if cluster is None:
            raise SystemExit(f"no cluster {cluster_id}")
        if not (cluster.notion_api_key and cluster.notion_database_id):
            raise SystemExit(f"cluster {cluster_id} has no Notion runbook database configured")
        return cluster.notion_api_key, cluster.notion_database_id


def _resolve_creds(args: argparse.Namespace) -> Tuple[str, str]:
    api_key = args.api_key or os.getenv("NOTION_API_KEY")
    database_id = args.database_id or os.getenv("NOTION_DATABASE_ID")
    if api_key and database_id:
        return api_key, database_id
    if args.cluster_id:
        return _creds_from_cluster(args.cluster_id)
    raise SystemExit(
        "no Notion credentials: pass --api-key/--database-id, set "
        "NOTION_API_KEY/NOTION_DATABASE_ID, or pass --cluster-id on a host "
        "with the platform database and CREDENTIAL_ENCRYPTION_KEY"
    )


async def dump(api_key: str, database_id: str) -> Dict[str, Any]:
    import httpx  # noqa: PLC0415

    async with httpx.AsyncClient(timeout=30.0) as client:
        db_resp = await client.get(
            f"https://api.notion.com/v1/databases/{database_id}", headers=_headers(api_key)
        )
        db_resp.raise_for_status()
        database = db_resp.json()
        db_title = "".join(
            x.get("plain_text", "") for x in database.get("title", [])
        ) or "runbooks"

        raw_pages = await _all_database_pages(client, api_key, database_id)

        pages: List[Dict[str, Any]] = []
        for page in raw_pages:
            blocks = await _all_block_children(client, api_key, page["id"])
            pages.append(
                {
                    "id": page.get("id", ""),
                    "url": page.get("url", ""),
                    "db_title": db_title,
                    # Flattened to plain strings, the shape
                    # audit_runbook_coverage.py's `_normalize_corpus` expects.
                    "properties": {
                        name: _prop_text(prop)
                        for name, prop in (page.get("properties") or {}).items()
                    },
                    "content": _blocks_to_markdown(blocks),
                }
            )

    return {
        "pages": pages,
        "databases": [
            {
                "id": database.get("id", ""),
                "title": db_title,
                "properties": sorted((database.get("properties") or {}).keys()),
            }
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="where to write the JSON dump")
    parser.add_argument("--api-key")
    parser.add_argument("--database-id")
    parser.add_argument("--cluster-id", help="read credentials from the platform database")
    args = parser.parse_args()

    api_key, database_id = _resolve_creds(args)
    corpus = asyncio.run(dump(api_key, database_id))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")

    empty = [p for p in corpus["pages"] if not p["content"].strip()]
    print(f"wrote {len(corpus['pages'])} pages to {args.out}")
    if empty:
        # An empty body usually means the integration can read the database
        # row but was never shared on the page itself.
        print(f"WARNING: {len(empty)} page(s) came back with no content:")
        for page in empty[:10]:
            print(f"  {page['properties'].get('Name') or page['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
