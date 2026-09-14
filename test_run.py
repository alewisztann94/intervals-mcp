"""Tests for the run tools: race_history, compare_sessions, easy_pace_trend, and
grade-adjusted pace / elevation per km on run rows.

Run against the default mock (no MOCK_DRIFT), where running gets faster week on week.
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
    if res.isError:
        raise AssertionError(res.content[0].text)
    if res.structuredContent is not None:
        sc = res.structuredContent
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(res.content[0].text)


def secs(pace):
    m, s = pace.split(":")
    return int(m) * 60 + int(s)


async def main():
    async with streamablehttp_client(URL) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()

            print("\n1. Tools")
            names = sorted(t.name for t in (await s.list_tools()).tools)
            print(f"     {names}")
            check("run tools exposed", {"race_history", "compare_sessions", "easy_pace_trend"} <= set(names))
            check("pace_at_hr_trend and its efficiency_index are gone", "pace_at_hr_trend" not in names)

            print("\n2. Run rows carry GAP and elevation per km")
            runs = payload(await s.call_tool("list_activities", {"days": 42, "activity_type": "Run", "limit": 200}))
            outdoor = [a for a in runs["activities"] if a["type"] == "Run"]
            tread = [a for a in runs["activities"] if a["type"] == "VirtualRun"]
            check("outdoor runs have gap_min_km", all(a["gap_min_km"] for a in outdoor))
            check("outdoor runs have elevation_m_per_km", all(a["elevation_m_per_km"] is not None for a in outdoor))
            check("treadmill runs have no gap", tread and all(a["gap_min_km"] is None for a in tread))
            hilly = max(outdoor, key=lambda a: a["elevation_m_per_km"])
            print(f"     hilliest: {hilly['elevation_m_per_km']} m/km, pace {hilly['pace_min_km']} gap {hilly['gap_min_km']}")
            check("GAP is quicker than raw pace on a hilly run", secs(hilly["gap_min_km"]) < secs(hilly["pace_min_km"]))

            print("\n3. race_history")
            rh = payload(await s.call_tool("race_history", {}))
            for race in rh["races"]:
                print(f"     {race}")
            by_name = {race["name"]: race for race in rh["races"]}
            check("race-flagged run found even without a race-like name",
                  by_name.get("Perth - Saturday hit out", {}).get("found_by") == "race flag")
            check("parkrun found by name", by_name.get("Perth parkrun", {}).get("found_by") == "name")
            check("sessions named '3k' are not races", not any("3k" in n for n in by_name))
            check("club champs not included by default", "Perth - club champs" not in by_name)
            check("oldest first", [x["date"] for x in rh["races"]] == sorted(x["date"] for x in rh["races"]))
            park = by_name.get("Perth parkrun", {})
            check("finish time, pace, GAP and elevation reported",
                  all(park.get(k) for k in ("finish_time", "pace_min_km", "gap_min_km")) and
                  park.get("elevation_m_per_km") is not None, park)
            check("5k labelled as a standard distance", park.get("standard_distance") == "5k")
            rh2 = payload(await s.call_tool("race_history", {"name_contains": "club champs"}))
            check("name_contains adds unusually named results",
                  any(x["name"] == "Perth - club champs" and x["found_by"] == "name_contains" for x in rh2["races"]))

            print("\n4. compare_sessions: work reps only")
            cs = payload(await s.call_tool("compare_sessions", {"name_contains": "perth - 3k", "limit": 4}))
            print(f"     filter: {cs['rep_filter']}")
            for row in cs["comparison"]:
                print(f"     {row}")
            check("found sessions", cs["sessions_found"] == 4, cs["sessions_found"])
            check("min_rep_hr defaults to top of zone 1", cs["rep_filter"]["min_rep_hr"] == 140)
            last = cs["sessions"][-1]
            print(f"     left out of last session: {last['left_out']}")
            check("one set of reps per session", all(len(x["rep_sets"]) == 1 for x in cs["sessions"]))
            rep_set = last["rep_sets"][0]
            check("exactly the 10 reps", rep_set["rep_count"] == 10 and len(rep_set["reps"]) == 10, rep_set["rep_count"])
            check("recoveries left out and counted", last["left_out"]["recovery"] == 18, last["left_out"])
            check("stride left out as too short", last["left_out"]["too_short"] == 1, last["left_out"])
            check("warm-up and cool-down km left out as easy running", last["left_out"]["easy_running"] == 5,
                  last["left_out"])
            check("first rep kept despite lagging HR", rep_set["reps"][0]["avg_hr"] == 139)
            check("every rep has pace, GAP and HR",
                  all(x["pace_min_km"] and x["gap_min_km"] and x["avg_hr"] for x in rep_set["reps"]))
            check("no rep slower than 5:30/km (no warm-up or recovery leaked in)",
                  all(secs(x["pace_min_km"]) < 330 for x in rep_set["reps"]))
            check("comparison is oldest first",
                  [x["date"] for x in cs["comparison"]] == sorted(x["date"] for x in cs["comparison"]))

            print("\n5. compare_sessions: rep lengths never mixed")
            mixed = payload(await s.call_tool("compare_sessions", {"name_contains": "Timed medium", "limit": 2}))
            sets = mixed["sessions"][-1]["rep_sets"]
            print(f"     sets: {[(x['rep_count'], x['typical_rep_seconds']) for x in sets]}")
            check("1km and ~3min reps reported as separate sets", len(sets) == 2 and all(x["rep_count"] == 4 for x in sets))
            only_1k = payload(await s.call_tool("compare_sessions",
                                                {"name_contains": "Timed medium", "rep_seconds": 280, "limit": 2}))
            kept = only_1k["sessions"][-1]
            print(f"     rep_seconds=280: {[(x['rep_count'], x['typical_rep_seconds']) for x in kept['rep_sets']]}, "
                  f"left out {kept['left_out']}")
            check("rep_seconds keeps only reps of that length",
                  len(kept["rep_sets"]) == 1 and kept["rep_sets"][0]["rep_count"] == 4)
            check("other-length reps counted as left out", kept["left_out"].get("other_length") == 4)
            none = payload(await s.call_tool("compare_sessions", {"name_contains": "no such run"}))
            check("no match explains itself", none["sessions_found"] == 0 and none["note"])

            print("\n6. easy_pace_trend")
            ep = payload(await s.call_tool("easy_pace_trend", {"weeks": 12}))
            print(f"     filters {ep['filters']}, left out {ep['runs_left_out']}")
            print(f"     {ep['earlier_vs_recent']}")
            for wk in ep["weekly"][-3:]:
                print(f"     {wk}")
            check("max_hr defaults to top of zone 1", ep["filters"]["max_hr"] == 140)
            check("sessions under the HR ceiling kept out by time above zone 1",
                  ep["runs_left_out"]["too_much_time_above_easy_zone"] > 20, ep["runs_left_out"])
            check("pace and HR are separate columns, no composite index",
                  all({"mean_gap_min_km", "mean_pace_min_km", "mean_hr"} <= set(wk) for wk in ep["weekly"]) and
                  not any("index" in k or "efficiency" in k for wk in ep["weekly"] for k in wk))
            check("elevation per km reported weekly", all(wk["elevation_m_per_km"] is not None for wk in ep["weekly"]))
            sessions_only = payload(await s.call_tool("easy_pace_trend", {"weeks": 12, "name_contains": "Perth - "}))
            check("no rep session counts as an easy run, even with average HR under the ceiling",
                  sessions_only["weekly"] == [], sessions_only["runs_left_out"])
            surges = payload(await s.call_tool("easy_pace_trend", {"weeks": 16, "max_pct_above_easy_zone": 30}))
            check("raising max_pct_above_easy_zone lets the surge runs back in",
                  surges["runs_left_out"]["too_much_time_above_easy_zone"]
                  < payload(await s.call_tool("easy_pace_trend", {"weeks": 16}))["runs_left_out"]
                  ["too_much_time_above_easy_zone"])
            check("treadmill runs counted", any(wk["treadmill_runs"] for wk in ep["weekly"]))
            half = ep["earlier_vs_recent"]
            check("recent easy GAP quicker than earlier in the improving mock",
                  secs(half["recent"]["mean_gap_min_km"]) < secs(half["earlier"]["mean_gap_min_km"]), half)
            strict = payload(await s.call_tool("easy_pace_trend", {"weeks": 12, "max_hr": 120}))
            check("lower max_hr leaves more runs out",
                  strict["runs_left_out"]["above_max_hr"] > ep["runs_left_out"]["above_max_hr"])
            tm = payload(await s.call_tool("easy_pace_trend", {"weeks": 6, "name_contains": "Treadmill"}))
            check("name_contains restricts to one route",
                  tm["weekly"] and all(wk["runs"] == wk["treadmill_runs"] for wk in tm["weekly"]), tm["weekly"])

            print("\n7. activity_detail on a session reports GAP and elevation per rep")
            det = payload(await s.call_tool("activity_detail", {"activity_id": last["id"]}))
            check("intervals have gap_min_km", any(i["gap_min_km"] for i in det["intervals"]))
            check("intervals have elevation_m_per_km", all("elevation_m_per_km" in i for i in det["intervals"]))
            check("activity has gap_min_km and elevation_m_per_km",
                  det["activity"]["gap_min_km"] and det["activity"]["elevation_m_per_km"] is not None)

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("All run checks passed.")


asyncio.run(main())
