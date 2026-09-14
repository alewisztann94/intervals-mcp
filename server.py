#!/usr/bin/env python3
"""
intervals-icu MCP server.

Exposes your own intervals.icu training data to an MCP client (Claude) as a
small set of tools that return *summaries*, not raw dumps.

Auth model
----------
Two separate things, don't confuse them:

  1. The server authenticates to intervals.icu using YOUR personal API key,
     read from INTERVALS_API_KEY. The key never leaves the server.

  2. Clients authenticate to THIS server by knowing the secret path segment
     in MCP_SECRET_PATH. The MCP endpoint lives at:

         https://<your-host>/<MCP_SECRET_PATH>/mcp

     Anyone with that URL can read your training data, so treat it like a
     password: long, random, never in git, never pasted publicly. HTTP access
     logging is switched off so the path never lands in platform logs.

Env vars
--------
    INTERVALS_API_KEY   required   intervals.icu -> Settings -> Developer
    MCP_SECRET_PATH     required   long random string, e.g. `openssl rand -hex 24`
    PORT                optional   defaults to 8080
    ATHLETE_ID          optional   defaults to "0" = "owner of this key"
"""

from __future__ import annotations

import asyncio
import os
import re
import statistics
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

# Overridable only so the test suite can point at a local mock.
BASE = os.environ.get("INTERVALS_BASE", "https://intervals.icu/api/v1")

API_KEY = os.environ.get("INTERVALS_API_KEY", "")
SECRET_PATH = os.environ.get("MCP_SECRET_PATH", "").strip("/")
ATHLETE = os.environ.get("ATHLETE_ID", "0")
PORT = int(os.environ.get("PORT", "8080"))

# Answering three questions in a row shouldn't mean three full pulls of the same
# activity history. Set to 0 to disable caching entirely.
CACHE_TTL = float(os.environ.get("CACHE_TTL_SECONDS", "300"))
CACHE_MAX_ENTRIES = 64

if not API_KEY:
    raise SystemExit("INTERVALS_API_KEY is not set.")
if not SECRET_PATH:
    raise SystemExit("MCP_SECRET_PATH is not set. Generate one: openssl rand -hex 24")

mcp = FastMCP(
    "intervals-icu",
    instructions=(
        "Training data from intervals.icu for a single athlete. Use training_summary for the overview, "
        "pace_at_hr_trend (run) or power_at_hr_trend (bike) to check whether aerobic fitness is "
        "drifting, and wellness for recovery signals. Sports are normalised: 'Bike' covers indoor and "
        "outdoor rides, 'Run' covers treadmill and trail. All distances are km, durations minutes, "
        "run paces min/km, bike speeds km/h."
    ),
    host="0.0.0.0",
    port=PORT,
    stateless_http=True,
    streamable_http_path=f"/{SECRET_PATH}/mcp",
)


# --------------------------------------------------------------------------- #
# HTTP plumbing
# --------------------------------------------------------------------------- #

# path+params -> (stored_at, payload). Only successful responses are cached;
# errors must stay live so a revoked key or an outage surfaces immediately.
_cache: dict[str, tuple[float, Any]] = {}
_key_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()

CACHE_HITS = 0
CACHE_MISSES = 0


def _cache_key(path: str, params: dict[str, Any] | None) -> str:
    items = sorted((params or {}).items())
    return path + "?" + "&".join(f"{k}={v}" for k, v in items)


async def _lock_for(key: str) -> asyncio.Lock:
    """One lock per key so simultaneous identical calls fetch once, not N times."""
    async with _locks_guard:
        if key not in _key_locks:
            if len(_key_locks) > CACHE_MAX_ENTRIES * 2:
                live = set(_cache)
                for stale in [k for k in _key_locks if k not in live]:
                    del _key_locks[stale]
            _key_locks[key] = asyncio.Lock()
        return _key_locks[key]


def _cache_store(key: str, value: Any) -> None:
    _cache[key] = (time.monotonic(), value)
    if len(_cache) > CACHE_MAX_ENTRIES:
        for old in sorted(_cache, key=lambda k: _cache[k][0])[: len(_cache) - CACHE_MAX_ENTRIES]:
            del _cache[old]


