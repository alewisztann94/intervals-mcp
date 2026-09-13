"""End-to-end test: real MCP client -> real MCP server -> mock intervals.icu.

Checks that the secret path is enforced, that every tool is listed, and that
each tool returns a sane shape against known-good fake data.
"""

import asyncio
import json
import os
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

SECRET = os.environ["MCP_SECRET_PATH"]
PORT = os.environ.get("PORT", "8080")
URL = f"http://127.0.0.1:{PORT}/{SECRET}/mcp"
WRONG_URL = f"http://127.0.0.1:{PORT}/not-the-secret/mcp"

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}{(' — ' + detail) if detail and not condition else ''}")
    if not condition:
        failures.append(label)


def payload(result) -> dict:
    """Unwrap a CallToolResult into the dict the tool returned."""
    if result.structuredContent is not None:
        sc = result.structuredContent
        # FastMCP wraps bare returns under "result" for non-object schemas
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(result.content[0].text)


async def main() -> None:
    print("\n1. Secret path enforcement")
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"http://127.0.0.1:{PORT}/healthz")
        check("healthz responds 200", r.status_code == 200, str(r.status_code))
        r = await c.post(WRONG_URL, json={}, headers={"Accept": "application/json, text/event-stream"})
        check("wrong path is rejected (404)", r.status_code == 404, f"got {r.status_code}")

    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print("\n2. Tool discovery")
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"     tools: {names}")
            expected = sorted(
                ["training_summary", "list_activities", "activity_detail", "wellness", "pace_at_hr_trend"]
            )
            check("all five tools exposed", names == expected, str(names))
            check(
                "every tool has a description",
                all((t.description or "").strip() for t in tools.tools),
            )

            print("\n3. training_summary")
            res = payload(await session.call_tool("training_summary", {"weeks": 6}))
            check("has weekly buckets", bool(res.get("weekly")))
            check("reports 6 week window", res["window"]["weeks"] == 6)
            wk = sorted(res["weekly"])[-1]
            sports = res["weekly"][wk]
            print(f"     latest week {wk}: {sports}")
            check("splits by sport", "Run" in sports)
            check("current fitness present", res.get("current_fitness") is not None)
            check(
                "ctl/atl/tsb all present",
                all(res["current_fitness"].get(k) is not None for k in ("fitness_ctl", "fatigue_atl", "form_tsb")),
            )

            print("\n4. list_activities")
            res = payload(await session.call_tool("list_activities", {"days": 10, "activity_type": "Run"}))
            check("returns rows", res["count"] > 0, str(res["count"]))
            check("filter applied", all(a["type"] == "Run" for a in res["activities"]))
            first = res["activities"][0]
            print(f"     newest: {first['date']} {first['km']}km @ {first['pace_min_km']} hr {first['avg_hr']}")
            check("pace computed", first["pace_min_km"] is not None)
            check("sorted newest first", res["activities"][0]["date"] >= res["activities"][-1]["date"])

            print("\n5. list_activities — bike only")
            res = payload(await session.call_tool("list_activities", {"days": 30, "activity_type": "Ride"}))
            check("finds rides", res["count"] > 0, str(res["count"]))
            check("watts surfaced", res["activities"][0]["avg_watts"] is not None)

            print("\n6. activity_detail — structured session")
            res = payload(await session.call_tool("list_activities", {"days": 7, "activity_type": "Run"}))
            hard = next((a for a in res["activities"] if a["avg_hr"] and a["avg_hr"] > 140), None)
            check("found a hard run to inspect", hard is not None)
            if hard:
                det = payload(await session.call_tool("activity_detail", {"activity_id": hard["id"]}))
                check("intervals returned", len(det["intervals"]) > 0, str(len(det["intervals"])))
                print(f"     {len(det['intervals'])} reps, first: {det['intervals'][0]}")
                check("rep pace computed", det["intervals"][0]["pace_min_km"] is not None)

            print("\n7. activity_detail — easy run (API 422s, must not crash)")
            easy = next((a for a in res["activities"] if a["avg_hr"] and a["avg_hr"] < 130), None)
            if easy:
                det = payload(await session.call_tool("activity_detail", {"activity_id": easy["id"]}))
                check("degrades gracefully", det["intervals"] == [] and det["note"] is not None)
                print(f"     note: {det['note'][:70]}...")
            else:
                print("     (no easy run in window, skipped)")

            print("\n8. wellness")
            res = payload(await session.call_tool("wellness", {"days": 30}))
            check("returns records", res["days"] > 0, str(res["days"]))
            check("resting hr present", res["records"][0]["resting_hr"] is not None)
            check("trend computed", res["trends"]["resting_hr"] is not None)
            print(f"     resting hr trend: {res['trends']['resting_hr']}")

            print("\n9. pace_at_hr_trend")
            res = payload(await session.call_tool("pace_at_hr_trend", {"weeks": 12, "activity_type": "Run"}))
            check("weekly series built", len(res["weekly"]) >= 4, str(len(res["weekly"])))
            check("verdict computed", res.get("verdict") is not None)
            print(f"     verdict: {res['verdict']}")
            check(
                "detects the improving trend baked into the mock",
                res["verdict"]["reading"] == "improving",
                res["verdict"]["reading"],
            )
            check("caveat included", "comparable" in res["caveat"])

            print("\n10. bad input handling")
            res = payload(await session.call_tool("training_summary", {"weeks": 999}))
            check("clamps absurd window", res["window"]["weeks"] == 52, str(res["window"]["weeks"]))
            bad = await session.call_tool("activity_detail", {"activity_id": "does-not-exist"})
            check("unknown id surfaces an error rather than hanging", bad.isError is True)
            bad = await session.call_tool("activity_detail", {"activity_id": "../athlete/0/wellness"})
            check("path traversal in activity_id is refused", bad.isError is True)

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("All checks passed.")


asyncio.run(main())
