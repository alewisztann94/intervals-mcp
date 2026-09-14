"""Fake intervals.icu for testing the MCP server end to end.

Serves payloads shaped like the real API: activities with the icu_* fields,
grade-adjusted pace (gap) and HR zone times, wellness records keyed by date,
athlete sport settings, and per-activity intervals.

Interval data mimics what intervals.icu really returns for a rep session:
warm-up and cool-down kilometres typed WORK (not just the reps), 60-second
recoveries and 1-second lap-button taps typed RECOVERY, short strides, and
similar laps sharing a group_id. Easy runs 422 on intervals, as the real API does.
"""

import os
import random
from datetime import datetime, timedelta, timezone

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

random.seed(7)
TODAY = datetime.now(timezone.utc).date()
DRIFT = float(os.environ.get("MOCK_DRIFT", "-0.004"))

# Zone 1 (easy) tops out at 140bpm, as in the athlete settings below.
HR_ZONES = [140, 149, 157, 166, 171, 176, 184]


def _zone_times(segments):
    """Seconds in each HR zone from (seconds, hr) segments."""
    out = [0] * len(HR_ZONES)
    for secs, hr in segments:
        idx = next((i for i, top in enumerate(HR_ZONES) if hr <= top), len(HR_ZONES) - 1)
        out[idx] += secs
    return out


class Session:
    """Builds a rep session interval by interval, then totals it into an activity."""

    def __init__(self, rng):
        self.rng = rng
        self.items = []

    def add(self, kind, secs, dist, hr, group, gain=0.0):
        secs = int(secs)
        speed = dist / secs
        self.items.append(
            {
                "type": kind,
                "label": None,
                "start_time": sum(i["elapsed_time"] for i in self.items),
                "moving_time": secs,
                "elapsed_time": secs,
                "distance": dist,
                "average_speed": speed,
                # Grade adjustment only moves pace where there's climbing.
                "gap": speed * (1 + gain / dist * 3),
                "average_heartrate": hr,
                "total_elevation_gain": gain,
                "group_id": group,
                "average_watts": None,
                "weighted_average_watts": None,
            }
        )

    def easy_km(self, n, pace):
        for _ in range(n):
            self.add("WORK", pace + self.rng.randint(-10, 10), 1000, 118 + self.rng.randint(-3, 3),
                     "410s@118bpm80rpm", gain=float(self.rng.randint(0, 20)))

    def reps(self, n, dist, pace, group):
        for i in range(n):
            # HR lags on the first rep, which is why reps are judged by group HR.
            hr = 139 if i == 0 else 150 + self.rng.randint(-3, 3)
            self.add("WORK", pace * dist / 1000 + self.rng.randint(-3, 3), dist, hr, group,
                     gain=float(self.rng.choice([0, 0, 1.2, 2.4, 6.0])))
            if i < n - 1:
                self.add("RECOVERY", 60, 80, 132, None)
                # A lap-button tap: a 1-second "recovery".
                self.add("RECOVERY", 1, 3, 150, None)

    def activity(self, base):
        secs = sum(i["elapsed_time"] for i in self.items)
        dist = sum(i["distance"] for i in self.items)
        gain = sum(i["total_elevation_gain"] for i in self.items)
        segments = [(i["elapsed_time"], i["average_heartrate"]) for i in self.items]
        return {
            **base,
            "distance": dist,
            "moving_time": secs,
            "elapsed_time": secs + 20,
            "total_elevation_gain": gain,
            "average_heartrate": round(sum(s * h for s, h in segments) / secs),
            "max_heartrate": 158,
            "gap": sum(i["gap"] * i["elapsed_time"] for i in self.items) / secs,
            "icu_hr_zone_times": _zone_times(segments),
            "_intervals": self.items,
        }