async def _get(path: str, params: dict[str, Any] | None) -> Any:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(
            f"{BASE}{path}",
            auth=("API_KEY", API_KEY),
            params=params or {},
            headers={"Accept": "application/json"},
        )
    if resp.status_code in (401, 403):
        raise RuntimeError(f"intervals.icu rejected the API key ({resp.status_code}).")
    if resp.status_code != 200:
        raise RuntimeError(f"intervals.icu returned {resp.status_code} for {path}: {resp.text[:200]}")
    return resp.json()


async def fetch(path: str, params: dict[str, Any] | None = None) -> Any:
    """GET against intervals.icu with basic auth, cached for CACHE_TTL seconds.

    Raises RuntimeError on any non-200. Failures are never cached.
    """
    global CACHE_HITS, CACHE_MISSES

    if CACHE_TTL <= 0:
        return await _get(path, params)

    key = _cache_key(path, params)
    hit = _cache.get(key)
    if hit and (time.monotonic() - hit[0]) < CACHE_TTL:
        CACHE_HITS += 1
        return hit[1]

    lock = await _lock_for(key)
    async with lock:
        # Another coroutine may have filled it while we waited for the lock.
        hit = _cache.get(key)
        if hit and (time.monotonic() - hit[0]) < CACHE_TTL:
            CACHE_HITS += 1
            return hit[1]

        CACHE_MISSES += 1
        payload = await _get(path, params)
        _cache_store(key, payload)
        return payload


def _window(days: int) -> tuple[str, str]:
    today = datetime.now(timezone.utc).date()
    return (today - timedelta(days=days)).isoformat(), today.isoformat()


def _num(value: Any) -> float | None:
    """Coerce to float, treating None/'' /non-numeric as missing."""
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # drop NaN


def _act_date(activity: dict) -> date | None:
    raw = activity.get("start_date_local") or activity.get("start_date")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _pace(distance_m: Any, seconds: Any) -> str | None:
    """min/km as m:ss."""
    dist, secs = _num(distance_m), _num(seconds)
    if not dist or not secs or dist < 100:
        return None
    s_per_km = secs / (dist / 1000)
    return f"{int(s_per_km // 60)}:{int(s_per_km % 60):02d}"


def _speed_kmh(distance_m: Any, seconds: Any) -> float | None:
    dist, secs = _num(distance_m), _num(seconds)
    if not dist or not secs or dist < 100:
        return None
    return round(dist / secs * 3.6, 1)


# --------------------------------------------------------------------------- #
# Sports
# --------------------------------------------------------------------------- #

# intervals.icu types -> the sport they count as. Anything unlisted (Tennis,
# WeightTraining...) stays as its own sport.
SPORT_GROUPS: dict[str, tuple[str, ...]] = {
    "Bike": (
        "Ride", "VirtualRide", "GravelRide", "MountainBikeRide", "EBikeRide",
        "EMountainBikeRide", "TrackRide", "Cyclocross",
    ),
    "Run": ("Run", "VirtualRun", "TrailRun"),
    "Swim": ("Swim", "OpenWaterSwim"),
}
_CANONICAL = {t.lower(): sport for sport, types in SPORT_GROUPS.items() for t in types}
_CANONICAL.update({sport.lower(): sport for sport in SPORT_GROUPS})


def canonical_sport(raw: Any) -> str:
    """'VirtualRide' -> 'Bike', 'trailrun' -> 'Run', 'Tennis' -> 'Tennis'."""
    raw = str(raw or "Other")
    return _CANONICAL.get(raw.lower(), raw)


def _same_sport(activity: dict, wanted: str) -> bool:
    return canonical_sport(activity.get("type")).lower() == canonical_sport(wanted).lower()


def _is_indoor(activity: dict) -> bool:
    # `trainer` is null on some Garmin-synced Zwift rides, so the type counts too.
    return bool(activity.get("trainer")) or str(activity.get("type", "")).lower().startswith("virtual")


def _power_source(activity: dict) -> str | None:
    """'measured' (power meter / smart trainer), 'estimated' (modelled), or None (no power)."""
    if not _num(activity.get("icu_average_watts")):
        return None
    return "measured" if activity.get("device_watts") is True else "estimated"


