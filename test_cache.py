"""Tests for the upstream response cache.

Proves the cache actually prevents calls to intervals.icu (by counting requests
on the mock), that it expires, that different arguments don't collide, that
concurrent identical calls fetch once rather than stampeding, and — importantly —
that failures are never cached.

Run the server with CACHE_TTL_SECONDS=3 so expiry is testable.
"""

import asyncio
import json
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = "http://127.0.0.1:8080/s3cr3t-test-path/mcp"
STATS = "http://127.0.0.1:9001/api/v1/_stats"
failures = []


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{(' — ' + str(detail)) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


async def upstream(reset=False):
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(STATS + ("?reset=1" if reset else ""))
        return r.json()


async def main():
    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as s:
            await s.initialize()

            print("\n1. Repeat calls hit the cache, not intervals.icu")
            await upstream(reset=True)
            for _ in range(5):
                await s.call_tool("training_summary", {"weeks": 6})
            hits = await upstream()
            print(f"     5 identical tool calls -> {hits['activities']} upstream request(s)")
            check("only one upstream fetch", hits["activities"] == 1, hits["activities"])

            print("\n2. Different arguments are cached separately")
            # Use windows not touched above, so both start cold — resetting the
            # mock's counter does not clear the server's cache.
            await upstream(reset=True)
            await s.call_tool("training_summary", {"weeks": 7})
            await s.call_tool("training_summary", {"weeks": 9})
            await s.call_tool("training_summary", {"weeks": 7})
            hits = await upstream()
            print(f"     two distinct windows, one repeated -> {hits['activities']} upstream request(s)")
            check("distinct windows fetched separately", hits["activities"] == 2, hits["activities"])
            check("and the repeat was served from cache", hits["activities"] != 3, hits["activities"])

            print("\n3. Concurrent identical calls don't stampede")
            await upstream(reset=True)
            await asyncio.gather(*(s.call_tool("wellness", {"days": 30}) for _ in range(8)))
            hits = await upstream()
            print(f"     8 concurrent calls -> {hits['wellness']} upstream request(s)")
            check("single fetch under concurrency", hits["wellness"] == 1, hits["wellness"])

            print("\n4. Cache expires (TTL is 3s for this run)")
            await upstream(reset=True)
            await s.call_tool("wellness", {"days": 14})
            await asyncio.sleep(4)
            await s.call_tool("wellness", {"days": 14})
            hits = await upstream()
            print(f"     same call either side of TTL -> {hits['wellness']} upstream request(s)")
            check("refetched after expiry", hits["wellness"] == 2, hits["wellness"])

            print("\n5. Failures are never cached")
            await upstream(reset=True)
            for _ in range(3):
                res = await s.call_tool("activity_detail", {"activity_id": "nope"})
                assert res.isError
            hits = await upstream()
            print(f"     3 failing calls -> {hits['activity']} upstream request(s)")
            check(
                "every failure retried live, never served from cache",
                hits["activity"] == 3,
                hits["activity"],
            )

            print("\n6. Cache counters are visible on /<secret>/stats, not on public /healthz")
            async with httpx.AsyncClient(timeout=10) as c:
                health = (await c.get("http://127.0.0.1:8080/s3cr3t-test-path/stats")).json()
                public = (await c.get("http://127.0.0.1:8080/healthz")).json()
                wrong = await c.get("http://127.0.0.1:8080/not-the-secret/stats")
            print(f"     {health['cache']}")
            check("public healthz leaks no usage counters", "cache" not in public, public)
            check("stats under the wrong path 404s", wrong.status_code == 404, wrong.status_code)
            check("ttl reported", health["cache"]["ttl_seconds"] == 3)
            check("hits recorded", health["cache"]["hits"] > 0)
            check("entries bounded", health["cache"]["entries"] <= 64)

            print("\n7. Data still correct when served from cache")
            a = json.loads((await s.call_tool("training_summary", {"weeks": 8})).content[0].text)
            b = json.loads((await s.call_tool("training_summary", {"weeks": 8})).content[0].text)
            check("cached response identical to live one", a == b)

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("All cache checks passed.")


asyncio.run(main())
