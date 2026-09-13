"""Fake intervals.icu for testing the MCP server end to end.

Serves payloads shaped like the real API: activities with the icu_* fields,
wellness records keyed by date, and per-activity intervals that 422 for
unstructured sessions (which is what the real one does).
"""

import os
import random
from datetime import datetime, timedelta, timezone

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

random.seed(7)
TODAY = datetime.now(timezone.utc).date()


def _activities():
    """~16 weeks of plausible training: runs, a few rides, one swim."""
    out = []
    aid = 1000
    for days_ago in range(16 * 7, -1, -1):
        d = TODAY - timedelta(days=days_ago)
        dow = d.weekday()
        week_idx = (16 * 7 - days_ago) // 7

        # Two disrupted weeks with no quality work — a taper and an off week.
        # These shift the weekly session mix, which is exactly the situation
        # the confound warning is supposed to catch.
        disrupted = week_idx in (5, 11)

        sessions = []
        if dow in (1, 3, 5) and not disrupted:  # sub-threshold days
            sessions.append(("Run", 14000 + random.randint(-1500, 1500), 275, 148))
        elif dow in (1, 3, 5):
            sessions.append(("Run", 9000, 340, 120))
        elif dow == 6:  # long run
            sessions.append(("Run", 26000 + random.randint(-2000, 3000), 320, 140))
        elif dow != 0:
            sessions.append(("Run", 10000 + random.randint(-1500, 2000), 330, 122))
        # bike shows up only in the last 3 weeks, like a new sport
        if days_ago < 21 and dow in (0, 2, 4):
            sessions.append(("Ride", 22000 + random.randint(-4000, 8000), 135, 118))
        if days_ago < 14 and dow == 0:
            sessions.append(("Swim", 1200, 0, 130))

        for sport, dist, pace_s_km, hr in sessions:
            aid += 1
            # Per-week change in pace at a given HR. Negative = getting faster
            # at the same HR (improving). MOCK_DRIFT flips it so the test suite
            # can also exercise the declining / overreaching case.
            drift = 1 + (week_idx * float(os.environ.get("MOCK_DRIFT", "-0.004")))
            secs = int((dist / 1000) * pace_s_km * drift) if pace_s_km else 1500
            out.append(
                {
                    "id": f"i{aid}",
                    "start_date_local": f"{d.isoformat()}T06:15:00",
                    "type": sport,
                    "name": f"{sport} session",
                    "distance": dist,
                    "moving_time": secs,
                    "elapsed_time": secs + 60,
                    "total_elevation_gain": random.randint(10, 180),
                    "average_heartrate": hr + random.randint(-3, 3),
                    "max_heartrate": hr + random.randint(10, 25),
                    "icu_training_load": random.randint(40, 120),
                    "icu_ctl": round(48 + week_idx * 0.6, 1),
                    "icu_atl": round(52 + random.uniform(-6, 6), 1),
                    "icu_weighted_avg_watts": 137 if sport == "Ride" else None,
                    "icu_efficiency_factor": round(1.7 + week_idx * 0.004, 3),
                    "feel": random.choice([None, 3, 4]),
                    # deliberately include a junk extra field, like the real API does
                    "start_latlng": [0.0, 0.0],
                }
            )
    return out


ACTIVITIES = _activities()
BY_ID = {a["id"]: a for a in ACTIVITIES}


HITS = {"activities": 0, "wellness": 0, "activity": 0, "intervals": 0}


async def stats(request):
    """Request counters, so tests can prove the cache actually prevents calls."""
    if request.query_params.get("reset"):
        for k in HITS:
            HITS[k] = 0
    return JSONResponse(HITS)


async def activities(request):
    HITS["activities"] += 1
    oldest = request.query_params.get("oldest", "1900-01-01")
    newest = request.query_params.get("newest", "2999-01-01")
    sel = [a for a in ACTIVITIES if oldest <= a["start_date_local"][:10] <= newest]
    return JSONResponse(sel)


async def wellness(request):
    HITS["wellness"] += 1
    oldest = request.query_params.get("oldest", "1900-01-01")
    newest = request.query_params.get("newest", "2999-01-01")
    out = []
    for days_ago in range(120, -1, -1):
        d = (TODAY - timedelta(days=days_ago)).isoformat()
        if not (oldest <= d <= newest):
            continue
        out.append(
            {
                "id": d,
                "restingHR": 44 + random.randint(-2, 3),
                "hrv": 82 + random.randint(-8, 8),
                "sleepSecs": (7 * 3600) + random.randint(-3000, 3000),
                "weight": 70.5,
                "fatigue": random.choice([None, 2, 3]),
            }
        )
    return JSONResponse(out)


async def activity(request):
    HITS["activity"] += 1
    aid = request.path_params["aid"]
    act = BY_ID.get(aid)
    if not act:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(act)


async def intervals(request):
    HITS["intervals"] += 1
    aid = request.path_params["aid"]
    act = BY_ID.get(aid)
    if not act:
        return JSONResponse({"error": "not found"}, status_code=404)
    # Mimic the real API: unstructured easy sessions have no interval data.
    if act["average_heartrate"] < 135:
        return JSONResponse({"error": "no intervals"}, status_code=422)
    reps = []
    for i in range(5):
        reps.append(
            {
                "label": f"rep {i + 1}",
                "distance": 1600,
                "moving_time": 380 + random.randint(-8, 8),
                "average_heartrate": 152 + random.randint(-3, 3),
                "average_watts": None,
            }
        )
    return JSONResponse({"icu_intervals": reps})


app = Starlette(
    routes=[
        Route("/api/v1/_stats", stats),
        Route("/api/v1/athlete/{athlete}/activities", activities),
        Route("/api/v1/athlete/{athlete}/wellness", wellness),
        Route("/api/v1/activity/{aid}", activity),
        Route("/api/v1/activity/{aid}/intervals", intervals),
    ]
)