def _activity_row(act: dict) -> dict:
    """Headline numbers for one activity, in the units that suit its sport."""
    sport = canonical_sport(act.get("type"))
    bike = sport == "Bike"
    d = _act_date(act)
    return {
        "id": act.get("id"),
        "date": d.isoformat() if d else None,
        "type": act.get("type"),
        "sport": sport,
        "name": act.get("name"),
        "indoor": _is_indoor(act),
        "km": round((_num(act.get("distance")) or 0) / 1000, 2) or None,
        "minutes": round((_num(act.get("moving_time")) or 0) / 60, 1) or None,
        # Pace is meaningless on a bike; speed is the unit riders read.
        "pace_min_km": None if bike else _pace(act.get("distance"), act.get("moving_time")),
        "speed_kmh": _speed_kmh(act.get("distance"), act.get("moving_time")) if bike else None,
        "avg_hr": _num(act.get("average_heartrate")),
        "max_hr": _num(act.get("max_heartrate")),
        "avg_watts": _num(act.get("icu_average_watts")),
        "np_watts": _num(act.get("icu_weighted_avg_watts")),
        "power_source": _power_source(act),
        "elevation_m": _num(act.get("total_elevation_gain")),
        "load": _num(act.get("icu_training_load")),
    }


# --------------------------------------------------------------------------- #
# Trend helpers shared by pace_at_hr_trend and power_at_hr_trend
# --------------------------------------------------------------------------- #

def _wmean(pairs: list[tuple[float, float]]) -> float:
    """Mean of values weighted by duration, so a 3h ride outweighs a 20min spin."""
    total = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / total


