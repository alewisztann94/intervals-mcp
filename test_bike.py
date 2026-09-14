"""Tests for bike support: sport normalisation, units, the pace guard,
power_at_hr_trend, and ride interval handling.

Run against the default mock (no MOCK_DRIFT), where indoor power at the same HR
improves week on week.
"""

import asyncio
import json
import sys
from collections import defaultdict
from datetime import date, timedelta

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

            print("\n1. training_summary pools indoor and outdoor rides under Bike")
            summary = payload(await s.call_tool("training_summary", {"weeks": 6}))
            sports = {sp for wk in summary["weekly"].values() for sp in wk}
            print(f"     sports: {sorted(sports)}")
            check("Bike present", "Bike" in sports)
            check("no raw Ride/VirtualRide rows", not ({"Ride", "VirtualRide"} & sports), sports)
            mixed = next(
                (wk for wk in summary["weekly"].values()
                 if {"Ride", "VirtualRide"} <= set(wk.get("Bike", {}).get("sessions_by_type", {}))),
                None,
            )
            check("a week shows both raw types inside Bike", mixed is not None)
            if mixed:
                b = mixed["Bike"]
                check("sessions_by_type adds up", sum(b["sessions_by_type"].values()) == b["sessions"])

            print("\n2. activity_type filters are normalised")
            bike = payload(await s.call_tool("list_activities", {"days": 30, "activity_type": "Bike", "limit": 200}))
            ride = payload(await s.call_tool("list_activities", {"days": 30, "activity_type": "ride", "limit": 200}))
            types = {a["type"] for a in bike["activities"]}
            print(f"     Bike matched types: {sorted(types)}")
            check("Bike matches Ride and VirtualRide", {"Ride", "VirtualRide"} <= types, types)
            check("'ride' resolves to the same set", ride["count"] == bike["count"])
            runs = payload(await s.call_tool("list_activities", {"days": 30, "activity_type": "Run"}))
            check("Run filter has no bikes", all(a["sport"] == "Run" for a in runs["activities"]))

            print("\n3. Units: speed for bikes, pace for runs")
            b0 = bike["activities"][0]
            print(f"     bike: {b0['type']} {b0['km']}km {b0['speed_kmh']}km/h pace={b0['pace_min_km']}")
            check("bikes have speed_kmh", all(a["speed_kmh"] for a in bike["activities"]))
            check("bikes have no pace_min_km", all(a["pace_min_km"] is None for a in bike["activities"]))
            check("runs keep pace_min_km", all(a["pace_min_km"] for a in runs["activities"]))
            check("runs have no speed_kmh", all(a["speed_kmh"] is None for a in runs["activities"]))
            indoor = [a for a in bike["activities"] if a["type"] == "VirtualRide"]
            check("VirtualRide flagged indoor even when trainer is null", all(a["indoor"] for a in indoor))
            check("avg_watts and np_watts are distinct fields",
                  any(a["np_watts"] and a["avg_watts"] and a["np_watts"] != a["avg_watts"] for a in indoor))
            check("power_source measured on trainer rides", all(a["power_source"] == "measured" for a in indoor))
            check("estimated power flagged on outdoor rides",
                  any(a["power_source"] == "estimated" for a in bike["activities"] if a["type"] == "Ride"))

            print("\n4. easy_pace_trend never includes rides")
            ep = payload(await s.call_tool("easy_pace_trend", {"weeks": 4, "name_contains": "Zwift"}))
            check("a ride name matches no easy runs", ep["weekly"] == [], ep["weekly"])

            print("\n5. power_at_hr_trend on measured power")
            p = payload(await s.call_tool("power_at_hr_trend", {"weeks": 16}))
            print(f"     verdict: {p['verdict']}")
            print(f"     filter: {p['power_filter']}")
            check("weekly series built", len(p["weekly"]) >= 4, len(p["weekly"]))
            check("reads the improving trend in the mock", p["verdict"] and p["verdict"]["reading"] == "improving",
                  p["verdict"])
            check("no-power rides excluded and counted", p["power_filter"]["sessions_excluded_no_power"] > 0)
            check("estimated-power rides excluded and counted",
                  p["power_filter"]["sessions_excluded_estimated_power"] > 0)
            check("no estimated sessions in the series", all(wk["estimated_power_sessions"] == 0 for wk in p["weekly"]))
            wk = p["weekly"][-1]
            print(f"     latest week: {wk}")
            check("EF = mean NP / mean HR", abs(wk["efficiency_factor"] - wk["mean_np_watts"] / wk["mean_hr"]) < 0.01,
                  wk)
            check("hours reported", wk["hours"] > 0)
            check("measured-only indoor series raises no environment warning", p["environment_warning"] is None)

            print("\n6. Weekly means are weighted by duration")
            acts = payload(await s.call_tool("list_activities", {"days": 120, "activity_type": "Bike", "limit": 200}))
            by_week = defaultdict(list)
            for a in acts["activities"]:
                if a["power_source"] == "measured" and (a["minutes"] or 0) >= 10:
                    d = date.fromisoformat(a["date"])
                    by_week[(d - timedelta(days=d.weekday())).isoformat()].append(a)
            def means(rows):
                weighted = sum(a["avg_hr"] * a["minutes"] for a in rows) / sum(a["minutes"] for a in rows)
                return weighted, sum(a["avg_hr"] for a in rows) / len(rows)

            # Only a week where the two means differ can tell them apart.
            target = next(
                (w for w in p["weekly"]
                 if by_week[w["week_start"]] and abs(means(by_week[w["week_start"]])[0]
                                                     - means(by_week[w["week_start"]])[1]) > 0.5),
                None,
            )
            check("found a week where weighted and per-session means differ", target is not None)
            if target:
                weighted, unweighted = means(by_week[target["week_start"]])
                print(f"     {target['week_start']}: tool {target['mean_hr']}, weighted {weighted:.1f}, "
                      f"unweighted {unweighted:.1f}")
                check("mean_hr matches the duration-weighted mean", abs(target["mean_hr"] - weighted) < 0.2)
                check("and not the per-session mean", abs(target["mean_hr"] - unweighted) > 0.3)

            print("\n7. Flags: estimated power and environment")
            p2 = payload(await s.call_tool("power_at_hr_trend", {"weeks": 4, "include_estimated_power": True}))
            check("including estimates raises power_source_warning", p2["power_source_warning"] is not None)
            check("and mixes indoor with outdoor, raising environment_warning", p2["environment_warning"] is not None)
            p3 = payload(await s.call_tool("power_at_hr_trend", {"weeks": 16, "environment": "outdoor"}))
            check("outdoor-only has no measured sessions yet", p3["weekly"] == [] and p3["verdict"] is None)
            check("indoor rides counted as excluded by environment",
                  p3["power_filter"]["sessions_excluded_environment"] > 0)
            bad = await s.call_tool("power_at_hr_trend", {"environment": "garage"})
            check("bad environment is an error", bad.isError is True)

            print("\n8. activity_detail on a steady ride")
            vr = next(a for a in bike["activities"] if a["type"] == "VirtualRide")
            det = payload(await s.call_tool("activity_detail", {"activity_id": vr["id"]}))
            act = det["activity"]
            print(f"     {act['type']}: moving {act['moving_seconds']}s elapsed {act['elapsed_seconds']}s, "
                  f"note: {det['note'][:70]}...")
            check("whole-activity RECOVERY block is not presented as an interval", det["intervals"] == [])
            check("note explains no efforts were detected", "no distinct efforts" in (det["note"] or ""))
            check("moving and elapsed both reported", act["moving_seconds"] and act["elapsed_seconds"])
            check("speed_kmh on the detail", act["speed_kmh"] is not None and act["pace_min_km"] is None)

            print("\n9. activity_detail on a structured run still returns reps")
            hard = next(a for a in runs["activities"] if a["name"] == "Perth - 1k")
            det = payload(await s.call_tool("activity_detail", {"activity_id": hard["id"]}))
            check("intervals returned", len(det["intervals"]) > 10, len(det["intervals"]))
            check("rep pace in min/km", det["intervals"][0]["pace_min_km"] is not None)

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("All bike checks passed.")


asyncio.run(main())