def _activities():
    """~16 weeks of Norwegian-singles style running, plus a few races, rides and swims."""
    out = []
    aid = 1000
    for days_ago in range(16 * 7, -1, -1):
        d = TODAY - timedelta(days=days_ago)
        dow = d.weekday()
        week_idx = (16 * 7 - days_ago) // 7
        # Per-week change in pace. Negative = getting faster (improving).
        drift = 1 + week_idx * DRIFT
        # Two disrupted weeks with no quality work — a taper and an off week.
        disrupted = week_idx in (5, 11)
        rng = random.Random(days_ago)

        base = {"start_date_local": f"{d.isoformat()}T06:15:00", "type": "Run", "trainer": None,
                "icu_training_load": random.randint(40, 120), "icu_ctl": round(48 + week_idx * 0.6, 1),
                "icu_atl": round(52 + random.uniform(-6, 6), 1), "feel": random.choice([None, 3, 4]),
                "icu_average_watts": None, "icu_weighted_avg_watts": None, "device_watts": None,
                "race": False, "start_latlng": [0.0, 0.0]}
        runs = []

        if dow == 5 and week_idx == 8:
            runs.append({"name": "Perth parkrun", "distance": 5000, "moving_time": int(1150 * drift),
                         "avg_hr": 170, "gain": 40, "hard": True})
        elif dow == 5 and week_idx == 12:
            runs.append({"name": "Perth - club champs", "distance": 3000, "moving_time": int(640 * drift),
                         "avg_hr": 172, "gain": 12, "hard": True})
        elif dow == 5 and week_idx == 14:
            # Ticked as a race in intervals.icu, but not named like one.
            runs.append({"name": "Perth - Saturday hit out", "distance": 10000, "moving_time": int(2460 * drift),
                         "avg_hr": 168, "gain": 90, "hard": True, "race": True})
        elif dow in (1, 3, 5) and not disrupted:
            s = Session(rng)
            s.easy_km(3, int(410 * drift))
            s.add("WORK", 20, 90, 130, "20s@130bpm90rpm")  # a stride
            if dow == 3:
                # Mixed session: 1km reps and ~3-minute reps, never to be averaged together.
                s.reps(4, 1000, 285 * drift, "285s@150bpm88rpm")
                s.add("RECOVERY", 60, 80, 132, None)
                s.reps(4, 680, 285 * drift, "194s@151bpm89rpm")
                name = "Perth - Timed medium"
            else:
                s.reps(10, 1000, 280 * drift, "280s@151bpm89rpm")
                name = "Perth - 3k"
            s.easy_km(2, int(420 * drift))
            aid += 1
            out.append(s.activity({**base, "id": f"i{aid}", "name": name}))
        elif dow == 6:  # long run
            runs.append({"name": "Perth Running", "distance": 26000 + rng.randint(-2000, 3000),
                         "pace": 330, "avg_hr": 128, "gain": rng.randint(150, 350)})
        elif dow == 4 and days_ago < 42:
            runs.append({"name": "Treadmill Running", "distance": 8000, "pace": 340, "avg_hr": 120,
                         "gain": None, "treadmill": True})
        elif dow == 2 and week_idx % 4 == 0:
            # Named like an easy run, but with surges: a quarter of it above zone 1.
            runs.append({"name": "Perth Running", "distance": 10000, "pace": 320, "avg_hr": 132,
                         "gain": 60, "pct_above": 0.25})
        elif dow != 0:
            runs.append({"name": "Perth Running", "distance": 10000 + rng.randint(-1500, 2000),
                         "pace": 345, "avg_hr": 122 + rng.randint(-3, 3), "gain": rng.randint(10, 180)})

        for r in runs:
            aid += 1
            dist = r["distance"]
            secs = r.get("moving_time") or int(dist / 1000 * r["pace"] * drift)
            gain = r.get("gain")
            speed = dist / secs
            above = int(secs * (0.9 if r.get("hard") else r.get("pct_above", 0)))
            zone_times = [secs - above, above, 0, 0, 0, 0, 0]
            out.append(
                {
                    **base,
                    "id": f"i{aid}",
                    "name": r["name"],
                    "type": "VirtualRun" if r.get("treadmill") else "Run",
                    "trainer": True if r.get("treadmill") else None,
                    "race": r.get("race", False),
                    "distance": dist,
                    "moving_time": secs,
                    "elapsed_time": secs + 30,
                    "total_elevation_gain": gain,
                    "average_heartrate": r["avg_hr"],
                    "max_heartrate": r["avg_hr"] + 15,
                    "gap": None if r.get("treadmill") else speed * (1 + (gain or 0) / dist * 3),
                    "icu_hr_zone_times": zone_times,
                }
            )

        # outdoor rides with no power meter show up in the last 3 weeks; their
        # watts are a Strava/Garmin-style estimate (device_watts false)
        if days_ago < 21 and dow in (0, 2, 4):
            aid += 1
            dist = 22000 + random.randint(-4000, 8000)
            secs = int(dist / 1000 * 135)
            out.append({**base, "id": f"i{aid}", "type": "Ride", "name": "Ride session", "distance": dist,
                        "moving_time": secs, "elapsed_time": secs + 60, "total_elevation_gain": random.randint(10, 180),
                        "average_heartrate": 118, "max_heartrate": 135, "icu_average_watts": 125,
                        "icu_weighted_avg_watts": 137, "device_watts": False})
        if days_ago < 14 and dow == 0:
            aid += 1
            out.append({**base, "id": f"i{aid}", "type": "Swim", "name": "Swim session", "distance": 1200,
                        "moving_time": 1500, "elapsed_time": 1560, "total_elevation_gain": None,
                        "average_heartrate": 130, "max_heartrate": 145})
    return out