def _drop_partial_weeks(per_week: dict[date, list]) -> list[str]:
    """Drop edge weeks clipped by the query window. Mutates per_week."""
    # The first and last weeks of the window are usually clipped by the window
    # edge rather than by how the athlete trained, so they carry an unrepresentative
    # session mix. Drop weeks holding less than half the typical session count.
    dropped: list[str] = []
    if len(per_week) >= 4:
        counts = sorted(len(v) for v in per_week.values())
        typical = counts[len(counts) // 2]
        for wk in [min(per_week), max(per_week)]:
            if len(per_week[wk]) * 2 < typical:
                dropped.append(wk.isoformat())
                del per_week[wk]
    return dropped


def _verdict(series: list[dict], key: str) -> dict | None:
    if len(series) < 4:
        return None
    half = len(series) // 2
    early = statistics.mean(s[key] for s in series[:half])
    late = statistics.mean(s[key] for s in series[half:])
    pct = (late - early) / early * 100 if early else 0
    return {
        "earlier_mean_efficiency": round(early, 3),
        "recent_mean_efficiency": round(late, 3),
        "change_pct": round(pct, 1),
        # +-2% is deliberately a wide "flat" band: week-to-week efficiency
        # swings on terrain, heat and session mix, so anything smaller is
        # noise rather than a fitness signal.
        "reading": ("improving" if pct > 2 else "declining" if pct < -2 else "flat"),
    }


def _hr_confound(series: list[dict], min_hr: int | None, max_hr: int | None) -> str | None:
    # If weekly mean HR is swinging, the weeks aren't comparable and the verdict
    # is reading session mix, not fitness. Say so rather than let it mislead.
    if len(series) < 2 or min_hr is not None or max_hr is not None:
        return None
    hrs = [s["mean_hr"] for s in series]
    spread = max(hrs) - min(hrs)
    if spread <= 5:
        return None
    return (
        f"Weekly mean HR ranges {min(hrs):.0f}-{max(hrs):.0f} bpm ({spread:.0f} bpm spread), "
        "so these weeks contain different mixes of easy and hard sessions and are not "
        "directly comparable. Re-run with min_hr/max_hr to isolate one session type "
        "before trusting the verdict."
    )


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

@mcp.tool()
async def training_summary(weeks: int = 6) -> dict:
    """Weekly training volume by sport plus current fitness/fatigue, for the last N weeks.

    This is the default overview — call it first when asked how training is going.

    Args:
        weeks: How many weeks back to summarise (1-52).
    """
    weeks = max(1, min(weeks, 52))
    oldest, newest = _window(weeks * 7)
    activities = await fetch(f"/athlete/{ATHLETE}/activities", {"oldest": oldest, "newest": newest})
    if not isinstance(activities, list):
        return {"error": "unexpected activities payload"}

    buckets: dict[tuple[date, str], dict] = defaultdict(
        lambda: {"sessions": 0, "km": 0.0, "minutes": 0.0, "load": 0.0, "types": defaultdict(int)}
    )
    latest_fitness: dict[str, Any] = {}
    latest_seen: date | None = None

    for act in activities:
        d = _act_date(act)
        if not d:
            continue
        sport = canonical_sport(act.get("type"))
        b = buckets[(_week_start(d), sport)]
        b["sessions"] += 1
        b["types"][str(act.get("type") or "Other")] += 1
        b["km"] += (_num(act.get("distance")) or 0) / 1000
        b["minutes"] += (_num(act.get("moving_time")) or 0) / 60
        b["load"] += _num(act.get("icu_training_load")) or 0

        if latest_seen is None or d >= latest_seen:
            latest_seen = d
            ctl, atl = _num(act.get("icu_ctl")), _num(act.get("icu_atl"))
            if ctl is not None:
                latest_fitness = {
                    "as_of": d.isoformat(),
                    "fitness_ctl": round(ctl, 1),
                    "fatigue_atl": round(atl, 1) if atl is not None else None,
                    "form_tsb": round(ctl - atl, 1) if atl is not None else None,
                }

    weekly: dict[str, dict] = defaultdict(dict)
    for (wk, sport), b in sorted(buckets.items()):
        weekly[wk.isoformat()][sport] = {
            "sessions": b["sessions"],
            "km": round(b["km"], 1),
            "hours": round(b["minutes"] / 60, 1),
            "load": round(b["load"]),
            "sessions_by_type": dict(b["types"]),
        }

    return {
        "window": {"from": oldest, "to": newest, "weeks": weeks},
        "total_activities": len(activities),
        "weekly": dict(weekly),
        "current_fitness": latest_fitness or None,
        "note": (
            "km and hours are per sport per ISO week (weeks start Monday). Sports are "
            "normalised: Bike = Ride + VirtualRide + Gravel/MTB/etc, Run = Run + VirtualRun + "
            "TrailRun, Swim = Swim + OpenWaterSwim. sessions_by_type shows the raw split."
        ),
    }


@mcp.tool()
async def list_activities(
    days: int = 14,
    activity_type: str | None = None,
    limit: int = 40,
) -> dict:
    """List recent activities with the headline numbers for each.

    Runs report pace_min_km; bikes report speed_kmh instead. avg_watts is true
    average power and np_watts is normalised power. power_source says whether the
    watts were measured (power meter / smart trainer) or estimated.

    Args:
        days: How many days back to look (1-365).
        activity_type: Optional filter, e.g. "Run", "Bike", "Swim". Case-insensitive and
            normalised, so "Bike" (or "Ride") matches indoor and outdoor rides together.
        limit: Max activities to return, most recent first (1-200).
    """
    days = max(1, min(days, 365))
    limit = max(1, min(limit, 200))
    oldest, newest = _window(days)
    activities = await fetch(f"/athlete/{ATHLETE}/activities", {"oldest": oldest, "newest": newest})
    if not isinstance(activities, list):
        return {"error": "unexpected activities payload"}

    if activity_type:
        activities = [a for a in activities if _same_sport(a, activity_type)]

    activities.sort(key=lambda a: str(a.get("start_date_local") or ""), reverse=True)

    rows = [{**_activity_row(act), "feel": act.get("feel")} for act in activities[:limit]]
    return {"window": {"from": oldest, "to": newest}, "count": len(rows), "activities": rows}


@mcp.tool()
async def activity_detail(activity_id: str) -> dict:
    """Full detail for one activity, including per-interval splits where they exist.

    Use this to inspect a specific session — e.g. rep-by-rep pace and HR on a
    sub-threshold workout. Get the id from list_activities.

    Timing: the activity's minutes are MOVING time (stops excluded). Interval
    seconds come from intervals.icu's analysis of the recording, which runs on the
    ELAPSED timeline, so on a ride with stops the intervals sum to more than the
    activity's minutes and interval avg_watts include the stopped (zero-watt) time.

    Interval types and zones (WORK, RECOVERY...) are computed by intervals.icu
    against the FTP and HR zones set in its sport settings, so they are only as
    good as those settings.

    Args:
        activity_id: The intervals.icu activity id.
    """
    # The id is interpolated into the upstream URL, so refuse anything that could
    # walk to a different endpoint (e.g. "../athlete/0/...").
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", activity_id):
        raise ValueError("activity_id must be a plain intervals.icu id, e.g. i12345.")
    act = await fetch(f"/activity/{activity_id}")

    summary = {
        **_activity_row(act),
        "id": activity_id,
        "moving_seconds": _num(act.get("moving_time")),
        "elapsed_seconds": _num(act.get("elapsed_time")),
        # intervals.icu's EF: normalised power / average HR.
        "efficiency_factor": _num(act.get("icu_efficiency_factor")),
        "ftp_setting_watts": _num(act.get("icu_ftp")),
    }
    bike = summary["sport"] == "Bike"

    # Interval splits are not part of the documented cookbook and 422 on
    # unstructured activities, so failure here is normal, not fatal.
    intervals: list[dict] = []
    note = None
    try:
        payload = await fetch(f"/activity/{activity_id}/intervals")
        items = (
            payload
            if isinstance(payload, list)
            else next(
                (payload[k] for k in ("icu_intervals", "intervals") if isinstance(payload.get(k), list)),
                [],
            )
            if isinstance(payload, dict)
            else []
        )
        for it in items:
            if not isinstance(it, dict):
                continue
            secs = _num(it.get("elapsed_time")) or _num(it.get("moving_time"))
            intervals.append(
                {
                    "label": it.get("label") or it.get("type"),
                    "type": it.get("type"),
                    "zone": it.get("zone"),
                    "seconds": secs,
                    "km": round((_num(it.get("distance")) or 0) / 1000, 3) or None,
                    "pace_min_km": None if bike else _pace(it.get("distance"), secs),
                    "speed_kmh": _speed_kmh(it.get("distance"), secs) if bike else None,
                    "avg_hr": _num(it.get("average_heartrate")),
                    "avg_watts": _num(it.get("average_watts")),
                    "np_watts": _num(it.get("weighted_average_watts")),
                }
            )
    except RuntimeError as exc:
        note = f"no interval data ({exc}). Normal for unstructured easy sessions."

    # When intervals.icu finds no distinct efforts it returns the whole activity as
    # one interval, typed by its intensity (often RECOVERY on an easy ride). That
    # reads like a mis-detected workout, so report it as what it is.
    elapsed = summary["elapsed_seconds"]
    if len(intervals) == 1 and elapsed and (intervals[0]["seconds"] or 0) >= 0.9 * elapsed:
        whole = intervals.pop()
        note = (
            f"intervals.icu detected no distinct efforts: its only interval is the whole "
            f"activity, typed {whole['type']} (zone {whole['zone']}) from the FTP/HR zones in "
            "its settings. Normal for steady or unstructured sessions."
        )

    return {"activity": summary, "intervals": intervals, "note": note}


@mcp.tool()
async def wellness(days: int = 30) -> dict:
    """Daily wellness records — resting HR, HRV, sleep, weight — for the last N days.

    Useful for spotting whether added training load is being absorbed: a resting
    HR drifting up or HRV drifting down over a couple of weeks is the early signal.

    Args:
        days: How many days back (1-365).
    """
    days = max(1, min(days, 365))
    oldest, newest = _window(days)
    records = await fetch(f"/athlete/{ATHLETE}/wellness", {"oldest": oldest, "newest": newest})
    if not isinstance(records, list):
        return {"error": "unexpected wellness payload"}

    rows = []
    for rec in records:
        rows.append(
            {
                "date": rec.get("id"),
                "resting_hr": _num(rec.get("restingHR")),
                "hrv": _num(rec.get("hrv")),
                "sleep_hours": round(_num(rec.get("sleepSecs")) / 3600, 1)
                if _num(rec.get("sleepSecs"))
                else None,
                "weight_kg": _num(rec.get("weight")),
                "fatigue": rec.get("fatigue"),
                "soreness": rec.get("soreness"),
            }
        )
    rows.sort(key=lambda r: str(r["date"] or ""))

    def trend(field: str) -> dict | None:
        vals = [r[field] for r in rows if r[field] is not None]
        if len(vals) < 6:
            return None
        half = len(vals) // 2
        first, second = vals[:half], vals[half:]
        return {
            "earlier_mean": round(statistics.mean(first), 1),
            "recent_mean": round(statistics.mean(second), 1),
            "change": round(statistics.mean(second) - statistics.mean(first), 1),
        }

    return {
        "window": {"from": oldest, "to": newest},
        "days": len(rows),
        "records": rows,
        "trends": {"resting_hr": trend("resting_hr"), "hrv": trend("hrv")},
    }


@mcp.tool()
async def pace_at_hr_trend(
    weeks: int = 12,
    activity_type: str = "Run",
    min_hr: int | None = None,
    max_hr: int | None = None,
) -> dict:
    """Track whether pace at a given heart rate is improving or drifting.

    This is the honest fitness signal: for aerobic work, getting faster at the
    same HR means the engine is improving; getting slower at the same HR over
    several weeks means fatigue is accumulating faster than it's being absorbed.

    IMPORTANT: compare like with like. Mixing easy runs and hard sessions makes
    this metric move with weekly session *composition* rather than fitness, so
    pass min_hr/max_hr to confine the analysis to one kind of session — e.g.
    min_hr=140 for sub-threshold work, or max_hr=130 for easy running. Without
    a band the result carries a confound warning when weekly mean HR is unstable.

    Not for bikes: bike speed is dominated by gradient, wind, drafting and
    position, so use power_at_hr_trend for rides.

    Weekly means are weighted by session duration, and weekly pace is total time
    over total distance, so a long run counts for more than a short jog.

    Args:
        weeks: How many weeks back (2-52).
        activity_type: Sport to analyse, e.g. "Run" (covers Run, VirtualRun, TrailRun).
        min_hr: Only include sessions with average HR at or above this.
        max_hr: Only include sessions with average HR at or below this.
    """
    sport = canonical_sport(activity_type)
    if sport == "Bike":
        raise ValueError(
            f"pace_at_hr_trend does not analyse bike activities ('{activity_type}'). Bike speed is "
            "dominated by gradient, wind, drafting and position, so speed per heartbeat is not a "
            "fitness signal on a bike. Use power_at_hr_trend instead."
        )

    weeks = max(2, min(weeks, 52))
    oldest, newest = _window(weeks * 7)
    activities = await fetch(f"/athlete/{ATHLETE}/activities", {"oldest": oldest, "newest": newest})
    if not isinstance(activities, list):
        return {"error": "unexpected activities payload"}

    per_week: dict[date, list[dict]] = defaultdict(list)
    excluded_by_band = 0

    for act in activities:
        if not _same_sport(act, sport):
            continue
        d = _act_date(act)
        hr = _num(act.get("average_heartrate"))
        dist = _num(act.get("distance"))
        secs = _num(act.get("moving_time"))
        if not d or not hr or not dist or not secs or dist < 2000:
            continue
        if (min_hr is not None and hr < min_hr) or (max_hr is not None and hr > max_hr):
            excluded_by_band += 1
            continue
        speed_m_s = dist / secs
        per_week[_week_start(d)].append(
            {
                "ef": (speed_m_s * 60) / hr,  # metres per beat-ish; comparable within sport
                "hr": hr,
                "secs": secs,
                "km": dist / 1000,
            }
        )

    dropped_partial = _drop_partial_weeks(per_week)

    series = []
    for wk in sorted(per_week):
        rows = per_week[wk]
        km = sum(r["km"] for r in rows)
        mean_s_km = sum(r["secs"] for r in rows) / km
        series.append(
            {
                "week_start": wk.isoformat(),
                "sessions": len(rows),
                "km": round(km, 1),
                "mean_hr": round(_wmean([(r["hr"], r["secs"]) for r in rows]), 1),
                "mean_pace_min_km": f"{int(mean_s_km // 60)}:{int(mean_s_km % 60):02d}",
                "efficiency_index": round(_wmean([(r["ef"], r["secs"]) for r in rows]), 3),
            }
        )

    return {
        "sport": sport,
        "window": {"from": oldest, "to": newest, "weeks": weeks},
        "hr_band": {"min_hr": min_hr, "max_hr": max_hr, "sessions_excluded": excluded_by_band},
        "partial_weeks_dropped": dropped_partial or None,
        "weekly": series,
        "verdict": _verdict(series, "efficiency_index"),
        "confound_warning": _hr_confound(series, min_hr, max_hr),
        "caveat": (
            "efficiency_index is speed per heartbeat and is only comparable within the "
            "same sport, session type and broadly similar terrain and conditions. Heat, "
            "hills and session mix all move it. Read trends over weeks, never single sessions."
        ),
    }


@mcp.tool()
async def power_at_hr_trend(
    weeks: int = 12,
    activity_type: str = "Bike",
    min_hr: int | None = None,
    max_hr: int | None = None,
    environment: str = "all",
    include_estimated_power: bool = False,
) -> dict:
    """Track whether power at a given heart rate is improving or drifting (the bike fitness signal).

    More watts at the same HR over several weeks means aerobic fitness is
    improving; fewer means fatigue is accumulating. Per week it reports
    efficiency_factor = duration-weighted normalised power / duration-weighted
    average HR — the same EF intervals.icu shows per activity.

    Power source matters. Only MEASURED power (smart trainer or power meter) is
    used by default; sessions with estimated or no power are excluded and counted.
    Indoor and outdoor rides are pooled but flagged per week, and a warning is
    raised when both appear — trainer and outdoor power meter readings can differ
    by a few percent, so filter with environment= before trusting a small change.

    Compare like with like: pass min_hr/max_hr to isolate one session type (e.g.
    max_hr=130 for endurance rides). Without a band the result carries a confound
    warning when weekly mean HR is unstable.

    Args:
        weeks: How many weeks back (2-52).
        activity_type: Sport to analyse, default "Bike" (covers Ride, VirtualRide, Gravel, MTB...).
        min_hr: Only include sessions with average HR at or above this.
        max_hr: Only include sessions with average HR at or below this.
        environment: "all", "indoor" (trainer/virtual rides) or "outdoor".
        include_estimated_power: Also use sessions whose watts are estimated rather than
            measured. Off by default: mixing a model with a measurement makes the trend meaningless.
    """
    environment = environment.lower()
    if environment not in ("all", "indoor", "outdoor"):
        raise ValueError("environment must be 'all', 'indoor' or 'outdoor'.")

    sport = canonical_sport(activity_type)
    weeks = max(2, min(weeks, 52))
    oldest, newest = _window(weeks * 7)
    activities = await fetch(f"/athlete/{ATHLETE}/activities", {"oldest": oldest, "newest": newest})
    if not isinstance(activities, list):
        return {"error": "unexpected activities payload"}

    per_week: dict[date, list[dict]] = defaultdict(list)
    excluded = {"no_power": 0, "estimated_power": 0, "environment": 0, "hr_band": 0}

    for act in activities:
        if not _same_sport(act, sport):
            continue
        d = _act_date(act)
        hr = _num(act.get("average_heartrate"))
        secs = _num(act.get("moving_time"))
        # Under 10 minutes is a warm-up fragment or a spin to the shops, not a session.
        if not d or not hr or not secs or secs < 600:
            continue
        source = _power_source(act)
        if source is None:
            excluded["no_power"] += 1
            continue
        if source == "estimated" and not include_estimated_power:
            excluded["estimated_power"] += 1
            continue
        indoor = _is_indoor(act)
        if (environment == "indoor" and not indoor) or (environment == "outdoor" and indoor):
            excluded["environment"] += 1
            continue
        if (min_hr is not None and hr < min_hr) or (max_hr is not None and hr > max_hr):
            excluded["hr_band"] += 1
            continue
        avg_w = _num(act.get("icu_average_watts"))
        per_week[_week_start(d)].append(
            {
                "hr": hr,
                "secs": secs,
                "km": (_num(act.get("distance")) or 0) / 1000,
                "avg_w": avg_w,
                "np": _num(act.get("icu_weighted_avg_watts")) or avg_w,
                "indoor": indoor,
                "estimated": source == "estimated",
            }
        )

    dropped_partial = _drop_partial_weeks(per_week)

    series = []
    for wk in sorted(per_week):
        rows = per_week[wk]
        mean_hr = _wmean([(r["hr"], r["secs"]) for r in rows])
        mean_np = _wmean([(r["np"], r["secs"]) for r in rows])
        series.append(
            {
                "week_start": wk.isoformat(),
                "sessions": len(rows),
                "indoor_sessions": sum(r["indoor"] for r in rows),
                "outdoor_sessions": sum(not r["indoor"] for r in rows),
                "estimated_power_sessions": sum(r["estimated"] for r in rows),
                "km": round(sum(r["km"] for r in rows), 1),
                "hours": round(sum(r["secs"] for r in rows) / 3600, 1),
                "mean_hr": round(mean_hr, 1),
                "mean_watts": round(_wmean([(r["avg_w"], r["secs"]) for r in rows])),
                "mean_np_watts": round(mean_np),
                "efficiency_factor": round(mean_np / mean_hr, 3),
            }
        )

    environment_warning = None
    if any(s["indoor_sessions"] for s in series) and any(s["outdoor_sessions"] for s in series):
        environment_warning = (
            "Indoor and outdoor rides are pooled. Trainer and outdoor power meter readings can "
            "differ by a few percent, and a shift in the indoor/outdoor mix moves EF on its own. "
            "Re-run with environment='indoor' or 'outdoor' before trusting a small change."
        )
    power_source_warning = None
    if any(s["estimated_power_sessions"] for s in series):
        power_source_warning = (
            "Some sessions use ESTIMATED power (no power meter). Estimates model speed and "
            "gradient rather than measure output, so EF from them is not a fitness signal. "
            "Re-run without include_estimated_power."
        )

    return {
        "sport": sport,
        "window": {"from": oldest, "to": newest, "weeks": weeks},
        "hr_band": {"min_hr": min_hr, "max_hr": max_hr, "sessions_excluded": excluded["hr_band"]},
        "power_filter": {
            "environment": environment,
            "include_estimated_power": include_estimated_power,
            "sessions_excluded_no_power": excluded["no_power"],
            "sessions_excluded_estimated_power": excluded["estimated_power"],
            "sessions_excluded_environment": excluded["environment"],
        },
        "partial_weeks_dropped": dropped_partial or None,
        "weekly": series,
        "verdict": _verdict(series, "efficiency_factor"),
        "confound_warning": _hr_confound(series, min_hr, max_hr),
        "environment_warning": environment_warning,
        "power_source_warning": power_source_warning,
        "caveat": (
            "efficiency_factor is normalised power per heartbeat and is only comparable within "
            "one session type and power source. Heat, fatigue, hydration and ride intensity all "
            "move it. Read trends over weeks, never single sessions."
        ),
    }


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request):  # noqa: ANN001
    """Public liveness probe. Reveals nothing about usage."""
    from starlette.responses import JSONResponse

    return JSONResponse({"ok": True, "service": "intervals-icu-mcp"})


@mcp.custom_route(f"/{SECRET_PATH}/stats", methods=["GET"])
async def stats(_request):  # noqa: ANN001
    """Cache counters. Behind the secret path because hit counts show when the data is being read."""
    from starlette.responses import JSONResponse

    return JSONResponse(
        {
            "ok": True,
            "cache": {
                "ttl_seconds": CACHE_TTL,
                "entries": len(_cache),
                "hits": CACHE_HITS,
                "misses": CACHE_MISSES,
            },
        }
    )


if __name__ == "__main__":
    import uvicorn

    # Not mcp.run(): that leaves uvicorn's access log on, which would write the
    # secret path into the platform's logs on every request.
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=PORT, access_log=False)
