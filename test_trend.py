"""Focused tests for pace_at_hr_trend: does it read a declining block correctly,
and does it warn when the weeks aren't comparable?

Run against a mock started with MOCK_DRIFT=0.006 (getting slower at the same HR).
The mock is generated relative to today, so a gentler drift can land inside the
±2% flat band on some dates and flake.
"""

import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = "http://127.0.0.1:8080/s3cr3t-test-path/mcp"
failures = []


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{(' — ' + str(detail)) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


def payload(res):
    if res.structuredContent is not None:
        sc = res.structuredContent
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(res.content[0].text)


async def main():
    async with streamablehttp_client(URL) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()

            print("\n1. Unbanded query where weekly session mix changes should warn")
            res = payload(await s.call_tool("pace_at_hr_trend", {"weeks": 16, "activity_type": "Run"}))
            hrs = [wk["mean_hr"] for wk in res["weekly"]]
            print(f"     weekly mean HR spread: {min(hrs):.0f}-{max(hrs):.0f}")
            check("confound warning raised when mix shifts", res["confound_warning"] is not None)
            if res["confound_warning"]:
                print(f"     {res['confound_warning'][:95]}...")

            print("\n1b. Partial edge weeks are dropped, not averaged in")
            check(
                "clipped week excluded",
                res["partial_weeks_dropped"] is not None,
                res["partial_weeks_dropped"],
            )
            print(f"     dropped: {res['partial_weeks_dropped']}")
            counts = [wk["sessions"] for wk in res["weekly"]]
            check("no unrepresentative 1-2 session weeks left", min(counts) >= 3, f"min {min(counts)}")

            print("\n2. Easy runs only (max_hr=130) — mock is declining")
            res = payload(
                await s.call_tool(
                    "pace_at_hr_trend", {"weeks": 16, "activity_type": "Run", "max_hr": 130}
                )
            )
            print(f"     verdict: {res['verdict']}")
            check("verdict computed", res["verdict"] is not None)
            check("reads as declining", res["verdict"]["reading"] == "declining", res["verdict"]["reading"])
            check("no confound warning once banded", res["confound_warning"] is None)
            check("excluded the hard sessions", res["hr_band"]["sessions_excluded"] > 0)
            hrs = [wk["mean_hr"] for wk in res["weekly"]]
            check("weekly HR now stable", max(hrs) - min(hrs) <= 6, f"{min(hrs)}-{max(hrs)}")

            print("\n3. Sub-threshold only (min_hr=140) — should also decline")
            res = payload(
                await s.call_tool(
                    "pace_at_hr_trend", {"weeks": 16, "activity_type": "Run", "min_hr": 140}
                )
            )
            print(f"     verdict: {res['verdict']}")
            check("reads as declining", res["verdict"]["reading"] == "declining", res["verdict"]["reading"])

            print("\n4. Sport with too little history returns no verdict")
            res = payload(await s.call_tool("pace_at_hr_trend", {"weeks": 12, "activity_type": "Swim"}))
            check("no false verdict on thin data", res["verdict"] is None, res["verdict"])
            print(f"     swim weeks available: {len(res['weekly'])}")

            print("\n5. Unknown sport returns empty, not an error")
            res = payload(await s.call_tool("pace_at_hr_trend", {"weeks": 12, "activity_type": "Kayak"}))
            check("empty series", res["weekly"] == [])
            check("no verdict", res["verdict"] is None)

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("All trend checks passed.")


asyncio.run(main())
