# intervals-icu MCP server

Puts your intervals.icu training data behind eight tools that Claude can call
directly — no CSV exports, no synced folders, no dependency on your laptop
being awake.

```
Claude  ──HTTPS──>  this server (Northflank)  ──HTTPS──>  intervals.icu API
                    holds your API key
```

## What it exposes

| Tool | What it does |
|---|---|
| `race_history(name_contains)` | Every race and time trial, oldest first: distance, finish time, pace, grade-adjusted pace. The main running progress measure. |
| `compare_sessions(name_contains, rep_seconds, ...)` | The same workout across dates, rep by rep: pace, grade-adjusted pace and HR of each work rep. |
| `easy_pace_trend(weeks, max_hr, name_contains, ...)` | Easy runs only: weekly grade-adjusted pace and HR as separate columns. |
| `training_summary(weeks)` | Weekly volume by sport, plus current fitness (CTL), fatigue (ATL) and form. The default overview. |
| `list_activities(days, activity_type, limit)` | Recent sessions with distance, pace and grade-adjusted pace (runs) or speed (bikes), HR, elevation per km, watts, load. |
| `activity_detail(activity_id)` | One session in full, including rep-by-rep splits where they exist. |
| `wellness(days)` | Resting HR, HRV, sleep, weight — plus whether they're trending. |
| `power_at_hr_trend(weeks, activity_type, min_hr, max_hr, environment, include_estimated_power)` | Bikes: whether power at a given HR (efficiency factor) is improving, flat or declining. Parked until there's enough bike history to read. |

### Sports are normalised

intervals.icu splits one sport across several types. Every tool groups them:

| Sport | intervals.icu types |
|---|---|
| `Bike` | Ride, VirtualRide, GravelRide, MountainBikeRide, EBikeRide, EMountainBikeRide, TrackRide, Cyclocross |
| `Run` | Run, VirtualRun, TrailRun |
| `Swim` | Swim, OpenWaterSwim |

`training_summary` reports one `Bike` row per week, with `sessions_by_type`
showing the raw split. Any `activity_type` filter is normalised the same way, so
`"Bike"`, `"Ride"` and `"VirtualRide"` all match indoor and outdoor rides
together. Unlisted types (Tennis, WeightTraining...) stay as their own sport.

### Running: three direct measures, no composite index

An earlier version combined pace and heart rate into one "efficiency index".
It was removed: route hills moved it more than fitness did. Pace and heart rate
are now always reported as separate columns.

