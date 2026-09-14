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
            cs = payload(await s.call_tool("compare_sessions", {"name_contains": "perth - 1k", "limit": 4}))
            print(f"     filter: {cs['rep_filter']}")
            for row in cs["comparison"]:
                print(f"     {row}")
            check("found sessions", cs["sessions_found"] == 4, cs["sessions_found"])
            check("min_rep_hr defaults to top of zone 1", cs["rep_filter"]["min_rep_hr"] == 140)
            check("one set of reps per session", all(len(x["rep_sets"]) == 1 for x in cs["sessions"]))
            first_sets = [x["rep_sets"][0] for x in cs["sessions"]]
            check("exactly the 10 reps in every session",
                  all(x["rep_count"] == 10 and len(x["reps"]) == 10 for x in first_sets),
                  [x["rep_count"] for x in first_sets])
            check("each 1km rep is a single lap", all(r["laps"] == 1 for x in first_sets for r in x["reps"]))
            last = cs["sessions"][-1]
            print(f"     left out of last session: {last['left_out']}")
            check("recoveries left out and counted", all(x["left_out"]["recovery"] == 18 for x in cs["sessions"]),
                  [x["left_out"] for x in cs["sessions"]])
            check("stride left out as too short", all(x["left_out"]["too_short"] == 1 for x in cs["sessions"]))
            check("warm-up and cool-down km left out as easy running",
                  all(x["left_out"]["easy_running"] == 5 for x in cs["sessions"]), [x["left_out"] for x in cs["sessions"]])
            # Some recordings carry intervals.icu's lap groups, some (manual laps) have none.
            # Either way the lagging first rep must survive, by its group's HR or by its pace.
            grouped = [x for x in first_sets if x["low_hr_reps"] == 0]
            manual = [x for x in first_sets if x["low_hr_reps"] == 1]
            check("both grouped and ungrouped recordings present", bool(grouped and manual),
                  [x["low_hr_reps"] for x in first_sets])
            check("grouped: lagging first rep kept by its group's HR, not flagged",
                  all(x["reps"][0]["avg_hr"] == 139 and not x["reps"][0].get("low_hr") for x in grouped))
            check("ungrouped: lagging first rep kept by pace and marked low_hr",
                  all(x["reps"][0]["avg_hr"] == 139 and x["reps"][0].get("low_hr") is True for x in manual))
            check("mean_hr not dragged down by the lagging rep", all(x["mean_hr"] > 147 for x in first_sets),
                  [x["mean_hr"] for x in first_sets])
            check("every rep has pace, GAP and HR",
                  all(r["pace_min_km"] and r["gap_min_km"] and r["avg_hr"] for x in first_sets for r in x["reps"]))
            check("no rep slower than 5:30/km (no warm-up or recovery leaked in)",
                  all(secs(r["pace_min_km"]) < 330 for x in first_sets for r in x["reps"]))
            check("comparison is oldest first",
                  [x["date"] for x in cs["comparison"]] == sorted(x["date"] for x in cs["comparison"]))

            print("\n5. compare_sessions: reps that span several laps")
            two_k = payload(await s.call_tool("compare_sessions", {"name_contains": "Perth - 2k", "limit": 2}))
            sess = two_k["sessions"][-1]
            sets = sess["rep_sets"]
            print(f"     2k sets: {[(x['rep_count'], x['typical_rep_seconds'], x['typical_rep_km']) for x in sets]}, "
                  f"left out {sess['left_out']}")
            check("auto-lapped 2km reps come back as 4 reps of 2 laps",
                  len(sets) == 1 and sets[0]["rep_count"] == 4
                  and all(r["laps"] == 2 and 1.9 <= r["km"] <= 2.1 for r in sets[0]["reps"]),
                  sets and sets[0]["reps"])
            check("typical rep is the whole 2km", 470 <= sets[0]["typical_rep_seconds"] <= 580,
                  sets[0]["typical_rep_seconds"])
            check("warm-up running straight into rep 1 is not joined to it", sess["left_out"]["easy_running"] == 5,
                  sess["left_out"])
            check("lap-button tap inside a rep does not split it", sess["left_out"]["recovery"] == 7, sess["left_out"])
            dropout = sets[0]["reps"][-1]
            print(f"     dropout rep: {dropout}")
            check("rep with an HR dropout kept and marked low_hr",
                  dropout.get("low_hr") is True and dropout["avg_hr"] < 140, dropout)
            check("counted in low_hr_reps", sets[0]["low_hr_reps"] == 1)
            check("its HR left out of mean_hr", sets[0]["mean_hr"] > 147, sets[0]["mean_hr"])

            timed = payload(await s.call_tool("compare_sessions", {"name_contains": "Timed medium", "limit": 2}))
            tsets = timed["sessions"][-1]["rep_sets"]
            print(f"     timed sets: {[(x['rep_count'], x['typical_rep_seconds'], x['typical_rep_km']) for x in tsets]}")
            check("8-minute reps auto-lapped into 1km + remainder come back whole",
                  len(tsets) == 1 and tsets[0]["rep_count"] == 4 and 420 <= tsets[0]["typical_rep_seconds"] <= 495
                  and all(r["laps"] == 2 for r in tsets[0]["reps"]), tsets)
            only_8 = payload(await s.call_tool("compare_sessions",
                                               {"name_contains": "Timed medium", "rep_seconds": 480, "limit": 2}))
            check("rep_seconds keeps reps of that length",
                  len(only_8["sessions"][-1]["rep_sets"]) == 1 and only_8["sessions"][-1]["rep_sets"][0]["rep_count"] == 4)
            not_1k = payload(await s.call_tool("compare_sessions",
                                               {"name_contains": "Timed medium", "rep_seconds": 270, "limit": 2}))
            kept = not_1k["sessions"][-1]
            print(f"     rep_seconds=270 on 8-minute reps: {kept['rep_sets']}, left out {kept['left_out']}")
            check("the km pieces of a longer rep never pass as 1km reps",
                  kept["rep_sets"] == [] and kept["left_out"].get("other_length") == 4, kept["left_out"])

            ladder = payload(await s.call_tool("compare_sessions", {"name_contains": "Perth - ladder", "limit": 1}))
            lsets = ladder["sessions"][-1]["rep_sets"]
            print(f"     ladder sets: {[(x['rep_count'], x['typical_rep_seconds'], x['typical_rep_km']) for x in lsets]}")
            check("two rep lengths reported as separate sets",
                  {(x["rep_count"], round(x["typical_rep_km"])) for x in lsets} == {(4, 1), (3, 2)}, lsets)
            none = payload(await s.call_tool("compare_sessions", {"name_contains": "no such run"}))
            check("no match explains itself", none["sessions_found"] == 0 and none["note"])
            no_hr = payload(await s.call_tool("compare_sessions",
                                              {"name_contains": "Perth - 2k", "min_rep_hr": 0, "limit": 1}))
            check("min_rep_hr=0 skips the heart-rate test (warm-up counted too)",
                  no_hr["sessions"][-1]["left_out"]["easy_running"] == 0
                  and sum(x["rep_count"] for x in no_hr["sessions"][-1]["rep_sets"]) > 4)

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
            check("earlier_vs_recent pools runs and says how many",
                  all(k in half["recent"] for k in ("runs", "km", "mean_pace_min_km", "mean_hr")), half)
            light = ep.get("light_weeks") or []
            light_rows = [wk for wk in ep["weekly"] if wk["week_start"] in light]
            print(f"     light weeks: {light_rows}")
            check("marathon-recovery week (one 2km jog) stays in the table but is flagged light",
                  len(light_rows) >= 1 and any(wk["runs"] == 1 for wk in light_rows), light)
            check("normal weeks are not flagged", len(light_rows) < len(ep["weekly"]) / 2)
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