def _bike_activities():
    """~16 weeks of indoor trainer rides with measured power, plus outdoor rides with none.

    Uses its own RNG so bike data never shifts the run data above.
    """
    rng = random.Random(11)
    out = []
    aid = 5000
    for days_ago in range(16 * 7, -1, -1):
        d = TODAY - timedelta(days=days_ago)
        dow = d.weekday()
        week_idx = (16 * 7 - days_ago) // 7
        aid += 1

        if dow in (1, 3, 6):  # Zwift: Tue, Thu, and a long Sunday ride
            secs = (7200 if dow == 6 else 3000) + rng.randint(-600, 900)
            # Long ride at lower HR than the midweek ones, so duration-weighted and
            # per-session weekly means genuinely differ.
            hr = (112 if dow == 6 else 122) + rng.randint(-2, 2)
            # Same convention as the runs: negative drift = more watts at the same HR.
            avg_w = round(140 * (1 - week_idx * DRIFT)) + rng.randint(-2, 2)
            speed_ms = 8.0 + rng.uniform(-0.5, 0.5)
            # Sunday rides sync via Garmin, where `trainer` comes through null.
            garmin = dow == 6
            out.append(
                {
                    "id": f"i{aid}",
                    "start_date_local": f"{d.isoformat()}T17:30:00",
                    "type": "VirtualRide",
                    "name": "Other" if garmin else "Zwift - Watopia",
                    "trainer": None if garmin else True,
                    "device_watts": True,
                    "distance": round(speed_ms * secs, 1),
                    "moving_time": secs,
                    # Stops at the lights in Watopia: elapsed runs longer than moving.
                    "elapsed_time": secs + rng.randint(30, 400),
                    "total_elevation_gain": rng.randint(50, 300),
                    "average_heartrate": hr,
                    "max_heartrate": hr + rng.randint(15, 30),
                    "icu_average_watts": avg_w,
                    "icu_weighted_avg_watts": round(avg_w * 1.08),
                    "icu_training_load": rng.randint(30, 90),
                    "icu_efficiency_factor": round(avg_w * 1.08 / hr, 3),
                    "icu_ftp": 250,
                    "feel": None,
                }
            )
        elif dow == 5 and week_idx < 12:  # outdoor ride, no power at all
            secs = 5400
            out.append(
                {
                    "id": f"i{aid}",
                    "start_date_local": f"{d.isoformat()}T08:00:00",
                    "type": "Ride",
                    "name": "Morning Ride",
                    "trainer": None,
                    "device_watts": None,
                    "distance": 45000,
                    "moving_time": secs,
                    "elapsed_time": secs + 600,
                    "total_elevation_gain": 220,
                    "average_heartrate": 125,
                    "max_heartrate": 150,
                    "icu_average_watts": None,
                    "icu_weighted_avg_watts": None,
                    "icu_training_load": 70,
                    "feel": None,
                }
            )
    return out


ACTIVITIES = sorted(_activities() + _bike_activities(), key=lambda a: a["start_date_local"])
BY_ID = {a["id"]: a for a in ACTIVITIES}


def _public(act):
    """The activity as the API returns it, without the mock's private fields."""
    return {k: v for k, v in act.items() if not k.startswith("_")}


HITS = {"activities": 0, "wellness": 0, "activity": 0, "intervals": 0, "athlete": 0}


async def stats(request):
    """Request counters, so tests can prove the cache actually prevents calls."""
    if request.query_params.get("reset"):
        for k in HITS:
            HITS[k] = 0
    return JSONResponse(HITS)


async def athlete(request):
    HITS["athlete"] += 1
    return JSONResponse(
        {
            "id": "i0",
            "sportSettings": [
                {"types": ["Ride", "VirtualRide"], "ftp": 250, "hr_zones": [134, 149, 155, 166, 171, 176, 184]},
                {"types": ["Run", "VirtualRun", "TrailRun"], "ftp": None, "hr_zones": HR_ZONES},
            ],
        }
    )


async def activities(request):
    HITS["activities"] += 1
    oldest = request.query_params.get("oldest", "1900-01-01")
    newest = request.query_params.get("newest", "2999-01-01")
    sel = [_public(a) for a in ACTIVITIES if oldest <= a["start_date_local"][:10] <= newest]
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
    return JSONResponse(_public(act))


async def intervals(request):
    HITS["intervals"] += 1
    aid = request.path_params["aid"]
    act = BY_ID.get(aid)
    if not act:
        return JSONResponse({"error": "not found"}, status_code=404)
    if act.get("_intervals"):
        return JSONResponse({"icu_intervals": act["_intervals"], "icu_groups": []})
    # Mimic the real API on a steady ride: no efforts found, so the whole activity
    # comes back as one interval on the elapsed timeline, typed by intensity.
    if act["type"] == "VirtualRide":
        return JSONResponse(
            {
                "icu_intervals": [
                    {
                        "type": "RECOVERY",
                        "label": None,
                        "zone": 1,
                        "moving_time": act["elapsed_time"],
                        "elapsed_time": act["elapsed_time"],
                        "distance": act["distance"],
                        "average_watts": act["icu_average_watts"] - 1,
                        "weighted_average_watts": act["icu_weighted_avg_watts"],
                        "average_heartrate": act["average_heartrate"],
                    }
                ],
                "icu_groups": [],
            }
        )
    # Mimic the real API: unstructured easy sessions have no interval data.
    return JSONResponse({"error": "no intervals"}, status_code=422)


app = Starlette(
    routes=[
        Route("/api/v1/_stats", stats),
        Route("/api/v1/athlete/{athlete}", athlete),
        Route("/api/v1/athlete/{athlete}/activities", activities),
        Route("/api/v1/athlete/{athlete}/wellness", wellness),
        Route("/api/v1/activity/{aid}", activity),
        Route("/api/v1/activity/{aid}/intervals", intervals),
    ]
)