**Grade-adjusted pace everywhere.** intervals.icu computes grade-adjusted pace
(`gap`, using Strava's run model) for every outdoor run and every interval. Every
run tool reports it as `gap_min_km` next to the raw `pace_min_km`; GAP is the one
to compare across routes. Treadmill runs have no GAP (no hills), so they use
plain pace where a GAP figure is needed. Every run row also has
`elevation_m_per_km` — 0 is flat, 10+ is a properly hilly route — so you can see at
a glance whether two runs are comparable.

**`race_history`** lists races and time trials, oldest first, with finish time
(elapsed, as races are timed), pace, GAP and a standard distance label (5k, 10k,
half, marathon, within 3%). intervals.icu has a `race` flag on each activity;
runs with it ticked are included, as are runs named with "race", "marathon",
"parkrun", "time trial" or "TT". Session names like "Perth - 5k" are *not*
treated as races. Tick the race box in intervals.icu to make sure a result is
picked up; `name_contains` adds unusually named results.

**`compare_sessions`** compares one workout across dates, rep by rep. What
intervals.icu returns for a rep session is messier than "reps and recoveries":
warm-up and cool-down kilometres are typed `WORK` too, and lap-button presses
appear as 1-second recoveries. So reps are picked out in three steps:

1. Drop anything typed `RECOVERY`.
2. Drop anything shorter than `min_rep_seconds` (default 90s): strides, lap taps.
3. Drop easy running by heart rate. intervals.icu groups similar laps; a group
   only counts as reps if its average HR is above `min_rep_hr`, by default the top
   of zone 1 from your intervals.icu settings. Judging by the group rather than
   each lap keeps a first rep whose HR is still climbing. A rep with a heart-rate
   dropout can be dropped this way; each session's `left_out` counts show what was
   excluded and why.

Reps of different lengths are never averaged together. `rep_seconds` keeps only
reps of about that length (±`tolerance_pct`, default 15%); without it, each
session's reps are split into sets of similar length and reported separately.
Set averages are weighted by rep duration. `comparison` gives one line per set,
oldest first.

**`easy_pace_trend`** reports weekly GAP, raw pace, HR, km and elevation per km
for easy runs. A run counts as easy when its average HR is at or below `max_hr`
(default: top of zone 1) *and* no more than `max_pct_above_easy_zone` (default 10%)
of it was spent above zone 1. The second test matters for Norwegian-singles
training: warm-up, cool-down and recoveries pull a sub-threshold session's
*average* HR under the easy ceiling even though a third or more of it was hard
running. On real data every such session had 31%+ of its time above zone 1, and
46 of 53 easy runs had none. Weekly figures are duration-weighted; clipped edge
weeks are dropped; `earlier_vs_recent` sets the first half of the window against
the second, pace and HR side by side. `name_contains` restricts it to one route.

### Bikes (parked)

`power_at_hr_trend` is the bike trend tool. Per week it reports
`efficiency_factor` = duration-weighted normalised power ÷ duration-weighted
average HR — the same EF intervals.icu shows per activity — with the same
verdict block, ±2% band, partial-week dropping and HR confound warning.

- **Power source.** Only *measured* power (`device_watts` true: a smart trainer
  or power meter) is used by default. Rides with estimated power, or none, are
  excluded and counted. `include_estimated_power=True` lets estimates in, with a
  warning — an estimate models speed and gradient, so an EF trend built from it
  is circular.
- **Indoor vs outdoor.** Rides are flagged indoor when `trainer` is set *or* the
  type is `Virtual*` (Garmin-synced Zwift rides arrive with `trainer` null). Each
  week reports indoor and outdoor session counts, and an `environment_warning`
  appears when both are present, because trainer and outdoor power meter
  readings differ by a few percent. `environment="indoor"`/`"outdoor"` filters.
  This is a flag rather than a hard split so measured outdoor rides join
  automatically once a power meter is fitted.
- Sessions under 10 minutes are ignored.

### Watts and timing in activity data

- `avg_watts` is true average power (`icu_average_watts`); `np_watts` is
  normalised power (`icu_weighted_avg_watts`). `power_source` is `measured`,
  `estimated` or null.
- An activity's `minutes` is **moving** time. `activity_detail` also returns
  `moving_seconds` and `elapsed_seconds`.
- Interval `seconds` come from intervals.icu's analysis of the recording, which
  runs on the **elapsed** timeline. On a ride with stops the intervals therefore
  sum to more than the activity's minutes, and interval average watts include the
  stopped, zero-watt time.
- When intervals.icu finds no distinct efforts it returns the whole activity as a
  single interval typed by intensity — often `RECOVERY` on an easy ride. That is
  not a failed request; `activity_detail` reports it as "no distinct efforts"
  instead of presenting it as an interval.
- Interval types and zones are computed against the FTP and HR zones in your
  intervals.icu sport settings (`ftp_setting_watts` shows the FTP used), so they
  are only as accurate as those settings.

## Future work

Deliberately not built yet:

- **eFTP and the power-duration curve.** Expose intervals.icu's eFTP and power
  curve. Becomes the primary bike progression metric once a real FTP test
  anchors it.
- **Aerobic decoupling (Pw:HR / Pa:HR drift) on long sessions.** The standard
  aerobic-durability measure. intervals.icu already computes `decoupling` per
  activity; it needs a trend tool restricted to long steady sessions.
- **Variability index** (normalised ÷ average power) for judging how evenly a
  ride was paced.
- **Swim units.** Swims still report `pace_min_km`; pace per 100m would suit them
  better.

## Caching

Upstream responses are cached in memory for `CACHE_TTL_SECONDS` (default 300).
Asking three questions in a row hits the network once instead of three times,
and the data is never more than five minutes stale. Failures are never cached,
so a revoked key or an outage surfaces immediately rather than being papered
over. Concurrent identical calls collapse into a single fetch. Set
`CACHE_TTL_SECONDS=0` to disable.

`GET /<MCP_SECRET_PATH>/stats` reports hits, misses and entry count if you want
to see it working. The public `GET /healthz` returns only `{"ok":true}` — hit
counts would reveal when the data is being read.

## Two different secrets

Don't conflate these:

**`INTERVALS_API_KEY`** — how the server authenticates to intervals.icu. Lives
only in the platform's secret store. Never reaches Claude.

**`MCP_SECRET_PATH`** — how clients authenticate to *this* server. The MCP
endpoint is `https://<your-host>/<MCP_SECRET_PATH>/mcp`, and anyone holding
that URL can read your training data. Treat it as a password: long, random,
never in git, never pasted anywhere public. Requests to any other path get a 404.
HTTP access logging is disabled so the path never appears in platform logs.

No secrets live in this repo — both values come from environment variables at
runtime, so the code is safe to publish.

This is deliberately simple rather than deliberately weak — it's proportionate
for personal training data. If you later want real auth, the MCP SDK supports
OAuth and the server is structured to take it.

## Deploy (Northflank)

Northflank's free Sandbox tier covers this: 2 services, always-on compute with
no sleeping, so there's no cold start when Claude opens a connection. A payment
method is required to create any resources regardless of plan — it's an identity
check, not a charge.

It builds from a Git repo, so push this to GitHub first.

1. Push the repo. `.gitignore` covers `.env` and logs, but check nothing secret
   is staged — a committed key has to be revoked, even if you delete the commit.
2. Northflank → **Create new service** → **Combined service**.
3. Pick the repo and branch. Build type: **Dockerfile**, path `/Dockerfile`.
4. Resources: the smallest plan is fine. The server idles at ~55MB and stays
   under 61MB in use.
5. **Ports & DNS** → add port `8080`, protocol **HTTP**, tick *publicly expose
   to the internet*. Northflank issues the TLS cert.
6. **Environment variables**: set `INTERVALS_API_KEY` and `MCP_SECRET_PATH`.
   Generate the second one with:
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`
7. Health check path: `/healthz`.
8. Deploy, then verify:

```bash
curl https://<your-service>.code.run/healthz
# expect {"ok":true,...}

curl -s -o /dev/null -w "%{http_code}\n" -X POST https://<your-service>.code.run/wrong/mcp
# must be 404 — if it isn't, the auth model is broken
```

Your intervals.icu key is at intervals.icu → Settings → Developer Settings.

Nothing here is Northflank-specific beyond the UI steps — it's a plain container
listening on `$PORT`, so any container host works.

## Connect it to Claude

Settings → Connectors → Add custom connector:

```
https://<your-service>.code.run/<MCP_SECRET_PATH>/mcp
```

Then ask for a training summary and check the tool actually fires.

## Run locally

```bash
pip install -r requirements.txt
export INTERVALS_API_KEY=...
export MCP_SECRET_PATH=local-dev
python server.py
# -> http://127.0.0.1:8080/local-dev/mcp
```

## Tests

`mock_intervals.py` is a fake intervals.icu serving realistically shaped data,
so the suite runs without touching the real API or needing a key. It also counts
requests, which is how the cache tests prove calls are actually being avoided.

```bash
# terminal 1
python -m uvicorn mock_intervals:app --port 9001

# terminal 2
export INTERVALS_API_KEY=testkey MCP_SECRET_PATH=s3cr3t-test-path
export INTERVALS_BASE=http://127.0.0.1:9001/api/v1 PORT=8080
python server.py

# terminal 3
python test_server.py
python test_bike.py
python test_run.py
python test_cache.py     # needs the server restarted with CACHE_TTL_SECONDS=3
```

- `test_server.py` — secret path, tool discovery, every tool's shape, bad input.
- `test_bike.py` — sport normalisation in summaries and filters, speed vs pace
  units, `power_at_hr_trend` (measured-only default, exclusion counts, EF
  definition, duration weighting, environment and estimated-power flags), and
  steady rides reported as "no distinct efforts".
- `test_run.py` — GAP and elevation per km on run rows; `race_history` (race flag,
  names, sessions like "3k" not mistaken for races, `name_contains`);
  `compare_sessions` (exactly the work reps: recoveries, strides, warm-up and
  cool-down left out and counted, lagging first rep kept, rep lengths never mixed,
  `rep_seconds` filter); `easy_pace_trend` (zone-1 defaults, rep sessions kept out
  even when their average HR is under the ceiling, separate pace and HR columns,
  route filter).
- `test_cache.py` — that repeats avoid the network, distinct arguments don't
  collide, concurrent calls don't stampede, the TTL expires, and failures are
  never cached.

## Cost

Free on Northflank's Sandbox tier, which is always-on. Measured footprint: ~55MB
idle, ~61MB under load, flat across 400 calls; responses to Claude are 2-13KB, so
roughly 3-5MB of egress a month at twenty calls a day.
