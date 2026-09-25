"""Fetch traces fresh from the tenant's Langfuse project and print an audit view.

Auditing instrumentation means reading the trace the run actually produced, not
the one the code looks like it should produce — so this always fetches live and
never caches.

Must run *inside* the API container: it resolves the org's Langfuse keys through
``ExecutionContext``, so the encrypted credentials are decrypted where they
already live and never travel to a developer machine.

    docker cp scripts/tools/langfuse_trace_audit.py sre-agent-api:/app/
    docker exec -w /app sre-agent-api uv run python langfuse_trace_audit.py
    docker exec -w /app sre-agent-api uv run python langfuse_trace_audit.py <trace_id>

The first form lists recent traces (name, session, user, tags, cost); the second
prints one trace's full observation tree with levels and status messages, which
is what tells you whether failed work is findable.
"""

import asyncio
import base64
import json
import sys


async def _creds():
    from sqlalchemy import select

    from backend import database, models
    from sre_agent.execution_context import ExecutionContext

    async with database.AsyncSessionLocal() as db:
        cluster = (await db.execute(select(models.Cluster).limit(1))).scalars().first()
        org = await db.get(models.Organization, cluster.org_id)
    ctx = ExecutionContext.from_cluster(cluster, organization=org)
    c = ctx.org_langfuse_credentials() or {}
    host = (c.get("host") or "https://cloud.langfuse.com").rstrip("/")
    auth = base64.b64encode(f"{c['public_key']}:{c['secret_key']}".encode()).decode()
    return host, {"Authorization": f"Basic {auth}"}


def _short(value, limit=400):
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + f"…(+{len(text) - limit})"


async def main():
    import httpx

    host, headers = await _creds()
    async with httpx.AsyncClient(timeout=60, headers=headers) as client:
        if len(sys.argv) < 2:
            r = await client.get(f"{host}/api/public/traces", params={"limit": 15})
            r.raise_for_status()
            for t in r.json().get("data", []):
                print(json.dumps({
                    "id": t.get("id"),
                    "name": t.get("name"),
                    "timestamp": t.get("timestamp"),
                    "sessionId": t.get("sessionId"),
                    "userId": t.get("userId"),
                    "tags": t.get("tags"),
                    "environment": t.get("environment"),
                    "latency": t.get("latency"),
                    "totalCost": t.get("totalCost"),
                    "observations": len(t.get("observations") or []),
                    "input": _short(t.get("input"), 200),
                    "output": _short(t.get("output"), 200),
                }, default=str))
            return

        trace_id = sys.argv[1]
        r = await client.get(f"{host}/api/public/traces/{trace_id}")
        r.raise_for_status()
        trace = r.json()
        print("== TRACE ==")
        print(json.dumps({
            "id": trace.get("id"),
            "name": trace.get("name"),
            "sessionId": trace.get("sessionId"),
            "userId": trace.get("userId"),
            "tags": trace.get("tags"),
            "environment": trace.get("environment"),
            "release": trace.get("release"),
            "version": trace.get("version"),
            "latency": trace.get("latency"),
            "totalCost": trace.get("totalCost"),
            "metadata": _short(trace.get("metadata"), 900),
            "input": _short(trace.get("input"), 900),
            "output": _short(trace.get("output"), 900),
        }, indent=2, default=str))

        obs = trace.get("observations") or []
        by_parent = {}
        for o in obs:
            by_parent.setdefault(o.get("parentObservationId"), []).append(o)
        for kids in by_parent.values():
            kids.sort(key=lambda o: o.get("startTime") or "")

        print(f"\n== OBSERVATIONS ({len(obs)}) ==")

        def walk(parent, depth):
            for o in by_parent.get(parent, []):
                pad = "  " * depth
                row = {
                    "type": o.get("type"),
                    "name": o.get("name"),
                    "model": o.get("model"),
                    "usage": (o.get("usageDetails") or None),
                    "cost": o.get("calculatedTotalCost") or o.get("totalCost"),
                    "level": o.get("level"),
                    # The level says something failed; the status message says
                    # *what*. Printing only the level hides whether every failed
                    # action is named or just the last one.
                    "statusMessage": _short(o.get("statusMessage"), 240),
                    "ms": o.get("latency"),
                }
                print(pad + json.dumps({k: v for k, v in row.items() if v not in (None, {}, "")}, default=str))
                if o.get("input") is not None or o.get("output") is not None:
                    print(pad + "   in : " + str(_short(o.get("input"), 240)))
                    print(pad + "   out: " + str(_short(o.get("output"), 240)))
                else:
                    print(pad + "   !! no input and no output")
                md = o.get("metadata")
                if md:
                    print(pad + "   md : " + str(_short(md, 240)))
                walk(o.get("id"), depth + 1)

        walk(None, 0)

        types = {}
        for o in obs:
            types[o.get("type")] = types.get(o.get("type"), 0) + 1
        print("\n== TYPE HISTOGRAM ==")
        print(json.dumps(types, indent=2))


asyncio.run(main())
