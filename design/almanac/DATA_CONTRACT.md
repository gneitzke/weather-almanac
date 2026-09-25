# Almanac overlay — live data contract

The console (data engine) writes this JSON to `wx.json`; the overlay HTML polls it
(~every 2 s) and updates text + gauges. Flat object, display-ready primitives
(numbers/strings), **not** the app's `[value, unit, ...]` lists. Missing/None → `null`;
the HTML shows an em-dash for null. Emitter converts from the app's
`Obs`/`Astro`/`Met`/`Sager`/`System` DictProperties (see the field map in the code-explorer notes / lib/properties.py).

```jsonc
{
  // ——— Freshness. Three independent clocks, and conflating them is how a dead
  // station looked healthy: "ts" only proves the emit tick ran, "obsAgeSec"
  // proves the station is still reporting, and the per-provider ages prove a
  // network fetch still succeeds. All ages are whole seconds, null = never.
  "ts": 1750000000,                 // engine heartbeat: epoch seconds when written
  "obsTs": 1749999940,              // epoch of the newest OUTDOOR observation (obs_st / obs_out_air)
  "obsAgeSec": 60,                  // now - obsTs; from ENGINE START while obsTs is still null
                                    // (a never-heard station ages like a silent one). Past ~5 min the console's masthead reads
                                    // "SILENT" (as against "STALE" for a stalled feed) and
                                    // /health reports status "degraded" — the numbers are
                                    // cached, however fresh "ts" looks. An indoor Air is ignored
                                    // here on purpose: it must not mask a dead Tempest.
  "station": "Seattle",             // [Station] Name
  "date": "Fri, 31 Jul 2026",       // System['date']
  "time": "12:52",                  // System['time']  (HH:MM)
  "dayStartTs": 1749970800,         // station-local midnight today, epoch seconds (the hero curve's day axis)

  // Temperature
  "temp": 64.0, "tempUnit": "°F",   // Obs['outTemp'][0],[1]
  "feelsLike": 64, "feelsDesc": "Warm",   // Obs['FeelsLike'][0],[2] with the core's "Feeling " prefix dropped
  "tempTrendPerHr": 4.6,            // Obs['outTempTrend'][0]  (+ = rising)
  "temp24hDelta": 4.9,             // vs this time yesterday (+ = warmer); null if unknown
  "obsLow": 49.8,  "obsLowTime": "06:15",   // Obs['outTempMin'][0],[2]
  "obsHigh": 64.8, "obsHighTime": "08:04",  // Obs['outTempMax'][0],[2]
  "fcLow": 50, "fcHigh": 82,        // forecast today low/high (Met[...] lowTemp/highTemp)
  "humidity": 67, "dewPoint": 52.9, // Obs['Humidity'][0], Obs['DewPoint'][0]

  // Conditions / short-term forecast
  "conditions": "Clear & Sunny",           // Met['Conditions']
  "conditionsNote": "Clear until 02:00 tomorrow",
  "fcHour": "10:00", "fcWind": "0 mph NW", "fcPrecipPct": 0, "fcDailyPct": 0,
  // 7-day outlook (Open-Meteo daily, hourly refresh; [] hides the band).
  // hi/lo are whole degrees in the console's own temp unit; code is the WMO
  // weather code; pp is max precipitation probability for the day.
  // The TODAY row's hi/lo are overridden with fcLow/fcHigh (WeatherFlow) when
  // known, so the hero and the band never disagree about today.
  // qpf is the day's expected precipitation in rainUnit (Open-Meteo is asked
  // for inch when the station is configured in inches, mm otherwise); null
  // when unknown. The band prints it after the chance and hides it below
  // what the unit can show (0.01 in / 0.1 mm). gust is km/h, a fixed unit.
  "fcDaily": [{"day": "SAT", "date": "2026-08-29", "today": true, "hi": 64, "lo": 54, "code": 95, "pp": 95, "qpf": 0.34, "gust": 38}],
  // Hourly forecast temperature (Open-Meteo, the SAME fetch as fcDaily), from
  // the current station-local hour through the end of tomorrow — 48 points at
  // most. Each point is [epoch SECONDS, temp]; the temp is in tempUnit (the
  // request asks for the console's own unit) to 1 decimal. [] when unknown.
  // This is what the hero's day-curve draws its dotted future segment through,
  // and the ONLY thing it may draw there: with [] the curve stops at now and
  // the forecast low/high stay text, because nothing in the payload says WHEN
  // they happen. The console maps each epoch onto the local day using ts and
  // time, so points from a stale fetch fall outside today and drop out.
  "fcHourly": [[1756400000, 71.2], [1756403600, 73.4]],
  "fcStale": false,   // true when no successful forecast fetch in 24 h; the console hides the band
  "fcAgeSec": 1800,   // seconds since the last SUCCESSFUL forecast fetch (null = never)

  // Wind  (dir in degrees; cardinal string; needle rotates to dir)
  "windSpd": 0.9, "windUnit": "mph", "windAvg": 0.1, "windGust": 2.9, "windMax": 4.3,
  "windDir": 206, "windCardinal": "SSW", "windStatus": "Calm",

  // Barometer  (needle maps slp on 980..1050)
  "slp": 1022.1, "slpUnit": "mb", "slpTrendPerHr": 0.4, "slpTrendDesc": "Rising",
  "slpSeries": [[1756400000, 1018.4], [1756401800, 1018.2]],   // 24h barograph trace, <=48 [epoch, slp] points in slpUnit; [] hides the graph
  "slp24High": 1022.1, "slp24HighTime": "08:20",
  "slp24Low": 1020.2,  "slp24LowTime": "00:00", "slpOutlook": "Unchanged",

  // Rainfall  (rainRateMm drives the tube: mm/hr mapped onto the console core's
  //  own intensity bands 0.25/1/4/16/50 mm/hr, each an equal fifth of the tube.
  //  rainRate is the same rate in display units, for the printed readout.)
  "rainToday": 0.00, "rainYest": 0.00, "rainMonth": 0.15, "rainYear": 40.0,
  //  rainRateMm is max(the sensor's raw minute, the 10-min time-weighted mean):
  //  the haptic sensor reports drizzle as an occasional trace minute with zeros
  //  between, and the window bridges those so light rain never reads as dry.
  //  rainRateInstMm is the raw minute; rainStatus takes the band word for the
  //  windowed rate when the core says "Currently Dry" inside a drizzle.)
  "rainUnit": "in", "rainRate": 0, "rainRateMm": 0, "rainRateInstMm": 0, "rainStatus": "Currently Dry",
  "drySpellDays": 12, "lastRainDate": "Sun 19 Jul", "lastRainAmt": 0.11,

  // Sun & UV  (sunFrac 0..1 = elapsed fraction of daylight → sun position on the arc)
  "uvIndex": 4.0, "uvDesc": "Moderate", "radiation": 468, "radUnit": "W/m²",
  "sunrise": "05:43", "sunset": "20:43", "sunFrac": 0.29,
  "daylight": "11h 37m", "peakSun": 0.65,

  // Air Quality  (US EPA AQI by station lat/lon, Open-Meteo; null hides the block)
  "aqi": 40, "aqiCategory": "Good", "aqiPm25": 13.8,
  // short forecast so a rising smoke event is visible before the number degrades
  "aqiForecast": [[1754247600, 40], [1754251200, 43]], "aqiPeak": 55, "aqiPeakTime": "5 PM",
  "aqiForecastCat": "Moderate", "aqiTrend": "rising", "aqiTrendText": "Moderate by 5 PM",
  "aqiStale": false,          // true if the last successful AQI fetch is > 1 h old
  "aqiAgeSec": 420,           // seconds since that fetch (null = never). A provider can serve a
                              // days-old station reading; this is the age of OUR download.

  // Weather alerts  (active NWS alerts by lat/lon; same-type collapsed, capped 3,
  // sorted by product level: warning > watch > advisory > alert > statement)
  "alerts": [
    { "event": "Air Quality Alert", "eventClass": "air", "level": "advisory",
      "tone": "brass",        // banner colour token: accent | brass | water | verdigris
      "priority": 2,          // level int (4 = warning, highest)
      "short": "Wildfire smoke", "areaShort": "King, Kitsap, Pierce +2",
      "onset": 1754251380, "until": 1754438400, "untilText": "Wed 5 PM",
      "headline": "Air Quality Alert issued August 3 …" }
  ],
  "alertCount": 1,            // distinct types; the HTML shows "+N more" = count-1
  "alertsStale": false, "alertsAsOf": "13:52",   // last successful fetch; stale after 1 h
  "alertsAgeSec": 300,

  // Moon
  "moonPhase": "Waning Gibbous", "moonIllum": 78,
  "moonrise": "22:14", "moonset": "09:38", "nextFull": "Aug 8", "nextNew": "Aug 23",

  // Lightning  (distance only — no bearing; null when quiet)
  // lightningDist is the core's +/-3 km RANGE text ("13-17"); lightningDistNum is
  // its midpoint, which is what the ring geometry and big-number readouts use.
  // lightningTs is the strike EVENT epoch; lightningSinceSec and lightningLast
  // are derived from it at emit time. The core's StrikeDeltaT is frozen at the
  // moment it was calculated, so a payload built an hour later would otherwise
  // still say the strike was seconds ago. Only used as a fallback when no epoch
  // is available.
  "lightningActive": false, "lightningDist": null, "lightningDistNum": null,
  "lightningDistUnit": "miles", "lightningTs": null, "lightningSinceSec": null,
  // lightningRate = strikes/min (the core's StrikeFreq); lightning3hr = the
  // rolling 3-hour count. There is no 3-min/30-min bucket in the data path.
  "lightningRate": 0, "lightning3hr": 0, "lightningToday": 0,
  "lightningLast": "3 days ago",

  // Sager
  "sagerCode": "G·2·3·D", "sagerText": "Fair, little temperature change...",
  "sagerIssued": "09:06",           // when the Sager forecast was generated (Sager['Issued']); sagerCode/Pressure/Wind/Sky are not sourced (null)
  "sagerPressure": "1022.1 rising", "sagerWind": "SSW backing", "sagerSky": "Clear"
}
```

Rules: numbers are numbers (HTML formats). Clock strings follow the console's
`Display/TimeFormat`: `"HH:MM"` on a 24 hr console, `"H:MM AM"` / `"H:MM PM"` on a
12 hr one (labels such as `fcHour`, `untilText` and `aqiPeakTime` drop `:00` on the hour:
`"Wed 5 PM"`). Every clock string in the payload, whether the upstream modules or
the emitter formatted it, uses the same setting. Meridiems are uppercase, preceded
by U+00A0 (no-break space); spaces in the examples are typographic shorthand.
The page parses both colon-bearing clock forms; sparse labels are display text. Angles/fractions
are numeric so the HTML can drive SVG geometry. The HTML treats any `null`/missing key as
an em-dash and leaves that gauge at a neutral position. The emitter must never write a
partial/invalid file (write to a temp path + atomic rename).

`aqiPm25` is PM2.5 concentration in µg/m³ and is populated only by providers that supply a
concentration. It is `null` for WAQI, whose `iaqi.pm25` value is a pollutant AQI rather than
a concentration.

## Freshness and /health

`design/almanac/kiosk/serve.py` turns these fields into a status the watchdog and any
monitor can act on:

| status | meaning | condition |
|---|---|---|
| `ok` | engine and station both live | `ts` fresh and `obsAgeSec` under 5 min |
| `stale` | engine stalled | `ts` older than `WFP_STALE_SEC` (20 s) |
| `degraded` | sensor silent | `ts` fresh, `obsAgeSec` over `WFP_OBS_STALE_SEC` (300 s) |
| `error` | engine down | `wx.json` missing or unparsable |

`obsAgeSec` in `/health` is the payload's value plus the file's own age, so it stays
truthful when the file itself has stopped moving. Anything but `ok` answers HTTP 503.

`/health` also reports two counters. `polls` counts `wx.json` requests from anyone;
`renders` counts frames the kiosk page confirmed it painted — the page adds `r=1` to
its next poll only after `render()` returned, and the server credits that mark only
from a loopback client. The launcher's watchdog reads `renders`, because a request
count never proved anything reached the screen.

## Radar display floor (2026-09-24)

`DISPLAY_FLOOR_DBZ` (15) is the drawing floor for every source. `source_palette()`
returns `_RADAR_DISPLAY_LUT`: the designed 26-stop LUT with the stops below the
floor (10, 12.5 dBZ) at alpha 0, so they and anything below the first stop render
transparent; the site clear-air band (5-10 dBZ) is no longer drawn. `radar.legend`
is `_RADAR_DISPLAY_RAMP`: `floorDbz` 15, first band 15-20 starting at the ramp's
own colour at 15 (`#639C7B`), no `kind`/`alpha` bands. `_RADAR_RAMP`, `_RADAR_LUT`
and the echo count (`weather_pixels`, 25 dBZ) are unchanged.

## Viewing, unattended radar and rain start (2026-09-23)

The page sends `view=radar` or `view=none` on every main loopback `wx.json`
poll, including first load, render acknowledgement, immediate polls, and visibility
changes. `viewSession` and increasing `viewSeq` order these reports independently
of camera ownership. Reloads claim the session returned by `X-View-Session` using
`viewClaim`. Once this protocol is established, auxiliary reads and obsolete
reports cannot change `radar_viewing`. Legacy polls retain their prior semantics
until the first explicit report. An accepted Radar report writes the marker;
an accepted exit removes it. The
engine treats the tab as open while the marker exists and its `last` is under
`RADAR_VIEWING_LAPSE_SEC` (60 s) old. `radar.attention.unattended` is true while the
tab is open and no touch (`presence` marker) has arrived for 30 minutes; the tier
stays `live` with 8 frames but `prefetch` is off.

`rainStatus` may read `"Rain Starting"`: the station's `evt_precip` event arrived
after the latest observation (`precipStartTs` > `obsTs`), the observed status is
dry, and the event was received within `RAIN_START_HOLD_SEC` (300 s). The parser keeps
`precipStartReceivedTs` on the Pi's clock for expiry; station epochs remain the
ordering clock. Duplicate and older events cannot renew receipt time. The next
observation governs, even if dry. A bounded onset counts as wet for attention even
when the preceding observation is stale. Event display updates publish only event
fields, so they cannot expose an observation worker's unfinished values.

## Radar off (2026-09-18)

A kiosk that cannot show radar runs none of it. The launcher passes
`WFP_RADAR="${WFP_RADAR:-${WFP_TABS:-1}}"` to the engine: with the tab bar off the
engine schedules no acquisition, cache scan, geography pre-warm or listings, and
publishes `radar: {available: false, reason: "radar off", enabled: false, starting:
null, attention: null}`. `/health.radar.enabled` says so. `WFP_RADAR=1` forces it on.

## Radar attention tiers (2026-09-17)

The engine spends radar bandwidth where a person is likely to look and weather is
worth looking at. `radar.attention` is published with every payload:
`{tier, reason, since, weather, mode, waking, waiting, frames, tiles}`.

| tier | when | acquisition |
|---|---|---|
| `live` | the Radar tab is open now (`radar_viewing` marker live) | newest at scan cadence, 8-frame loop, zoom/mode prefetch |
| `warm` | a human touched the device (`presence` marker) or looked at radar within 45 min | newest at cadence plus four history frames |
| `watch` | weather present (rain hold 60 min, lightning hold 30 min, echo hold 60 min, forecast ≥ 50 % or precipitation words until < 30 %), or observations unknown (older than 5 min), or a usual glance hour, or a LAN browser polling within 15 min | Site: primary/newest Level III scan only. Region: 8 frames by day (06:00–23:00 local), newest only by night |
| `rest` | quiet weather, nobody around | site listings every 15 min, no Site tiles or Level III products, a 4-tile zoom-5 MRMS sentinel at home every 60 min by day / 120 by night |
| `dormant` | rest for 2 h at night, or no attention for 3 days | one listing an hour, no tiles, no sentinel |

Promotion is immediate. Live drops to warm the moment the tab closes; every other
demotion waits ten minutes in the tier and for every stronger hold to expire. The
page adds `touch=1` to the poll after any pointer event on any screen; the server
writes the `presence` marker (controllers only, at most every 10 s). `weather` (holds
only, never "unknown") drives a small mark on the Radar tab. `waking` is true from a
rise into warm/live until a fresh, complete frame for the accepted preferences
publishes, or 90 s. Partial tile successes do not end waking. The radar note
reads "Waking radar · showing HH:MM while the newest scan loads", or "Waking
radar · fetching the first scan" when there is no image. A scheduled retry or
source refusal takes precedence. `waiting` keeps a quiet engine's Radar tab
accessible even if it has never acquired a frame and startup has timed out.

Real view/touch markers promote demand before an intent pass can start; changed
preference files and boot stamps alone cannot bypass a quiet tier. Quiet passes
retire deferred frame work, and their listing deadlines are measured from the
last quiet check, not from each re-arm. More frame demand (including weather
wakes and daytime Watch) starts acquisition promptly, surviving an in-flight
pass. History acquisition and pending/retry accounting share the same target.
Rest's two-hour dwell starts on actual entry; absent attention markers age from
process startup for the three-day away rule.

`WFP_RADAR_ATTENTION=shadow` publishes tiers without applying them (the test suite
runs so). A file `radar_attention_force` in the data directory naming a tier
overrides the decision for two hours (ops/testing). `/health.radar.attention`
adds holds, the last 32 transitions, knobs, `bytesByTier`, the sentinel result and
glance-history telemetry. Bytes are response bodies read, charged to the tier at
request start, including completed chunks and reported partial bytes on failures;
headers, unreported partial reads, HTTP error bodies that are not read, and
transport overhead are excluded. This is not a wire-byte budget.
Each frame in the manifest carries `echo` (any validated pixel at least 10 dBZ
in its footprint; `null` when coverage is unknown). Site clear air at 5–10 dBZ
does not count. The sentinel uses the same native-palette decoding and intensity
floor, validates MRMS timestamps and PNGs, and reports `complete`. Positive
sentinel evidence needs 20 pixels; negative evidence needs all four tiles.
Holds age from the scan timestamp, not download time. Glance history
(`radar_glances.json`) is inert until 14 days and 30 view starts, then an hour with
three glances and three times the mean rate is an expected glance hour.

## Radar starting state and observation carry-forward (2026-09-16)

`radar.starting` is `null` whenever the engine has a radar result or a conclusive
failure. While the engine has produced neither, because it is booting, it is an
object: `{phase: "cache" | "acquire", sinceSec, cacheFiles}`. `cache` means the
tile inventory is still being validated (about 200 tiles a second on the Pi 4;
`cacheFiles` grows as it goes); `acquire` means the first pass is under way. The
state ends after `RADAR_STARTING_MAX_SEC` (300 s) whatever happened, after which
`available: false` means what it always did.

The page shows the Radar tab when `available` OR `starting`; in the starting
state it shows an empty plate with "Starting" and a caption naming the phase,
and never leaves the radar screen on the user's behalf. A page that has seen
`available: true` in its lifetime treats a later `available: false, reason: "no
data yet"` as starting even from an engine that predates this field.

An engine that has just restarted has no observation for ~20 s and no forecast,
AQI or Sager text until their first fetches. It reads the previous run's `wx.json`
at start and, for `CARRY_WINDOW_SEC` (600 s), fills any still-null top-level field
from it; while no live observation has arrived it keeps the previous `obsTs`, so
`obsAgeSec` is the real age and the page's freshness mark judges the carried
numbers rather than showing dashes. `carried: true` marks such a payload. Radar
(it has `starting`), alerts (they expire), the clock and version fields are never
carried, and nothing older than `CARRY_MAX_SEC` (6 h) is. The kiosk wipes the
browser profile on every start, so this lives in the engine, not the page.

## Radar v5.7 — ordered intent and bounded acquisition

This section is normative for fetch scheduling, intent, publication and switch
completion. It supersedes the older version-specific behavior below; rendering,
reflectivity palettes and legend specifications are unchanged.

### Switch contract

With at least four available scans, a healthy provider path and sufficient
capacity in the shared rolling **240 requests/minute** window, a mode tap or zoom
must reach **four decoded visible frames and an advancing painted loop within
20 seconds of input**. “Ready” metadata and one target tile do not satisfy this
contract. A mode tap synchronously paints `Switching to <choice> · showing
<displayed source>`; zoom synchronously paints `Updating view · showing
<displayed source>`. Both are observable in the first animation frame.

At 20 seconds, an unfinished transaction remains visibly `Updating view` or
`Switching to <choice>`. A missed UI deadline alone is not evidence of a retry.
Automatic loop buffering
settles once decoded playback advances; only a user intent transaction requires
its matching engine acknowledgement.
`Retrying` appears only for a scheduled retry whose `refresh.nextRetry` is
strictly later than the same payload's top-level `ts`. The caption and note name
`refresh.retryReason`; the note says `next attempt Ns`, or `next attempt now`
when less than one second remains. Neither the browser wall clock nor elapsed
switch time determines whether a retry is pending. A past/equal timestamp is
ignored, including legacy payloads. The countdown changes with each payload.
The requested choice and old usable bitmap/loop remain. A retry does not silently
choose another source. Paused, reduced-motion and clear-weather loops explicitly
retain their corresponding static states instead of claiming advancing playback.
The 20-second acceptance gate is measured on loopback; it is not a new claim
about Pi CPU performance.

#### Retry fields (v6.3)

- `radar.refresh.nextRetry`: optional Unix epoch seconds, with fractional seconds
  preserved. Present **only after a retry timer has been successfully armed and
  while its due time is strictly greater than the emitter time at serialization**
  (therefore also later than the whole-second payload `ts`). Absent otherwise;
  it is not a historical timestamp or a null placeholder.
- `radar.refresh.retryReason`: present alongside `nextRetry`; one of `budget`
  (request/build capacity or paced history), `deadline` (acquisition timeout),
  `provider` (provider failure, cooldown or recovery probe), `local` (local
  connection/resources/processing), or `not reporting` (site is not reporting).
  The page uses respectively “work budget”, “acquisition deadline”,
  “provider issue”, “local issue”, and “site not reporting”.
- `refresh.reason` retains its existing acquisition/refusal meaning, including
  `not reporting`; it cannot describe routine budget/deadline yields because
  those can coexist with `state: "idle"` and `reason: null`. The distinct
  `retryReason` carries the scheduled timer's cause. For older producers the
  page can use a recognized `refresh.reason`, otherwise “scheduled retry”.
- Top-level `radar.nextRetry` and `radar.retryReason` mirror the optional refresh
  fields for compatibility; `refresh` is authoritative for the page.

Timer dispatch clears the fields before pass admission, including a busy lane
whose work is queued. A scheduled/discovery pass that overtakes the timer
consumes and cancels it at pass start. Completion retires the fulfilled retry;
`_RadarUnchanged`, supersession and stop also clear it. A successful intent pass
preserves an inherited timer only while it is still pending and in the future:
reusing cached intent knowledge does not replace scheduled validation. A pass
that defers/fails may arm a new retry; that future schedule survives publication
of retained/idle frames. Serialization independently omits an expired timestamp
if timer dispatch is delayed. Cancelled callbacks cannot consume replacement
timers.

Consecutive local-failure passes back off 2, 4, 8, 16, 32, then at most 60 seconds.
Both repair and discovery wakeups honor that floor. Explicit camera/source intent
still gets an immediate attempt; its automatic continuations obey the floor.
Success and ambiguous transport failures reset the streak. Every DNS failure,
whatever its errno (a Pi with its network down reports `EAI_NONAME` as readily as
`EAI_AGAIN`), and every resolver timeout is local: it feeds the streak, never trips
a host breaker and never advances the fallback chain.

Closest-site off-air rechecks currently use `nexrad.nextCheckTs`/`nextCheckAt`
and the discovery schedule, not a repair retry; they keep the measured off-air
note and settled Region caption. `not reporting` is supported as a retry reason
without inventing an additional timer for that refusal. The masthead `STALE`
continues to mean stalled/missing engine data (and `SILENT` old observations);
it never depends on radar retry state. Radar imagery retains its separate age
and refresh-failure status.

### Intent protocol and ownership

The page owns `{session, generation, camera, zoomPolicy, preferredMode}`.
`radarSession` and `radarGeneration` accompany activity. `radarCommit=1`,
`radarSource=auto|mosaic|site`, `radarPolicy=auto|manual`, `radarGeoZoom` and
`radarGeoCenter` form one settled transaction. A mode tap commits immediately;
settling increments generation before the network debounce. Heartbeats carry a
separate increasing `radarHeartbeat` ordinal and never invent a new intent.

A reload follows the accepted runtime intent without claiming. Only a user
commit with a positive page-local generation may carry
`radarClaim=<acknowledged owner>` (empty before the first owner) and
`radarClaimEpoch=<acknowledged epoch>` (zero before the first owner). The server
compares and swaps ownership and writes the camera atomically under its writer
lock. Old sessions, generations and reordered heartbeats are rejected before
camera intent changes. Panel activity uses independent view ordering. A page
whose acknowledgement names another session demotes itself, discards its rejected commit and follows the accepted
camera, zoom policy and source. Reconciliation never creates a commit.
`X-Radar-Intent` returns `{intent, session, epoch, generation, acceptedGeneration}`.
Runtime records include session, epoch, generation, resolved numeric zoom, zoomPolicy,
source, center, acceptedAt and the existing worker sequence. A 250ms debounce
persists `auto` as policy, not its resolved number. Payload/displayed source is
never implicitly sent back as preference after a failed attempt.

Motion continues reporting/polling during gestures, without committing the
intermediate camera. Moving ownership expires after five seconds; geo work
resumes after settlement or lease expiry. A true camera/policy no-op retains
all decoded frames. Policy-only Auto changes commit without invalidating them;
rapid zoom presses accumulate from the pending animation target.

### Remote control

There is one engine view. Browsers on the home network can steer zoom, pan,
Auto/Region/site and Smooth; the panel follows. Controllers are loopback
and explicit private ranges: IPv4 `10/8`, `172.16/12`, `192.168/16`; IPv6
`fc00::/7`, `fe80::/10`; IPv4-mapped IPv6 addresses use their IPv4 classification.
The peer address is classified with `ipaddress`, not forwarded headers or the
broader `is_private` category. IPv4 default gateways are excluded: this network's
router rewrites forwarded internet requests to its own LAN address. At startup,
`/proc/net/route` supplies all active default gateways (little-endian IPv4).
Missing/unreadable route tables yield an empty exclusion set off Linux; tests
inject route files or the gateway set. Other addresses remain read-only and
receive no control acknowledgement headers. The listener still requires a LAN
bind (`WFP_BIND=0.0.0.0` for IPv4); the default is loopback.

Ownership is **last accepted user action wins**, using the compare-and-swap
transaction above. Every transfer increments a server ownership `epoch`, also
stored in the runtime intent and returned in `X-Radar-Intent`. A takeover must
echo both `radarClaim` (owner session) and `radarClaimEpoch`; an old A→B claim
cannot succeed after A→B→A. Per-session accepted generation and heartbeat high
water marks survive ownership changes. A generation at or below its accepted
high water can never recommit a camera, even with a refreshed claim. Same-owner
retries may acknowledge the same generation without rewriting it. The table is
bounded at 4096 accepted sessions for the server lifetime: it never evicts a
fence, and rejects new sessions at capacity while existing sessions continue.
After a server restart, the runtime record restores the current owner's epoch
and generation fence.

Passive polls, opening Radar and reloading never claim. A rejected claim follows
the winning view until another user action. Smooth and renderer writes are
explicit pending taps with a valid `radarSession`, independent of camera
ownership. Controllers receive `X-Radar-Intent`, `X-Radar-Smooth`,
`X-View-Session` and `X-Radar-Panel: 1|0`; only loopback gets
panel value `1`.

The panel alone may recenter an away camera after the accepted intent is 90
seconds old, regardless of camera ownership. It must be visible on Radar with
no active local gesture or pending commit. The timer uses accepted intent age,
not the age of the last follower poll. It rechecks eligibility before firing
and posts a normal settled commit, including the epoch claim when taking over.
Reloaded panels recover this timer; LAN pages never auto-recenter.

`_view_transaction`, `radar_viewing`, `radar_viewed`, `radar_activity` and `r=1`
render counts are **panel-only (loopback)**. An admitted panel report updates
activity (including viewport and theme) whenever its view-ordering transaction
accepts, regardless of camera ownership. The same acceptance gates
`radar_viewed`, so a delayed radar poll cannot refresh the 15-minute hint after
the panel left Radar. LAN themes never select the panel's geography prebuild.
All no-session legacy camera paths (settled camera, ordered legacy intent and
individual durable preferences) are loopback-only, and stop once a session owns
the camera. LAN pointer and keyboard input set `touch=1` presence; an unattended
browser does not. Source expiry runs before recording touch, so expired manual
choices are not revived by presence.

A per-peer token bucket admits 20 controller polls/second with a burst of 60.
Activity, presence, expiry, preferences and panel viewing writes share this
admission check. Panel render counts are never throttled: the watchdog must
still see successful paints. Panel-only `/radar-bad-tile` reports use a separate
bucket and return 429 when exhausted. IPv4 and mapped aliases share buckets.
Buckets use monotonic time, reclaim clients idle for 60 seconds and cap storage
at 4096 clients (new clients cannot write while the table is full).

Throttled `wx.json` responses still serve data and current acknowledgements,
with `X-Radar-Throttled: 1`. The page keeps pending camera commits, preference
taps and presence, renders the data, and retries on the next poll. A throttle
acknowledgement is not a rejection of ownership. Followers never label camera
changes they did not make as “Updating view”. Engine-initiated Auto source
switches do show “Switching to <target> · showing <drawn source>” on every page
when the refresh target differs from the drawn source.

### One worker and fixed work order

One radar worker coalesces the newest transaction. Explicit pending newest,
four-frame, eight-frame and optional work survives discovery and busy-worker
wakeups. Unchanged metadata cannot discard unfinished acquisition. Supersession
stops old admissions; bounded paid-for tile work may finish into immutable cache.
Every unfinished mandatory task retains a continuation.

| Priority | Work |
| --- | --- |
| 1 | Visible newest tiles, nearest first |
| 2 | Four complete visible frames, newest first |
| 3 | Eight complete visible frames |
| 4 | Newest margin, opposite mode, then adjacent zoom warming |
| 5 | Deep history after the continuous-view gate |

Optional work reserves missing mandatory tiles across every required site layer
plus the existing deep-history headroom. Mandatory acquisition does not reserve
capacity for optional work. All attempts, metadata and archive probes share the
240/minute gate. Margin completeness never determines visible playback readiness.
Source transitions stage four complete server frames and four decoded browser
frames behind the retained source before acceptance (or all available frames
when the provider offers fewer than four, with the retry contract still visible).

Camera footprint, echo native level and basemap level are explicit: basemap uses
camera level; capped echo coverage scales 956×490 by `2**(native-camera)` before
tile enumeration. Every optional target honors its adapter limit, including
MRMS ≤9 when site camera zoom is 10 and RainViewer ≤7.

### Bounded work and failure ownership

HTTP retains six leases and two concurrent TLS setups per host, warm-only
hedges, at most two attempts, and absolute deadlines through admission, DNS,
connect, TLS, headers and body. Ordinary/background tile work retains its six
second budget. Mandatory committed-camera work uses a **two-second total tile
budget**, first attempt up to 750ms and a warm hedge after 500ms, leaving time
for fresh retry. The source/pass caps remain 16/25 seconds. No exception creates
a third tile attempt.

DNS and local route/resource errors are local; a silent reused connection is
ambiguous until a fresh path supplies evidence. Neither category contributes
provider-failure samples. A fresh service timeout or invalid service response
can. Three consecutive failures to deliver the acquisition objective qualify
fallback even if most individual requests succeeded. Local rate admission is
separate. Five-minute dwell applies to elective recovery while fallback remains
usable; three failed active-source passes permit emergency escape.

The page shares four bounded echo/geography slots. Each job has a 2.5-second
absolute deadline covering headers, body and cancellable image decode, owns an
AbortController and always releases admission. Superseded jobs are cancelled;
late completion cannot publish into another generation. Echo work precedes geo
work. Composites yield after three seconds without progress and retry after two
seconds, allowing another usable frame to compose.

Metadata is limited to 128 entries with one-hour age pruning, negative entries
to 512 with expiry, archive positives to 128, and active process-wide resolver
jobs to four even across HTTP session replacement. Browser negative maps are
also bounded at 512. Native tiles remain capped at 400 and browser accounting at
40MiB. The two disk variants share the instance limits described below. Disk protection pins the exact current and retained
visible eight-frame loops, not every recently accessed camera. A process-owned
validated inventory is built once on `radar-inventory`, started at boot before
the first provider poll. Normal passes never walk/stat/open cached PNGs. Writes
atomically add the key `(source, site, stamp, z, x, y)`, byte length and validated
metadata; evictions remove it. Frame/level masks are cached by scan/level mutation
generation (1,024 bounded entries); site coverage is cached by geometry (512).
The existing cached geography/render revision functions remain in place.

At boot, disk caps are derived from the nearest existing ancestor's free space:
bytes are 2% of free space, clamped to 64,000,000–256,000,000; files are bytes/8192,
clamped to 8,000–12,000. Failure to query the disk uses the lower bounds. These
are retention limits, not guaranteed boot durations or reserved free space.
`MAX_ENTRIES` is max(40,000, 3×file cap). Both installed render trees are discovered
together and admitted newest-stamp-first across sources/sites/variants. The
entry budget bounds admission/PNG validation, yielding every 32 files; discovery
of stamp directories and removal of unowned files must still traverse metadata
outside that budget. `health.cache.startup.entries` counts charged admission
entries; `discoveredEntries` separately counts source/site/stamp entries examined.
An early UI intent yields the radar lane until this separate worker finishes;
any wait for it precedes the provider acquisition deadline.

Every unindexed file is removed, including abandoned temporary files and the
unscanned remainder of a partially indexed stamp. Symlinks are removed without
following their targets. Indexed records are sorted by stamp across both variants
and evicted oldest-first to both instance caps. A full or truncated cache becomes
writable after reconciliation. `health.cache` publishes actual files/bytes, caps,
readiness and combined startup removal/eviction totals. Normal operation uses the
process-owned inventory; no periodic filesystem reconciliation is performed.

The local page reports an unexpected tile 404 or invalid PNG through loopback-only
`POST /radar-bad-tile` (one canonical tile path, at most 256 bytes). The server
atomically retains at most 128 paths in `radar_bad_tiles`. The engine reads only
changed report files, validates only reported indexed tiles, invalidates damage,
and resumes acquisition. Healthy reported tiles remain cached. External cache
maintenance must notify the index owner or restart the emitter. This replaces
silent external deletion detection on every pass.

Per-pass work is `O(M + W + P + E)`, independent of the total disk inventory:
`M` is missing tiles, `W` is the bounded requested frame/site/zoom footprint,
`P` is the bounded displayed/retained pins at pressure, and `E` is evicted tiles.
Discovery/intent reads are constant or bounded by four selected sites; planning
looks up the requested footprint in memory; cached masks avoid repeated geometry
work; each changed scan/level recomputes only its requested mask. Raster work and
one atomic file write per new tile run on tile workers. Pressure eviction walks
victims and displayed pins, with no directory enumeration or stat. The counting
gate covers radar-cache paths: zero stat/open on warm passes, and at most one
open per written tile plus two constant operations on cold passes. Intent/view
marker checkpoints are separate control I/O, proportional to completed work.
`wx.json` serialization remains on the two-second emit tick; intermediate worker
snapshots coalesce there. No per-tile JSON write is scheduled.

### Publication and measurement

Immutable inventory includes its own `intent`, `geometry`, `camera` and
`publishedAt`. `radar.intent` identifies those pixels; `radar.refresh.intent`
identifies acquisition in progress, which can be newer while old imagery is
retained. `advertisedTs`, `acquiredTs` (also legacy `observedTs`), pending work,
nextRetry and switchDeadlineSec=20 are explicit. Completed inventory and progress
are captured under the same publication lock. A panned camera uses station
identity plus camera bounds/native zoom/source identity for regression checks;
newer matching frames cannot disappear when metadata goes backward.

The page separately labels the frame actually painted and ages it using a
monotonic clock. A newly advertised stamp cannot move acquired time before a
tile lands. Failure copy works even without an observation timestamp.

`/health.radar` includes bounded phase/request records: intent, monotonic and CPU
time, requests, bytes, queue wait and failure class. Browser `radarMetrics.path`
records tap, accepted intent, publication, first visible tile, decode/four frames,
advancing paint and deadline retry. Correlate records by session/generation;
server/page monotonic clocks have different origins. The loopback harness records
publication and request traces alongside these measurements.

Acceptance: `test_radar_v57.py`, the request-sequence tests in
`test_radar_priority.py`, `verify_radar_v57_browser.py` (both themes),
`verify_radar_v57.py --flaky` (30% first-attempt hangs, zoom 8→7→6→7→8 and
Region↔site), and `--cold-switch --sites 2|4` with 30 requests already used.
The full offline suite and both-theme `verify_radar_headless.py` remain required.

## Radar v4 — the panel owns the map

`radar` is an independent side artifact. It never changes `ts`, `obsAgeSec`, or
engine `/health`. Missing radar or `available:false` hides the tab. A station
change releases the previous station's radar and camera. Geography and source
knowledge can be published before the first observed tile; a null observed time
never invents a measurement.

The page owns one `{lat,lon,zoom}` Mercator camera. Pointer events move that camera;
no gesture waits for a request, payload acknowledgement or server image. The
emitter acquires and validates native 256×256 XYZ tiles, then publishes immutable
256×256 remaps for both Smooth off and Smooth on (v6.1). It no longer
allocates viewport RGBA canvases, per-site RGBA layers, crops or basemap SVGs.
`lib/data/radar-natural-earth.bin` remains the sole bundled geographic source.
Attribution remains text; no provider origin is added to Chromium's URL policy.

The default preference is **mosaic** when no saved choice exists. A durable
`site` preference survives restarts; an unavailable saved site falls back to
mosaic without an error card. This resolves “cold start Mosaic” as first use,
while respecting the requested restart persistence.

| Source | `sourceId` | `provider` | Nominal `cadenceSec` | Display stale at (`staleSec`) | Zoom bounds |
| --- | --- | --- | --- | --- | --- |
| IEM MRMS | `iem-mrms-lcref` | `iem` | 120 | 600 seconds | 4–9 |
| IEM NEXRAD N0B | `iem-nexrad-n0b` | `iem` | 300 | 900 seconds | 7–10 |
| RainViewer | `rainviewer` | `rainviewer` | 600 | 1200 seconds | 4–7 |

Mosaic tries IEM first for CONUS station centers, then RainViewer. A bundled
coarse land polygon determines CONUS eligibility. Outside CONUS, RainViewer
is used directly. Site-mode eligibility additionally requires the nearest
bundled NEXRAD within 230 km. This is geographic eligibility, not a guarantee
that a radar sees every point inside that circle. `nexrad` still reports the
nearest site within 285 miles; `distanceMeters` provides the unrounded value
used to decide eligibility.

### Auto source (2026-09-25)

The source picker is **Auto | Region | <site>**. The site keeps its callsign and
contributor count, such as `KATX +3`. Auto is the default without a preference.
`radar_source` and the camera transaction accept `auto`, `mosaic` and `site`.
The same controller check, single-value validation, camera owner/generation fence,
atomic durable write and `X-Radar-Intent` acknowledgement apply to all three.
Moving camera reports cannot commit a source choice.

`radar.sourcePref` is the requested policy. `radar.sourceMode` is the displayed
source (`mosaic` or `site`). Auto stays pressed while either source is drawn.
Its normal caption starts with `Auto · `, for example `Auto · Region` or
`Auto · KATX radar`. `refresh.targetMode` names a staged source. The page uses
`Switching to … · showing …` during that transition and retains the old image.
Site always uses the Level III mosaic, including in Auto and on Pi 3 boards.

`lib/radar_auto.py:choose` uses only the accepted, settled camera zoom:

- At zoom 8 or higher, choose Site.
- At zoom 6 or lower, choose Region.
- At zoom 7, retain the displayed source. A cold start uses Region.
- Enter Site with a fresh reporting closest radar and at least 85% coverage;
  remain in Site down to 70%. At zoom 7, measure coverage on the zoom-8
  footprint at the same centre. Coverage is the union of the reporting sites'
  230 km spherical range discs, integrated over 512 Web Mercator screen rows.
  Overlaps count once. Tile margins do not count as viewport area.
- Budget, timeout and transport outcomes are initially unknown. Unknown evidence
  can hold Site only while its displayed scan age is strictly below
  `RADAR_SITE_MAX_AGE_SEC` and its adapter has fewer than three consecutive
  provider failures. Otherwise select Region through the normal staging path.
  Failed listings replace reporting=true with unknown, then unavailable after
  one Site scan cadence (300 seconds from the first consecutive failed check).
  Repeated failures do not restart that clock; a successful check resets it.
  The closest radar must report; unknown neighbours matter only when their range
  discs could change the 85% entry / 70% stay verdict. A redundant failed listing
  cannot veto Site. A real not-reporting listing or dark-site refusal can select
  Region immediately. Region remains in the adapter chain after Site fails.
- Hold against a reverse automatic switch for 10 seconds after publication,
  unless settled zoom moved at least 2 levels since that switch. Loss of valid
  reporting takes precedence over this hold; coverage changes obey it. The existing intent
  watcher retries when the hold ends. Failed candidates never reset the clock.

Selection does not publish a candidate. The existing frame staging path retains
the old source until the candidate newest frame is publishable. The page keeps
its decoded frame staging for source changes: the complete previous loop stays
visible until min(4, n) frames of the new loop are decoded, where n is the
advertised target, not just the frames published so far. Tile scheduling and
acceptance use the same readiness predicate. A renderer-only change on the same
source/site accepts as soon as the new variant's newest frame is decoded;
history backfills normally. A degradation says `Level III unreachable` (or
`Daily Level III limit reached`), then `loading IEM tiles · showing NOAA Level III`
until decoded fallback tiles replace native. While fallback tiles draw, it says
`showing IEM tiles`. Recovery says `Restoring NOAA Level III · showing IEM tiles`.
Renderer transitions never use “Sharpening” and never blank the existing map.
Region still has a native zoom ceiling of 9; Auto
can select Site at camera zoom 10 when the guards permit it.

A manual Region or Site choice holds until Auto is tapped or 45 minutes pass
without a touch. The lease reads the existing `presence` marker used by radar
attention; view polling does not renew it. Without a touch marker, the source
preference's timestamp starts the hold. Future lease timestamps are clamped to
the first observed current time. A stamp-specific exclusive anchor file in the
marker directory preserves that time across server/engine restarts; repeated
polls and restarts cannot extend the hold. If a durable anchor cannot be read or
written, that future marker expires rather than receiving another lease.
Camera moves alone do not renew it; the page sends
`radarSource` only for an explicit source change, and camera-only transactions
preserve the source lease's acceptance time. A poll acknowledges an explicit
source change only if that request actually carried `radarSource` and its
intent generation is still current. An older poll cannot consume a newer tap.
The engine checks expiry without needing a browser. The server persists Auto
before recording a subsequent touch, so an expired choice cannot revive on the
next poll or restart. Manual Site retains the zoom-7 floor and
`site-zoom-floor` fallback.

### Closest-site evidence and refusal (v6.0)

The closest-site identity stays independent of the drawn timeline. When `nexrad`
is non-null, these fields describe its **latest listing check**. Failed checks
report unknown availability, aging to unavailable after one scan cadence; they
never preserve an earlier reporting=true verdict. Successful cached listings
retain their check time, and Auto reuses them for at most one scan cadence.
Auto bounds displayed Site retention by `RADAR_SITE_MAX_AGE_SEC`:

| Field | Meaning |
| --- | --- |
| `nexrad.reporting` | Boolean freshness result at the latest listing evaluation; null during the first cadence of an unknown check. False alone does not prove an empty listing: inspect `reason` and `newestTs`. |
| `nexrad.newestTs` | Newest accepted UTC scan epoch from the listing, or null for an empty/failed listing. Existing listing validation and history horizon still apply. |
| `nexrad.ageSec` | Nonnegative whole seconds since `newestTs`, recomputed at publication; null without a scan. This clock advances while `checkedTs` stays fixed. |
| `nexrad.reason` | Null when reporting, `not reporting` for an empty or over-age listing, `scan unavailable` for a failed/invalid listing; null before any check. |
| `nexrad.checkedTs` | Wall-clock epoch of the latest listing attempt. Cache reuse and heartbeat publication do not advance it. Null before a check. |
| `nexrad.checkedAt` | Station-local configured-clock rendering of `checkedTs`, or null. It is a check time, never a claimed outage start. |
| `nexrad.nextCheckTs` | Current existing discovery wakeup epoch, or null when no wakeup is scheduled. Budget/busy-lane rescheduling changes this value. |
| `nexrad.nextCheckAt` | Station-local configured-clock rendering of `nextCheckTs`, or null. |
| `refresh.reason` | `not reporting` for an empty-listing refusal; otherwise null/absent. Existing `refresh.intent` identifies the rejected session/generation. Refusal leaves Region's refresh state idle and does not mark its measurements stale. |

`sourceFallback` adds `site-not-reporting`: the durable site preference is kept
while the existing Region window is shown. A refusal does not acknowledge new
geometry in `tiles.intent` or change any scan timestamp. It creates no provider
failure streak, repair retry or 20-second switch deadline. The engine checks
last evidence before transport on a tap; newly empty listings also refuse in
that pass. This applies to an empty closest-site choice from an already measured
Region view. Existing site playback still uses its nearest-reporting timeline;
an old but nonempty listing or a transport error follows normal acquisition.

While Region shows, its existing discovery wakeup checks the closest eligible
site before the unchanged-MRMS early return, including manual Region and Auto
below zoom 8. Quiet rest/dormant passes also refresh that listing on their
existing tier cadence. These checks preserve pre-tap status, scan/check times,
and immediate dark-site refusal. Auto at zoom 8+ gathers viewport Site evidence
for its decision, reusing successful listings within a 300-second cadence and
caching coverage by viewport geometry and reporting-site coordinates (bounded
to 16 entries). The independent Region closest-site check retains its original
discovery cadence rather than inheriting the Auto decision cache interval.
Auto includes Site breaker dependencies only at settled zoom 8+ or while Site
shows; an unused Site/S3 breaker cannot shorten Region's discovery due time or
create probe recovery retries. Region's request admission and 100 ms intent
watcher do not evaluate native policy; a Region fallback checkpoint uses the
acquisition target even while Site is still displayed. There is no additional
timer, emit-triggered I/O or polling loop. The closest-site check shares the
pass deadline and 240/minute gate, preserving the footprint-aware mandatory
reserve (at least 34 interaction slots). It yields under budget pressure without
inventing a new observation. A per-pass listing table prevents repeating the
closest-site request when site acquisition/fallback/warming follows in that
same pass. Later discovery refreshes both site knowledge and Region imagery;
reporting recovery can fulfill the durable site preference on that wakeup.

A settled, viewed Region warms the Site newest tiles at the same centre and
zoom plus eligible neighbours; a completed mosaic return retries this warming.
Manual Site retains opposite-mode Region warming. In Auto, Region warms Site
at settled zoom >= 7, and Site warms Region at settled zoom <= 7. Warming fetches
Level III products at the normal ceiling; the newest-only ceiling warms only
the primary site’s newest scan. IEM tiles warm only during a Level III outage
or the paused ceiling. Shadow attention does not gate native. Existing viewed/idle, attention prefetch, and interaction
reserve admission still apply.

Both themes keep the closest segment tappable when the site is `not reporting`
or its scan is unavailable. A second line, also in its accessible name, says
`off air · checked 09:12`, `last scan 27 min ago`, or `scan unavailable` according
to the evidence. Null knowledge adds no guessed status. An empty-listing tap
immediately says `KATX is off air · showing Region · Checking again at 09:32`
(omit the last clause without a schedule). It bypasses the note's 600ms grace
and cancels switch timing; a matching engine refusal does the same in its poll.
Older generations cannot refuse a newer choice. Region's existing caption is
unchanged. Choosing Region can cancel the saved site preference after refusal.
An empty recent listing cannot establish “off air since HH:MM,” so the UI
explicitly labels the time as a check.

### Acquisition, budgets and tile service (N)

MRMS metadata:
`https://mesonet.agron.iastate.edu/data/gis/images/4326/mrms/lcref.json`.
`meta.end_valid` must be UTC, an even minute, fresh, with `product:lcref` and
`units:0.5 dBZ`. Conditional requests/304 retain validators and revalidate age.
Candidates start at the advertised `end_valid`. In v5.0,
`RADAR_IEM_READY_LAG_SEC = 300` predicts when the next scan should be fetchable;
it does not cap the advertised stamp or delay an early available scan. Tiles
that are not rendered yet remain missing for this pass; validated sibling tiles
publish partial and the next pass repairs from cache. Newest-acquisition archive
misses expire after 20 seconds so a previous miss cannot mask readiness; history
and prefetch retain their 120-second negative cache. Every accepted timestamp
still names matching provider tiles.

During validation, an MRMS candidate probes the original archive with HEAD
unless that stamp already has a successful probe:
`https://mesonet.agron.iastate.edu/archive/data/YYYY/MM/DD/GIS/mrms/lcref_YYYYMMDDHHMM.png`.
Tiles use
`https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/mrms::lcref-YYYYMMDDHHMM/z/x/y.png`.
UTC rollover applies to both paths. MRMS's native raster covers longitude
−130…−60, latitude 20…55; crossing that domain sets `partialCoverage:true`.

As of 2026-09-14, newest discovery belongs to the source, independently of viewport
geometry. Successful MRMS metadata/readiness/archive validation retains `(newest_stamp, validated_monotonic)`; RainViewer retains its
validated manifest paths/host with the stamp, before tiles begin. N0B retains parsed scan listings
(including empty listings) and newest stamp separately per site. Intent-triggered
zoom, centre, source changes and supersede restarts reuse this knowledge only
while monotonic age is strictly below the source cadence: MRMS 120 s, site 300 s,
RainViewer 600 s. Reuse does not reset that clock. Scheduled/retry passes, first
passes and sources/sites whose acquisition failed validate again; expiry cannot
be extended by repeated interaction. Newly encountered sites list independently. Selected viewport and primary-timeline
sites list concurrently in a four-worker pool; nearest reporting primary and
per-frame scan alignment are independent of response arrival order.

A pass reusing MRMS knowledge goes straight to tiles for newest and history,
without metadata or archive requests. Full validation caches each positive HEAD
for the emitter lifetime, independently of zoom, viewport, transport session and
cadence; negative probes retain the 120 s retry TTL. All tiles still require the
same PNG validation and tile metadata rules. Failure of remembered newest tiles
(including a purged scan's 404 or failed site layer) discards knowledge and runs
one full source validation in the same pass, sharing its original deadline,
build limit, cooldown and request budget. Supersession is not a provider failure.
Idle prefetch reuses still-valid remembered stamps/listings. It may discover the
other mode: cached per-site listings for N0B, or MRMS metadata plus archive
readiness when no valid MRMS stamp exists. These requests share the background
reserve and cancellation checkpoints. No separate background worker is added; provider cadence,
observed timestamps and stale thresholds are unchanged. v4.9 removes the artificial
readiness delay and yields incomplete newest work before starting these tiers.

The transport uses standard-library `http.client.HTTPSConnection`, with at most
nine leased connections per host during tile races: six ordinary workers plus
three rescue slots. Metadata-only sessions retain the six-connection bound. A
connection stays leased until the response body has been consumed or closed;
newest frames use six tile workers, history
and idle prefetch use four. Sessions and successful IPv4 DNS results survive
worker passes and socket expiry. Each host is resolved with `AF_INET`; the
resolved IP retains the original TLS SNI, certificate hostname verification and
HTTP Host. DNS has its own 15-minute monotonic TTL. One caller resolves outside
the pool lock, with a per-host event sharing the result among cold callers;
another host can proceed meanwhile. Expired good addresses remain immediately
usable while one background refresh runs. A failed refresh keeps those addresses
and retries next pass; a cold resolver failure is also charged only once per pass.
System DNS cannot be interrupted by socket timeout: cold lookups also run in a
daemon resolver thread. Every cold caller waits on the shared event only until
its deadline; a late result may populate the cache, but cannot issue a request.
Cached callers never wait for resolution.

**Transport baseline (v5.7 mandatory-camera timings above take precedence).** Each immutable tile gets at
most two attempts, each with a six-second timeout including DNS, pool waits, TCP,
TLS, send and every raw header/body read. Their race is bounded by twelve seconds
and by the unchanged source/batch/pass deadline, whichever expires first. A failed tile does not abort siblings.
The winner must be a complete, bounded, decoded 256×256 native PNG; truncated,
placeholder and invalid responses cannot win. A fast failure retries immediately
on a fresh connection. A partial-body failure may also retry the immutable tile.
No third attempt is possible, including through the v4.8 transport retry path.

For newest only, no received-byte progress for `RADAR_HEDGE_SEC = 2` launches the
second attempt only on a reserved warm connection while the first remains eligible to win.
Every raw socket read resets inactivity, including partial status lines, headers,
chunk framing and bodies. A header/body stall is raced just like a silent first
byte (v5.9); continuing progress postpones admission. Both attempts retain their
absolute caps and the enclosing batch/pass deadline. First valid response wins. The loser is shut down, drained/joined
and discarded before cache mutation. At most `floor(len(ctx.tiles)/2)` hedges are
reserved per source pass, shared by its site layers; every admitted hedge/retry
uses the rolling rate gate. The cap is conservative for multi-site views (the
viewport count, rather than the sum of layer tiles). Six busy primaries leave no
hedge capacity. Reused primaries still have a three-second first-byte deadline;
a sequential retry has its own up-to-six-second attempt budget within the remaining
batch/pass deadline. Without a warm hedge, a six-second timeout must not consume
the second attempt's entire budget.
There are no extra rescue slots or hedge pool waiters.
History and warming have four tile workers, sequential retries and no hedges.
An incomplete newest publishes its partial inventory and requests another pass
(normally two seconds with headroom) before history or warming can start.

Metadata and archive HEADs have eight-second absolute request budgets. The
v4.8 zero-byte retry remains for these: a reused socket gets up to three seconds
from send for its first response byte; a zero-byte failure retries once fresh
inside the original eight-second budget. First attempts and retries are sampled
and rate-gated. Partial metadata responses are not replayed. Cold discovery is
still a dependency and does not promise a three-second acquisition; valid intent
knowledge and stale-while-refresh DNS remove that wait on warm paths.

`RADAR_BUILD_DEADLINE_SEC = RADAR_PRIMARY_DEADLINE_SEC = 25` remains the shared
absolute pass deadline. Each source gets at most 16 seconds to acquire newest,
leaving up to nine seconds for the next source. Only three consecutive failed
provider passes advance the site → MRMS → RainViewer chain. A successful/unchanged pass resets the streak. Local setup, pool and rate-budget failures retain the source; provider-caused
acquisition deadline exhaustion qualifies objective failure.
The previous source's manifest and frames remain published until the candidate
has at least four complete frames (also when staging off-tab). An automatic
usable fallback dwells for 300 seconds before elective preferred-source recovery;
three failed active-source passes permit emergency escape;
explicit source/mode choices can change that chain. Each committed switch logs
old/new source and its reason. A failed recovery continues refreshing the active
fallback. Candidate tiles remain reusable between passes.
After newest publication, background tiers may use the remaining original
25-second budget. Resumed warming gets a new 25-second pass. All active requests
and their cleanup remain bounded by the original deadlines (0.4–0.5 second test
cleanup grace; no hard real-time Pi claim).

Idle sockets still expire after two seconds or the shorter advertised Keep-Alive
minus 250ms. Every checkout checks expiry. A hedge atomically reserves a warm,
idle, connected socket before request admission; no available lease means no
hedge, DNS lookup, pool wait or handshake. Retries may open fresh connections.
The pool stays at six connections per host, with at most two concurrent TCP/TLS
setups per host. One verified SSL context per host retains server session tickets
for subsequent connections, without weakening certificate or hostname checks.
`Connection: close`, provider changes and shutdown discard connections. SNI,
certificate verification and IPv4 DNS caching remain intact. Best-effort Linux
TCP keepalive uses 2/2/2 seconds/seconds/probes; missing options are harmless.

**Host circuit breaker.** Completed attempt outcomes form a rolling 60-second
window per hostname/port. At ≥6 samples and <50% successes, stop admitting that
host for 30 seconds. Client TCP/TLS setup or pool-admission timeouts count as
`localFailures`, not host samples. Cancellation of a losing attempt is also
excluded; it does not overwrite the last meaningful error. At a tile deadline,
setup still counts as local, while an established request waiting for a response
counts as a host failure; deadline cleanup is not a discarded hedge.
HTTP 404 is a responding host with unavailable content, not a host outage.
After cooldown, one fresh metadata request owns the `half` state; concurrent
probes are rejected. Success clears the old samples and closes; failure opens
another 30 seconds. Tile-only hosts use HEAD of a remembered tile URL because
another metadata hostname cannot prove their recovery. Existing 429 cooldowns
and the shared rate cap still apply. Fallback and recovery obey the failed-pass
threshold, publication barrier and dwell above.
A shared-host outage applies to both IEM products. Paid-for successes stay cached.

**Operator health.** `wx.json.radar.health` is exposed as `/health.radar`:

```json
{"lastSuccessTs":1789444700.5,"successRate60s":0.78,"hedges":9,"stallHedges":3,"retries":2,
 "discardedHedges":2,"localFailures":3,"hedgeSuspendedSec":0,
 "breaker":"closed","lastError":"radar connection/TLS setup timed out",
 "hosts":{"example.invalid":{"breaker":"closed","samples60s":41,"successRate60s":0.78}}}
```

`lastSuccessTs` is Unix seconds of the last usable newest publication (including
partial), not the measurement timestamp. Ratios are 0–1, null without samples.
`hedges` and `retries` are distinct cumulative admitted second attempts, including
bounded retry DNS/pool waits. `hedges` counts warm overlapping requests raced after 2 seconds
without received-byte progress. `stallHedges` is the subset issued after some
response bytes arrived; silent-first-byte hedges are only in `hedges`. `retries` counts second attempts after the first has
failed (including transport recovery). A winning or losing hedge is never a
retry; either kind consumes the tile's sole second attempt. Denied admission
increments neither counter and does not consume the second attempt. A warm
lease may return after the first hedge admission check: the race retries
admission every 50ms, bounded by the original tile/batch deadline. Receiving
a response byte rearms the inactivity deadline. No extra connection, request, attempt,
hedge allowance or deadline is granted. `discardedHedges` counts issued hedges that did not
win, including failures/deadlines; a primary cancelled by a winning hedge is not
a discarded hedge. If discarded/issued is greater than 50% in the rolling
60-second window, suspend new hedges for 300 seconds (`hedgeSuspendedSec`).
Sequential failure retries remain available. A new observation window begins
after suspension; old losses cannot repeatedly retrigger it.
`breaker` is the worst state across known hosts (`open`, `half`, `closed`), so a
working fallback does not hide the primary outage. `lastError` retains the last
request error even after recovery. Radar faults do not change the engine/sensor
HTTP health verdict. Health reflects the latest engine payload, like other
`/health` fields.

**v6.2 logging (no wire-field changes).** The per-pass INFO message is a compact
summary, separate from the health object: `outcome`, attempted `source`/`site`,
`elapsed` seconds, `requests` attempted, `ok`, `failed`, failure `classes`,
per-pass `hedges`, aggregate `breaker`, `nextRetrySec`, and one `error` from this
pass (not historical `lastError`). Counts cover admitted transport attempts,
including inner stale-connection retries, and are independent of the rolling
128-record histories. Validated HTTP 304 is OK; HTTP failures (including missing
404 tiles), local, host, ambiguous and cancelled attempts have distinct classes.
Denied retry admission adds no extra attempt. `nextRetrySec` is the nonnegative
delay to the earliest scheduled acquisition retry or discovery, null if neither
is scheduled. This is logging telemetry only; scheduling is unchanged.
Errors are escaped onto one line and limited to 240 encoded characters. Tests
require a complete pass message plus newline under 768 UTF-8 bytes even with
saturated histories and long Unicode/control-character errors. The complete
request/phase histories stay in `/health.radar`; no DEBUG history duplicate is
emitted. The v4.8 transport retry INFO retains `transport_retries` and
`stale_first_byte_retries`.

Failure WARNING messages are keyed by `(source, exception class, full message)`;
clipping affects display only. The first occurrence and changed failures log
immediately. Identical failures, including concurrent site listings, repeat
only every 600 monotonic seconds: five 120-second discovery backoff intervals
provide a ten-minute reminder during an extended outage. Reminder, change and
recovery lines include the number of suppressed warning occurrences (not passes).
Verified source recovery emits INFO once and resets its episode. Budget-only or
cached passes and success on another source do not announce recovery; successful
listings can resolve listing errors, but cannot resolve tile-acquisition errors.
`PYTHONPATH=. ./venv-test/bin/python tools/benchmark_radar_logging.py` measures
1,800 passes at two-second intervals in Region and Site with fake transport and
clocks, no network or sleeps. `--revision HEAD` runs the identical harness against
the local git baseline without altering the tree. Byte counts include UTF-8 log
messages and newlines, excluding logger-specific prefixes.

v5.0 adds `health.discovery`: `expectedReadyTs`, `nextPollTs`, `lastPollTs`
(Unix seconds, null before known), `fastPolls` (0–6), `backingOff` (the bounded
fast window is exhausted), and `ageSec` (current newest measurement age, null
without a measurement). These remain in `/health.radar`; per-pass INFO includes
only the next retry delay, not this discovery object.
`nextPollTs` includes budget/breaker delays and busy-lane rearming. Age is computed
when telemetry is emitted, never frozen at fetch time.

Validated native PNG bytes enter a 400-tile in-memory LRU before cancellation is
checked. Keys include source, site when applicable, scan timestamp, zoom and tile
X/Y. IEM native bytes do not depend on the console palette revision; RainViewer
keys also include its server-side colour scheme and options. A pan or restarted
pass reuses overlapping tiles without HTTP, even if the earlier crop never finished.
Truncated, oversized, placeholder and invalid tiles never enter this cache.

The existing request budget remains 240/minute, rolling and shared across every
source, HEAD, metadata request, tile hedge and retry. The interaction reserve is
34; optional warming and deep history retain 60 further slots. No gesture bypasses
a provider cooldown. The worker has one flight, its preference watcher runs every
100 ms, and supersession drains paid-for tile responses into the cache before
scheduling the latest view. A superseded pass publishes no restarted notice.
Successful partial tiles survive another tile's failure or a budget yield.

| Priority | Work |
| --- | --- |
| 1 | Visible newest tiles, nearest first |
| 2 | Four complete visible frames |
| 3 | Eight complete visible frames |
| 4 | Newest margin, opposite mode, adjacent zoom |
| 5 | Deep history, after 20 seconds of continuous viewing at this view |

Multi-site retains eight source slots. Other-mode warming retains its existing
adjacent-level work after its current-level tiles, within the same reserve.
The `radar_viewed` 15-minute demand hint and runtime `radar_viewing:{since,last}`
continuous-view gate survive. Hidden documents omit the view signal. Off-tab
passes fetch newest only and retain the hour on disk; warm view-start publishes
that retained manifest without changed observation/fetch times. It also resumes
missing opposite-mode warming; already resident rounds need no provider HTTP.

**Readiness discovery (v5.0).** After the staggered boot fetch, a single cancellable
one-shot replaces the old 180-second radar interval. MRMS discovery runs at
`publishedStamp + 120 + RADAR_IEM_READY_LAG_SEC`, then every 20 seconds when the
stamp has not advanced. Site discovery uses `publishedStamp + expectedCadence`:
the median of the last four listed volume gaps, bounded to 300–600 seconds
(default 300), followed by 30-second re-polls. RainViewer uses its 600-second
cadence with 25-second re-polls. Six attempts bound each fast polling window;
without a new stamp discovery backs off to 120 seconds until recovery. A new
published source/site/stamp resets and re-aligns the window; rezooms and warming
of the same stamp do not reset it.

Discovery uses the existing single-flight radar lane and real metadata GETs
(including conditional MRMS responses) or concurrent site listings. An unchanged
complete newest scan refreshes validated intent/prefetch knowledge and ends a
discovery pass before tiles/history/prefetch. A new
stamp goes immediately through normal primary acquisition, v4.7 manifest sliding,
and v4.6 wrap adoption. Existing partial repair and warming retries remain
separate, with their original reserve and tiers. Discovery needs one free request
slot to start; every listing, tile, retry and hedge still pays the shared 240/minute
gate. Open breakers wait for their recovery probe, provider cooldowns and full
budgets postpone wakeups, and a busy lane re-arms after five seconds while warming
yields at its next checkpoint. A coincident repair/history retry becomes the due
discovery pass, consuming that wakeup once. Breakers/cooldowns are scoped to the
active/preferred source chain; an unused fallback cannot force one-second polls.
Stop cancels the readiness event through the normal lifecycle registry. The two-second emit
tick only reports data; it does not schedule discovery.

On a healthy regular MRMS feed within the bounded polling window, the target is
newest pixels on screen within about 30 seconds of fetchability and AS OF age
below `120 + 300 + 30 = 450` seconds, including discovery/acquisition/display.
This is a warm-path target, not a guarantee during provider outages, exhausted
budgets or sustained transport failure. The 80% of `staleSec` suffix threshold
is unchanged. `tests/test_radar_v50.py` uses a deterministic clock;
`tests/verify_radar_v50.py` exercises five scheduled publications with a
300-second lag and publication jitter through real loopback TLS and both browser
themes. Only the inter-poll waits are accelerated; decoding/playback and I/O
latency are measured in real time. A canvas draw audit measures the first visible
new-stamp composite, including the wait through the preserved playback wrap.
The 20-second MRMS re-poll leaves room for acquisition and that wrap.

```
radar/t/<renderRevision>/<sourceId>/<ICAO-or->/<YYYYMMDDHHMM>/<z>/<x>/<y>.png
```

The stamp is the provider's actual UTC scan time. The 12-hex rendering revision
hashes the remap revision, native inverse tables, both ramps and palettes,
wire-metadata revision, and basemap revision. A changed renderer gets
a new URL. The page checks PNG metadata against `tiles.remapRevision`. The site
table has an independent content revision and immutable URL. Unknown revisions
and themes return plain 404, even if an obsolete file remains on disk. `radar_palette.remap` runs on
cache fill, once per tile, after native PNG/size/placeholder validation. Writes
are atomic. `radarRemap` PNG tEXt has exactly `remapped`, `unmatchedColors`,
`opaqueColors`, `unmatchedPixels`, `opaquePixels`, `ambiguousPixels`, `revision`.
A separate `radarVisiblePixels` tEXt integer records nontransparent output pixels
after remapping (native opaque pixels can be below the displayed floor). This
avoids client readback allocation while keeping clear-state decisions exact.
Cache hits decode and verify 256×256 variant dimensions, revision, counts and LUT colours,
including the supplemental output-alpha count.
Invalid entries are removed and rebuilt. Native bytes retain their separate
400-entry LRU and transport lifecycle; no RGBA layer cache replaces it.

The existing HTTP/1.1 static handler serves valid, existing tile and geometry
paths with `Cache-Control: public, max-age=31536000, immutable`. Missing files
return ordinary 404, without immutable caching; there is no 202, long poll or
new dynamic route. The process-owned insertion order drives runtime eviction;
serving a tile does not stat it or update that inventory. Writes pin the exact
current and retained visible eight-frame inventories. Outside that protected set,
oldest inventory entries yield first. Both file and byte instance caps apply. Admission yields if protection leaves
no room: it cannot both exceed the cap and promise retention. The first remapper
revision run removes owned crop and SVG directories and obsolete remapped tiles.

At approximately 8 KiB per tile, one level's 31×35 working set is about 8.7 MB,
and write churn is about 76 GB/year. `WFP_RADAR_DIR` may point to tmpfs beneath
the static root to trade persistent warm starts for no SD writes; this is an
optional deployment choice, not a changed security or preference model.

### Manifest and measurement honesty (P)

```jsonc
"geo": {"version":"<sha256-first-12>", "base":"radar/geo/", "sites":"radar/sites-<siteRevision>.json"},
"tiles": {
  "base":"radar/t/", "revision":"<renderRevision>", "remapRevision":"native-v5.2-1", "source":"iem-mrms-lcref", "site":"-", "z":8,
  "levels":[7,8,9], "grid":{"x0":39,"y0":88,"w":4,"h":3},
  "newest":{"stamp":"202609141733","mask":"fff","expectedMask":"fff","completeMask":"fff"},
  "frames":[{"ts":1789407180,"at":"10:33","stamp":"202609141733",
             "siteScans":[],"levels":{"7":false,"8":true,"9":false}}]
},
"center":{"lat":47.61,"lon":-122.33},
"units":"mi", "rings":[{"meters":40233.6,"label":"25 mi"}],
"scaleChoices":[{"meters":16093.44,"label":"10 mi"}, {"meters":40233.6,"label":"25 mi"}],
"zoomAuto":true, "zoomAutoLevel":8, "zoomMin":4, "zoomMax":9,
"zoomDesired":null, "zoomCapped":false, "zoomSource":"MRMS",
"refresh":{"state":"history","frameIndex":4,"frameTotal":8}
```

`center` is **always the station**. `tiles.z/grid` describes the worker's latest
reported viewport, not a command to move the page. Grid covers the viewport;
the one-tile warming margin is additional. At other levels its geographic bounds
are rescaled into XYZ, rather than reusing Z's integer indices. This resolves the
v4 example's 35-tile grid against its normative ≤35 grid-plus-margin and ≤15
history budgets. `mask` has one meaningful row-major bit per grid position, LSB
at `(x0,y0)`, zero-padded to `ceil(w*h/4)` hex digits. A set bit means an actual
newest tile exists (at least one aligned site for multi-site); padding bits are
zero. `frames[].levels[Z]` is true only when the frame's needed grid tiles at Z
exist. History is oldest first, never padded or fabricated. `siteScans` carries
the actual aligned source timestamps in stacking order.

The existing source, attribution, cadence, stale, observed/fetched time,
partialCoverage, frame count/spacing/gap, site-reporting, source preference and
fallback fields survive. `frameCount` counts candidate slots;
`completeFrameCount` describes completed sets. The page's decoded inventory
(including frames on either side of a gap) reflects decoded coverage at its own current camera, not those server counters.

`frameSpacingSec` is median
completed-set spacing, not a promise of fixed scan cadence. `observedAt` is
scan time, `updatedAt` fetch completion time (retained for telemetry, absent
from the face). A failed fetch changes neither. `ageSec` is scan age;
`stale` starts at the source's `staleSec` (a per-source threshold set above that
source's freshest-possible frame — MRMS is never shown younger than ~5 min
because IEM 503s newer minutes, so a bare 3×cadence = 6 min would flag every
healthy scan; `staleSec` is exposed so the console re-derives it consistently and
a legacy payload falls back to 3×cadence). Header age suffix starts at **80% of
`staleSec`** (3×cadence for a legacy payload), floor-rounded to minutes — so a
routinely-delayed feed reads clean and the suffix forecasts the stale flag. Nominal cadence belongs only in the
source caption; scan age belongs only beside AS OF. Never label data LIVE.

AS OF, ageSec and staleness all describe the newest primary scan. The loop
frame read names the displayed historical scan once the eight-frame inventory
is ready. A tile contributes its own remap counts only when drawn. More than
2% discarded opaque pixels or any cross-stop ambiguity marks `palette incomplete`.
Incomplete acquisition or remapping cannot assert clear conditions.

Missing tiles first use a **same-stamp**, same-source/site-set parent at Z−1 or
Z−2 within the same render revision, nearest-neighbour scaled with Smooth off
(or bilinear with Smooth on). Without that fallback, missing radar tiles draw
nothing. There is no hatch, pattern, coverage tint,
delayed overlay, or partial-coverage aria suffix in either theme. Same-stamp
parent tiles still supply measured echoes while finer tiles are in flight;
other timestamps never fill a missing tile. **Absence of echoes in a region may
mean "not yet loaded".** The loop read and note carry that acquisition state.
The loop read shows `Buffering · N of 8` (or `Paused · N of 8`) until the eight
frames are ready, including when a partially acquired scan already shows echoes.
The note retains `Refreshing · newest frame` / `Refreshing · frame N of M`.

The former acquiring-versus-partial 40% classification is removed. What remains
is copy driven by decoded frame readiness/inventory and the refresh stage
(newest versus history). A tile's `partial` flag still prevents an incomplete
multi-site tile from claiming readiness/clear weather and permits retry; it
never controls decoration. Cold acquisition tiles paint at full opacity on the
next rAF after decode. Temporal playback blending does not apply to arrivals.
This user decision supersedes P4 and the v4.4 hatch rules.

The reporting timeline owner (`siteId`, station-nearest reporting site) remains
the caption subject while its tiles are late (the picker stays on `nexrad.id`):
e.g. `Camano Island radar + 1 nearby · precipitation mode · new scan every ~4 min · IEM / NOAA · KATX loading`. Nearby counts
count actual other contributors; station distance stays with the nearest site.
`KATX loading` replaces `timeline KATX` (v4.4 overrides the exception vocabulary
retained in Fable v4.3 §1.4). A site explicitly marked `not reporting` can still
yield to a drawn contributor, with the existing honest not-reporting suffix.

Retired: public `frames`, frame `id/url/complete`, `latest`, `basemap`,
`geometryOnly`, `marker`, `centered`, `bounds`, `viewport`, `metersPerPixel`,
`scaleBar.pixels`, `rings[].px`, `intent`, `refresh.forSeq/state:superseded`,
`X-Radar-Intent-Seq`, fast poll windows, crop hashes, CSS layer transforms,
preview/commit/gesture fences and `Refreshing · restarted`. Internal worker
completion and cancellation bookkeeping is not a page acknowledgement.

### Local raster geography (v4.1 L′)

The unchanged Natural Earth bundle is the only shipped basemap artifact.
`radar_basemap.tile(theme,z,x,y)` produces opaque 256×256 PNG-8 tiles for z4–10.
URLs are `radar/geo/<version>/<paper|night>/<z>/<x>/<y>.png`. Version is SHA-256
of bundle, exact style table, style revision and renderer revision, first 12 hex.
No geometry binary service, client projection buffers or backing canvas remain.

Paper ground is **#EBE6DB**, including the 3.5% ink plate inset; night ground is
**#0B0D11**. Water is #DFDCD4 / #151B21. Full coast-over-water is #95A3AA /
#445A68; coast-over-land #9BA9AE / #3F525F. Admin0 over land is #9F9B90 /
#686766, admin1 #CFCBC0 / #575756, roads #BAB6AB / #464747. Stroke widths are
1px at the tile's zoom. Admin1 starts at z5, major highways z6, secondary roads
z8. Admin1 dashes are 4/3. Degree-cell ocean runs and stitched line fragments
are retained; Douglas–Peucker tolerance is always 0.5 tile pixels.

Compound ocean/lake fills use one aliased even-odd scanline pass at 1×.
Each stroke uses a reused 1024×1024 L coverage buffer and `Image.reduce(4)`.
Later stroke coverage replaces previous strokes against the original backdrop.
The two backdrops and five stroke layers × two backdrops × sixteen coverages
form a fixed 162-entry palette, with no dither, alpha or tRNS chunk.

**v4.2 scheduling:** geography has its own single-flight `geo` worker, scheduled
every 250ms from engine startup, independently of radar fetches, view markers
and live viewing sessions. Each invocation renders at most one missing tile;
250ms is an admission quantum, not a render deadline. A slow tile completes
before the next geo invocation, without holding the radar result or transport
locks. Background work sleeps another 50ms after a tile when there is no fresh
viewed viewport. An in-progress tile cannot be interrupted.

The home 7×5 block is pinned at every zoom in both themes (490 tiles away from
the polar tile-row limits). Order is home zoom's central 5×3, its margin, z−1,
z+1, z4, remaining zooms by distance; then the other theme in the same order.
The initial theme defaults to paper without an activity report. The queue
retains progress and idles when complete; station or basemap revision changes
rebuild it, reusing any existing immutable tiles. An engine restart checks the
same disk set and does not rerender existing tiles.

Accepted ordered panel `radar_activity` atomically carries
`{at, theme, moving, center, zoom}`, independently of camera ownership. The
displayed camera (`radarGeoCenter`, `radarGeoZoom` on the ordinary poll) becomes
live zoom/centre intent only through an accepted settled camera commit. Moving
reports affect geography priority only. The radar worker watches the canonical
runtime intent; durable zoom follows and never overrides it.
With a viewed marker and activity age **0–5s**, missing tiles for that settled
viewport and its margin precede the remaining home queue, in the reported theme.
The freshness check applies only to viewport priority. Missing, malformed or
stale activity never gates home warming, except that a report of `moving=true`
suppresses both queues until a settled report or marker removal. No request is
added during gestures: existing polls remain suppressed while moving, so the
engine cannot know about unreported motion and can only yield at tile boundaries.
Home and viewport PNGs, and the served geography revision marker, use a unique
same-directory temporary file followed by atomic replacement. Geography has an
independent **32,000,000-byte / 6,000-file** served-atime
LRU. Pruning removes empty directories and never evicts home tiles. Radar cache
pressure cannot evict geography. Routine tile access/miss logs are suppressed.

`tools/benchmark_radar_kiosk.py` prints `summary` first: cached `firstPaintMs`,
pan/pinch `{frames, maxDrawMs, p95DrawMs, maxDrawImages, serverRequests}`,
`memoryPeakMiB`, `geoTilesOnDisk`, `radarTilesOnDisk`, and `fences`. CDP network
events exclude browser-cache/service-worker responses from server request counts;
each gesture includes its settling interval. Disk counts cover all revisions
under `--radar-dir` (default `WFP_RADAR_DIR` or `~/almanac_web/radar`), and are
null if that root is absent. Raw frames/fetches/decode/GPU details require
`--verbose`. These graphics counters do not measure Chromium process RSS.

Every opaque base-canvas paint is: baked ground → whole-plate graticule →
same-version/same-theme z−2 ancestors → z−1 ancestors → exact tiles. Ancestors
are skipped when all exact tiles are resident. Thus uncovered regions retain
graticule rather than silently reading as land or sea. No hatch or note denotes
missing geography. The graticule uses `--rule-faint` and the largest step from
5°, 2°, 1°, .5°, .25°, .125° with local parallel spacing ≤220px. At extreme
latitudes where .125° exceeds that bound, it is halved until the bound holds;
meridian/parallel steps are also widened independently when needed to retain
the 18/7 segment limits. This explicitly corrects the delta's incompatible
finite-step/segment assumptions at polar and equatorial edges; spacing remains
below 220px. The Seattle table is unchanged.

The page retains ≤36 basemap bitmaps across tab switches. Theme changes evict
that LRU and change the path prefix. Base tiles use bilinear sampling when
scaled and disable smoothing at native scale; echoes always disable smoothing.
Neither layer uses a CSS filter. Missing tiles retry no faster than every 2s.
Only missing visible coverage requests ancestors; resident margins do not churn
against unnecessary ancestor prefetch. Cached activation targets <100ms to an
actual geography-tile paint, not merely a ground/graticule paint.

### Rendering, graphics memory and gestures (M′/Q)

`#rad-base` and `#rad-echo` are 956×490. Fractional zoom uses the floor native
level until the integer snap, bounding each exact layer to fifteen draws even
at half zoom. Echoes and geography share unwrapped placement math; only cache
keys and URLs wrap x. The manifest rectangle and row-major masks are unwrapped.
Site `expectedMask` denotes geographic intersection; `mask` means any acquired
contribution and `completeMask` means all required site contributions acquired.
Outside-range tiles do not block playback, and partial site tiles cannot claim
complete acquisition or clear conditions.

Steady playback uses up to eight 956×490 bitmaps (including newest) plus one reusable
plate scratch, within the same 40MiB admission cap. Newest tiles remain available
for acquisition and camera moves. A moving history bitmap exclusively owns its projected rectangle;
component tiles are clipped outside that rectangle, so alpha/reflectivity never
double-composites. Its hasEcho/legend metadata remain attached to its pixels.
Settle/cancellation snaps and clamps the camera and reports one settled intent.
A geometry change retains the playing composites and their frame deadline while
acquiring replacement history. Due loop paints also run during camera movement;
retained composites use the same camera reprojection as gesture previews.
History work runs only on otherwise unpainted idle frames, after playback. Draw-count fences are ≤32 steady,
≤56 degraded. Wall-time target is 4ms on the actual panel, requiring CDP evidence.

One rAF loop targets 30fps. Events mutate camera state; drawing is coalesced.
Echo arrivals repaint the echo plate; geography arrivals damage only their
rectangles. Polls do not rerender geography.
Overlay groups `rad-geo` and `rad-chrome` persist; gesture updates change
attributes only. Ring labels use one translation attribute apiece (site labels
retain x/y), bounding four sites plus two rings to twelve writes. Site arcs are geodesic circles sampled once at 120 points per
site/session. The scale's chosen distance freezes during gestures; its length
still follows `156543.034*cos(lat)/2^zoom`, and settle chooses the largest distance
≤25% of the short axis. Theme changes repaint geography, not radar data.

The graphics budget admits storage **before allocation**, with a shared 40 MiB
cap across echo and geography jobs. Retained canvases are 3×1,873,760 bytes,
window composites normally ≤8×1,873,760, echo LRU ≤40 entries (262,144 bytes
for either preference) and basemap LRU
≤36×262,144. During geometry or Smooth-preference acquisition the eight-frame cycle can coexist with four incoming
plates (any separately held aged-out subject stays accounted too); incoming work pauses at four until wrap adoption. Four shared
fetch slots each reserve 786,432 bytes for either radar variant or geography; native image decodes are serialized and
admitted at most once per animation frame, after loop paints. The compositor
also completes at most one plate per animation frame, newest first. The shared
merge scratch remains 256×256 (262,144 bytes), including with Smooth on.
Simultaneous nominal maxima require eviction; the cap is unchanged.
History transfers reserve a plate before allocation; if eviction cannot make
room, admission blocks. Merging has no getImageData/readback buffer. Compressed
responses are bounded at 128 KiB for both radar variants and geography;
oversize tiles follow the unavailable path. Visible geography is protected before
evicting echo tiles under pressure. Smooth introduces no larger allocation.
A job transfers 262,144 bytes of its existing reservation to a retained tile;
its `finally` returns the remainder on success, 404, deadline, cancellation,
decode failure or a cap exception. Busy/pending ownership is cleared before
memory diagnostics, so a diagnostic throw cannot strand a slot. Admission also
rolls back its reservation if its own post-admission diagnostic throws.

A refused tile pump records the memory ledger at refusal and performs no further
admission iterations while that ledger is unchanged. Releases, evictions and
frame retirement let the next frame or queue event retry; a manifest poll also
rearms it. Rebuilding a queue or advancing animation frames alone does not retry.
This bounds acquisition work, while the animation clock continues playing.
This accounting covers owned graphics and bounded decode working storage,
not Chromium process RSS, driver internals or its HTTP cache. The harness also
tracks bitmap creation/close and canvas dimensions independently.

Gesture state is `idle | gesturing | inertia`. Pan follows the pointer 1:1;
polar edges and 1.5 viewport-diagonal station radius resist at .3 and clamp on
release. Inertia uses release-time samples from the last 60ms (a stationary hold cannot fling), capped at 2400px/s,
exponential τ325ms, stopping below 8px/s or at the clamp (≤780px). Reduced motion
has no inertia. Pinch preserves the geographic point under a translating midpoint.
Release snaps to the nearest integer in 160ms about the final focal point;
reduced motion snaps immediately. The zoom read shows the eventual integer.
Wheel/double tap use ±1/+1 about the input point; steppers zoom about the centre.
Recenter and panel-only 90-second idle recenter ease to the station over 280ms. Only the
plate uses touch-action:none; controls remain target-gated, with ≥44px hits.

The only motion is 160ms zoom, 280ms recenter, inertia, 120ms note opacity,
120ms temporal scan crossfade, tick movement and 120ms Play press dip. Reduced
motion removes blending and incidental animation, retaining its opt-in single
sweep with hard cuts. No tile fade, shimmer, skeleton or loading pulse is introduced.

A cold provider outage with a valid station keeps Radar available. The local map
and graticule remain while measurement inventory is empty; no observed time or
clear conditions are invented. Cold activation uses durable manual/auto zoom,
clamps to the camera range 4–10, retains that camera through provider tile caps, and
reports its initial camera. AS OF ordering is scoped to source/station/primary
identity. Newer accepted transitions invalidate old pending source generations.
Failed viewport reports remain pending until delivered; only a newer settled
camera supersedes them. First tab entry skips acquisition only after the full
required eight-frame inventory has been checked against the validated memory index. Publishable partial
frames are distinct from complete sets in completion counters and retry work.

### Controller reports and the single note

The page sends the v5.7 session/generation transaction and independent heartbeat
ordinal described above, using `radarGeoZoom`, `radarGeoCenter`, `radarSource`
and `radarPolicy` on the existing controller poll. Runtime intent wins over durable
defaults; reload reconciles ownership first. The resolved numeric camera drives
acquisition and Auto remains an independently durable policy.

Activation reports visibility without committing or claiming a camera. User
camera commits follow settle's 120ms trailing debounce.
**v4.3 source input** renders intent synchronously on primary pointer contact;
native click also supports mouse, keyboard and assistive activation. The next
event-loop task sends the complete intent on the existing controller `wx.json` GET
channel, coalescing a synchronous burst and aborting an obsolete in-flight poll.
The pointer's compatibility click does not send a duplicate transaction. Source
input below the site floor snaps the camera to zoom 7 before sending.

Ordinary polling remains 2s and continues reporting motion during gestures/inertia. A source
choice polls every 300ms until the payload acknowledges its source preference,
for at most 20s; hidden/inactive Radar never gets accelerated polling. Pending
presentation lasts until that source's tiles land, even after fast polling ends.
New plate activity cancels an unposted camera report. An in-flight tile is
allowed to finish into the LRU; the pending queue is rebuilt for the new camera.
Four concurrent, cancellable page tile jobs leave room for wx.json. Visible
newest/four/eight-frame demand precedes margin and adjacent warming. During gestures only newly
visible demand is added. Missing tiles back off at least two seconds and the
manifest mask suppresses requests for known unavailable newest positions.

The only status corner is `#rad-note`, a fixed 14px box at top418/right12 with
`role=status`, `aria-live=polite`, `pointer-events:none`. For 600ms after a local
report it stays suppressed. Priority is refused-source copy (v4.3b), newest/history refresh, failure naming
the visible scan, upper zoom-cap copy, then `KATX resumes at zoom 7`. Copy remains
`Refreshing · newest frame`, `Refreshing · frame 4 of 8`, and
`Couldn't refresh · showing 17:12`. Failure copy names the retained scan, or an em dash before any measurement.
The 20-second retry state takes priority. Both the loop read and AS OF name the
actually painted scan, never fetch completion time. Only a 120ms opacity transition is used,
disabled for reduced motion. There is no Updating pill, spinner, second note,
or `aria-busy` write on the interactive plate.

### Immediate source choice (v4.3)

`#rad-src` and the requested segment carry `data-state="pending"`; the group
carries `aria-busy="true"`. The requested segment has a static dotted underline,
while `aria-pressed` identifies Auto when requested, otherwise the displayed source. The caption
synchronously names the requested choice and retained displayed source in the
input frame. Only the corner note has a 600ms grace. A matching inventory starts
acquisition; four decoded target frames replace pending. Failed attempts retain
the preference and enter visible retry by the 20-second deadline. Repeated
payloads retain in-flight work; a newer generation cancels obsolete jobs.
No new animation is introduced, including under reduced motion.

The emitter's existing idle tier writes both native LRU bytes and the immutable
remapped disk paths used by `serve.py` and the v4 page. Opposite-mode newest at
the current camera takes precedence over optional zoom neighbours when eligible.
Manual Site warms Region, and manual Region warms Site. Auto warms the next
source at the settled camera: Region-to-Site at zoom >= 7, Site-to-Region at
zoom <= 7. Site warming uses native; newest-only warming is restricted to the primary
site’s newest scan. Only an outage or paused ceiling uses IEM fallback tiles. Source cooldowns cannot block
another source's eligible round; the shared 240/minute
cap and footprint/layer-aware mandatory reserve apply (v5.7 replaces the fixed
34-request reserve). A round is
remembered only after all its tiles succeed; indexed evictions or reported disk
damage invalidate the completion shortcut. Interrupted rounds resume using paid-for native/disk tiles.
A warm eight-frame tab return publishes immediately and resumes missing
opposite-mode work on the next 100ms watcher tick in the same radar flight lane. Expired
current-source metadata prevents warming its own neighbours, but does not prevent
discovery of the opposite source. Warm intent passes reuse fresh listings; no
listing request is needed until their existing cadence expires. Cold passes
retain the displayed-source caption plus `· switching` after the grace while discovery/acquisition runs. First-tile
publication orders timestamps within source/primary identity, so a site volume
older than the displayed mosaic can still publish immediately.

### Smooth preference (v6.1; persistence introduced in v5.6)

The quiet **SMOOTH** button beside zoom reset uses the existing control colours,
`aria-pressed` and a 64×44px target. Default is **off**. Any controller with a
valid page session sends `radarSmooth=on|off` on an explicit tap; exactly one
value is accepted, independently of camera ownership. Duplicate, empty and
invalid values and non-controller callers cannot write it. The handler atomically replaces `radar_smooth`
only when its value changes, following the durable symlink just like zoom.
The launcher backs it with `$XDG_STATE_HOME/wfpiconsole/radar_smooth` (default
`~/.local/state/wfpiconsole/radar_smooth`), preserving it across tmpfs recreation.
`X-Radar-Smooth: on|off` acknowledges the stored preference on controller responses,
including reloads while the emitter is still acquiring the selected variant.
The emitter watches this marker even when an ordered `radar_intent` exists;
changed preference supersedes in-flight work at the existing tile checkpoints.

**Interpolation is in reflectivity, not colour.** Smooth converts each native
256×256 field to an engine-only 512×512 intermediate using pixel-centred bilinear weights in linear half-dBZ
index space: N0B `i/2−33`, MRMS `i/2−32`. Verified palettes retain numeric indices;
RGBA/RainViewer use the existing native inverse and ambiguity diagnostics.
Reserved, unknown, zero-alpha, below-floor and explicitly suppressed gates have
zero weight. The interpolated weighted index is divided by interpolated valid
coverage, so a missing gate cannot supply intensity or pull it toward zero.
Before that division, a BOX reduction averages each 2×2 group of weighted field
and coverage back to 256×256. It never averages RGB or quantized legend colours.
At an interior output gate the combined separable weights are 1/8, 3/4, 1/8;
tile-edge weights clamp to 7/8, 1/8. A missing output gate between two valid
neighbours can average their covered subpixels, with 1/4 coverage; it does not
claim a full-coverage measurement there. A zero-support output stays transparent.
Coverage interpolates separately; the chosen
legend alpha multiplies coverage, rounded once. Tile edges clamp to the native
edge; no neighbouring tile or extra fetch is required. This is a 2× bilinear
field resample followed by area reduction, not a Gaussian convolution.

The interpolated value is quantized to the existing discrete legend LUT. Every
nontransparent **tile RGB** is a LUT entry; fractional edge alpha is coverage,
not another intensity. The site clear-air floor and slate alpha remain 5 dBZ
and 180; MRMS/RainViewer stay at 10 dBZ. The page enables bilinear echo scaling
for Smooth frames, including fractional zoom and retained plate reprojection;
off retains nearest-neighbour scaling. Browser scaling, alpha composition and
temporal frame blends can mix display colours; their pixels are not additional
dBZ samples. Geography colours, geometry, legend and camera policy are unchanged.

On a 1024×600 panel the radar canvas is 956×490 physical pixels. Native
256px tiles display 1:1 at each integer camera zoom within the provider's range.
Smooth therefore softens gate edges at zoom 7; it adds no measured resolution.
The button explanation says this explicitly. The same limitation applies at
other integer zooms; enlarged capped-source views spread those softened edges:

| Camera zoom | Region / IEM MRMS (cap 9) | Site / N0B (7–10) | Worldwide / RainViewer (cap 7) |
|---|---|---|---|
| 4, 5, 6 | 256px / 1:1 | Region fallback | 256px / 1:1 |
| 7 | 256px / 1:1: softened gate edges | 256px / 1:1 | 256px / 1:1 |
| 8 | 256px / 1:1 | 256px / 1:1 | 512px / 2:1 |
| 9 | 256px / 1:1 | 256px / 1:1 | 1024px / 4:1 |
| 10 | 512px / 2:1 | 256px / 1:1 | 2048px / 8:1 |

Widths above are displayed tile widths; emitted tiles always remain 256px.
Fractional zoom scales by `2**(cameraZoom-nativeLevel)`. A temporary same-stamp
ancestor can enlarge further. Browser scaling does not create new measurements.

Both preference directions use the v5.9 retained-window handoff. Publication of
a new render revision aborts superseded tile jobs and advances the echo epoch;
it retains outgoing cycle plates, subject and playback deadline. Four incoming
decoded frames permit adoption at the existing wrap. Before four, every playing
plate survives; afterward only already-passed outgoing prefixes may retire.
The incoming `loaded` list counts the new variant; outgoing owned plates are in
`retired`/`cycle` and must be included when auditing retained memory. At adoption
the playing count may become four and grow to eight, just as with a zoom step.
The v6.0 copy is shared: “Playing previous view · sharpening N of M.” The count
is incoming decoded plates, never outgoing ones. Rapid reversals also retain the
currently playing cycle and fence cancelled job completions by epoch/revision.

`radar.smooth` and `radar.tiles.smooth` describe the published variant;
`radar.tiles.tileSize` is always 256. `tiles.revision` has a distinct Smooth
hash and `tiles.remapRevision` is `field-bilinear-2x-box-256-v61-1` when on. Frames retain
that identity through asynchronous decode, source switches and retained playback.
The server admits the two installed immutable revision trees, via
`.tile-revision` and `.smooth-revision`. Both share **one disk-sized inventory and
eviction budget** (8,000–12,000 files / 64–256 million bytes), with a variant
suffix on Smooth keys. Startup admits both trees in one global age order and
within one shared entry budget. Native byte LRU
identity is unchanged: toggling can remap cached native bytes without a provider
request, and returning to an existing variant can reuse its rendered PNGs.
Remap diagnostics count native pixels (≤65,536); `radarVisiblePixels` counts
output pixels (≤65,536 for either variant), including interpolated coverage edges.

Prewarm, prefetch and acquisition tiers, request limits and deadlines are unchanged.
`tools/benchmark_radar_smooth.py` measures offline transform and decode/encode CPU
cost on deterministic native P/RGBA storm fields. `tests/test_radar_v56.py` checks
field/LUT/boundary invariants, durable loopback writes, variant reuse and restart;
`tests/verify_radar_v56.py` checks real loopback PNG acquisition, both themes,
reload persistence, fractional scaling flags, unchanged geography and independent
bitmap/canvas accounting under 40 MiB. `tests/test_radar_v61.py` drives four
concurrent jobs through every cleanup outcome with explicit barriers and a
manual deadline clock. `tests/verify_radar_v61.py` checks both preference
handoffs in both themes, retained plate ownership, the 600-frame admission
refusal fence, release/poll retries, and before/after memory tables (`--baseline`).

### v2 mosaic and clutter control (2026-09-25)

**Inputs and time.** The existing primary radar clocks the loop, including
single-site views. Native loops contain at most eight frames. For each frame,
a reporting neighbour contributes its newest scan with `ts <= frame.ts + 60`
and `frame.ts - ts <= 480` seconds. These `ts` values retain IEM's minute scan
clock; each decoded product also records its exact `volumeTs` from NOAA.
A missing, stale or invalid N0B leaves that site out of the frame. No temporal
interpolation or MRMS mask is applied. Auto's source, coverage and attention
policy is unchanged; its v2 site renderer now uses this mosaic.

**Classification.** Product 165/N0H has 360 approximately one-degree radials,
1200 250 m gates (300 km) and a separate 0.1-degree bearing lookup. Its decoder
shares bounded header, compression, packet, position, coverage and volume-time
validation with N0B but validates its own categorical description layout.
The product's volume second must equal N0B's exactly. HCA is sampled at N0B
ray centres using actual bearing tables, never `row // 2`. Class 20 (ground
clutter/AP) and 150 (range folded) become no data. Class 10 (biological) becomes
below threshold unless more than half of its 5-radial by 9-gate N0H
neighbourhood is precipitation (classes 30–120), in which case it keeps N0B.
Azimuth wraps across north; range does not wrap. Missing/outside neighbours
count as non-precipitation in the fixed 45-cell window. BI cannot turn an N0B
range-folded gate into a valid measurement. All other classes, including 0 and 140, keep N0B.
Outside HCA range or missing HCA bearings, N0B is retained. The existing N0B
15 dBZ floor and despeckle remain. Missing/invalid HCA retains that site's N0B
and marks it unfiltered. Sites acquire N0B concurrently and each starts N0H
as soon as its N0B volume is known. The frame waits at most 2.5 seconds after
its N0B inputs are ready for all classification together, reserving two seconds
of the frame deadline for rendering. It uses whichever classifications arrived;
a late classification can upgrade the key. In-flight N0H requests keep their
bounded transport deadlines and populate the scan cache without holding the
render; the shared wait does not become another timeout per site.

**Per-pixel ownership.** `radar_mosaic.mosaic_codes` orders candidates by beam
centre height using the product's antenna height/elevation and 4/3-earth
geometry. Blockage occurs along each radar's beam path. Only candidates inside
the 230 km disc, product gate range and radial coverage qualify. Among valid
gates in height order, the lowest at or above the 15 dBZ display floor wins.
If none reaches the floor, the pixel is clear and nothing is drawn. N0B code 1
and QC no data (GC/RF) fall through. The design rationale is:
a lower beam's clear return can fall through to a higher **filtered** echo.
A lower filtered candidate that measured clear or below floor excludes higher
**unfiltered** candidates at that pixel. Missing bearings, out-of-range gates,
and code 1 are not clear measurements and do not impose that exclusion. A lower
unfiltered echo can still own a pixel; any unfiltered site drawing pixels keeps
the unfiltered caption (contributor metadata conservatively labels the frame).

A process-wide 48 MiB byte-bounded LRU caches each site's tile geometry by site
position, antenna height, elevation, z/x/y, sample size and range limit. A
spherical disc/rectangle intersection rejects off-tile sites before projection;
these sites never enter the cache. Each projection owns only the rectangular
sampled footprint of its 230 km disc in the tile. uint16 bearing bins (0–3599)
and gates (0–32767), the smallest integer dtypes that hold those domains, plus
float32 beam heights cost eight bytes per retained sample. Cropped arrays own
their storage, with no full-tile backing arrays. Bearing bins are translated
through each volume's actual radial table; radial rows are never reused across
volumes. Gate boundaries are computed in float64 before integer compaction.
Only foreground viewport-grid tiles at the current camera zoom admit or promote
entries. Margin builds and adjacent-zoom prefetch may read existing projections
but compute misses without insertion or LRU promotion. An eight-frame four-site
956×490 Seattle viewport loop, with margin-1 and z±1 warm rounds between frames,
has 87.5% foreground hits including the cold first frame at z7–10. Retained
projection bytes are respectively 34,131,024 / 19,467,888 / 23,231,248 / 25,165,824.

Render allocation is charged from sample size `S` and candidate count `C`:
`1 MiB + S² × max(96 + 12C, 16 + 26C)` bytes. The first term bounds a cold
projection's float64 temporaries plus previous projections/samples; the second
bounds ownership sorting, masks and indices. It includes cold projections even
when they become retained cache entries. The public renderer accepts at most
four candidates and at most 512×512 samples. Its worst-case charge is
38,797,312 bytes, so slot count is `min(cpu_count, floor(80,000,000 / 38,797,312))`,
at least one (two on a multicore host; total charged transient 77,594,624 bytes).
Cold measured peaks on the local arm64 Mac are 26.62 MB at z7 and 6.90 MB at
z8–10, including newly retained geometry, with Pillow imported before measuring.
The separate retained geometry cap remains 48 MiB. Waiting for a slot observes
the tile deadline. `tools/benchmark_radar_mosaic.py --viewport --background
--zooms 7 8 9 10 --memory` reproduces the workload from local N0B/N0H files.
z7 uses 2×2 maximum supersampling after selection; z8–10 use one sample.
The existing source palette code-to-slot table and PNG `weatherPixels` metadata
are unchanged. Attention consumes the mosaic counts; the sentinel remains MRMS.

**Immutable identity and storage.** Native frame metadata adds `mosaicKey`,
`unfilteredSites`, and `siteScans[].volumeTs` / `.filtered`. Contributor metadata
is distinct from tile layers. The URL is
`radar/t/<native-revision>/iem-nexrad-n0b/M<24-lowercase-hex>/<YYYYMMDDHHMM>/<z>/<x>/<y>.png`.
The 96-bit SHA-256 prefix hashes the render revision and sorted
`(site, exact volume second, has N0H)` tuples (the full native tile render revision participates, including palette identity). Changing a contributor, scan or
QC availability produces a new key, including on repair. A restart reuses the
identity of cached inputs.
A published key never changes meaning. Old keyed tiles remain valid until
ordinary eviction. Decoder/QC/selection changes bump `NATIVE_REVISION`; the
existing migration removes obsolete revision roots and updates `.native-revision`.
The server accepts mosaic segments only for NEXRAD under the native revision.
Inventory scans, disk keys, bad-tile reports, manifests and pinning use the
mosaic segment as the storage site, and frame time as the stamp. All viewport
tiles are expected, including transparent tiles outside radar coverage.
A small `frame.json` beside each frame's zoom directories records the frame
stamp, requested contributor stamps, mosaic key, exact per-site volume seconds
and filtering state. It is atomically replaced and synced under the native
revision directory. Reads validate its revision, stamps, contributor types and
recomputed key; only validated, complete tile inventory can bypass acquisition.
Sidecar paths are indexed during the inventory scan and maintained on writes and
last-tile pruning; reads never glob mosaic directories. Identical sidecars skip
replacement and fsync. A cached-path tile evicted before its batch fails closed:
zero-scan rendering is forbidden, and the incomplete frame/round acquires inputs
on the next pass instead of storing a blank tile under an existing key.
A fully cached loop, including adjacent-zoom warming, makes no Level III requests
after restart. Sidecars survive boot with validated tiles and are removed when
the last tile of their frame is evicted. Admission prices missing mosaic inputs,
using mosaic storage identity rather than real-site tile paths.

**Page and health.** A native mosaic frame fetches and draws one layer per tile;
v1 retains farthest-to-nearest site stacking. The mosaic key participates in
page frame identity, staging, tile readiness and bitmap reuse. The newest
frame's `unfilteredSites` appends `· unfiltered` to the caption, or names the
sites if only some lack HCA. `/health.radar.mosaic` carries the newest key and
unfiltered sites; payload `sites[].filtered` reports newest contributor status.

**Acquisition cost.** Separate product-specific hourly prefixes keep S3 listings
bounded: the date follows the product name, so `{SITE}_N` would list unrelated
products across all hours. A cold site-volume costs two listings and two
products; listings are reused by hour and decoded products by volume. The listing
cap is four sites times two products times four hours (32), providing twice the
hour-boundary demand. Eviction removes the oldest hour first, then the least
recent listing in that hour, preserving current-hour entries. N0H adds
about 25 KB, using the same Level III transport, single-flight, negative cache,
rate admission and byte ledger as N0B. HCA failures retry after 20 s (10 s for
transport errors), allowing late HCA to publish a new key for any retained loop
frame within 180 seconds of that site's volume. Negative results are bounded
per volume and stop being retried after this window; an expired missing HCA
no longer disables unchanged discovery. Requested reflectivity pairs that did
not contribute follow the same upgrade-window and negative-memory rules; they
cannot make a partial sidecar permanently complete. Restarts detect these gaps
from requestedPairs versus siteScans and upgrades get a new immutable key.
The emitter owns long-lived bounded N0B and HCA executors, each with eight
running-plus-queued admissions and at most eight worker threads. A frame uses
at most four per product, leaving a full frame's spare capacity when the previous
frame's transports stall. HCA cannot occupy N0B workers. If two whole frames
remain stalled, further admission fails promptly instead of growing threads or
queues; foreground reflectivity can still publish without HCA. Cancellation is
per frame and releases a queued admission only when its wrapper is consumed;
late N0B completion cannot start HCA after that frame is cancelled. `stop()`
shuts down both executors without waiting for transport I/O; `start()` creates
the next lifecycle's executors. Timed-out and cancelled flights enter bounded per-volume negative
memory; late successful products still clear that failure and can upgrade. Prefetch propagates HCA budget refusals
and skips only the current target when classification is unfinished, continuing
later targets (including Region). A budget denial may end the round. No unfinished
round is recorded as successfully prefetched. Watch requests N0H only for the primary newest N0B. Rest/dormant and paused
native request no HCA; newest-only permits no native history. Optional HCA has its own circuit/cooldown state, published at
`/health.radar.classification`, so its failures cannot open N0B's circuit.
Neither a failed HCA nor its failure cache blocks N0B. An unfiltered newest
frame less than three minutes old schedules a 20-second readiness retry.

### v2: the radar's own cells (Level III, 2026-09-25)

**v1** is only the automatic fallback described above: IEM ridge tiles, gridded by IEM to about 1 km before
we remap them. **v2** mosaics contributing NEXRAD sites from NOAA's Level III base
reflectivity (N0B, product 153: 720 radials of 0.5 degrees, 1840 gates of 250 m),
read from the public bucket `https://unidata-nexrad-level3.s3.amazonaws.com/`.
Design and the later phases: `RADAR-NATIVE-DESIGNS.md`.

**One site renderer.** Every device, including BCM2837/Pi 3 boards, uses v2.
There is no renderer control, poll parameter, response header, or renderer
preference. Old `radar_render` files (including malformed or unreadable files)
are ignored and no longer linked by the launcher. The watched preferences are
`radar_intent` and `radar_smooth`, plus legacy `radar_zoom`, `radar_source`, and
`radar_center` when there is no ordered intent. Region retains MRMS. Smooth
remains available for Region and automatic IEM fallback and is disabled while
native frames draw.

**Render variant.** v2 is a third tile variant beside plain and Smooth:
`_radar_variant(ctx, source)` is `'native'` for `iem-nexrad-n0b` when Level III
is reachable, effective attention is live/warm/watch, and the ceiling is not paused.
Otherwise it is the Smooth boolean. The variant has its own render revision directory
(`_radar_render_revision('native')`, advertised in `radar/.native-revision` so
the server serves it immutable), a disk key suffixed `('native',)`, and PNG
metadata with `revision` = `level3-n0b-n0h-mosaic-v5` (`radar_level3.NATIVE_REVISION`).
Gates are measurements, not matched colours, so `unmatchedPixels` and
`ambiguousPixels` are 0 and `remapped` is true. The manifest adds
`tiles.variant` (`false`, `true` or `"native"`); `tiles.smooth` stays a boolean,
true only for Smooth, so the page never interpolates v2 pixels. The payload adds
`radar.native` (the drawn variant). `radar.nativeFallback` contains `active`
(true only when the published site frames are IEM tiles), `reason`
(`level3-unreachable`, `daily-limit`, or null), and `recovering` (true when IEM
frames remain displayed after the cause clears). There is no `renderPref`.
The page uses the drawn/staged manifest for captions, retaining the old loop
until the replacement newest frame decodes. Native attribution reads `NOAA Level III`.
`/health.radar.nativeFallback` retains the host diagnostics (`active`,
`breakerOpen`, `reason`, `since`, `retrySec`); it is separate from the drawn state.

**Attention and daily bytes.** Watch keeps exactly one site and one newest
Level III scan warm: the primary radar, with its matching optional N0H. Any
unviewed site-loop build follows the same bound, even in warm/live. Discovery
lists only that primary site, never other sites; no older scan is downloaded
if the newest is unpublished. Rest and dormant fetch no Site tiles or Level III
products. Rest keeps its four-tile zoom-5 MRMS sentinel at home every hour by
day and every two hours at night; dormant only lists.
Shadow tiers do not apply acquisition restrictions; the effective tier remains
live, while an unviewed build still has the primary/newest bound. Live viewing
expands to the full mosaic loop, reusing the warmed primary product even when
the scan timestamp has not changed. All Level III bodies use the same ledger;
newest-only stays native and paused uses automatic IEM fallback. At a rainy-day
5–6 minute cadence, 0.25–0.33 MB N0B plus 0.025 MB N0H is about 2.75–4.26 MB/hour
(10–12 scans); listings/retries add overhead.

`lib/radar_native_budget.py` counts received Level III response-body bytes,
including listings, products, invalid bodies and partial reads. Cache hits and
304 responses add no body bytes. Counts use UTC days and persist by atomic
replacement through the `radar_native_bytes.json` runtime symlink into
`$XDG_STATE_HOME/wfpiconsole/` (default `~/.local/state/wfpiconsole/`). Counts
survive engine restarts and reboots. A stored UTC day up to one day ahead is
retained to tolerate a small backward clock correction. A day more than one day
ahead is invalid and resets to zero on today's UTC day; normal rollover uses a
later day. Accounting holds a short memory lock. A single asynchronous writer
per ledger performs atomic replacement, file fsync and directory fsync outside
the renderer/accounting locks; request workers and pass completion use
`persist(wait=False)` and never wait for ledger storage. Threshold
crossings and day changes wake it immediately, bypassing ordinary coalescing.
Other changes flush at most once per two seconds, including a trailing burst
with no later request or watcher tick. With healthy storage, SIGTERM/SIGKILL or
a power loss can lose at most approximately two seconds of counts, plus any
in-progress filesystem flush; durability never depends on a clean exit or
atexit. An unavailable/stalled filesystem cannot satisfy that bound.
A write failure logs once per failure streak and the writer retries autonomously
(with watcher ticks also able to wake it) with exponential backoff
(5, 10, 20, 40, 80, 160, then at most 300 seconds). Success clears the failure.
Native remains allowed while persistence is unavailable, but every body byte
still counts in memory and both byte ceilings still apply. A restart during an
unpersisted interval can lose those bytes. Ledger health is independent of the
daily ceiling; failure never masquerades as daily-limit exhaustion or replaces
the transport result/metrics.
This is a response-body budget, not an ISP traffic counter. A request already
in flight may cross a threshold.

Above 150,000,000 bytes in a UTC day, native acquisition is newest-only, with no
native history backfill. Optional Site warming is primary/newest-only. Up to two older scans may be tried
if the newest scan is unavailable, still building just one frame. Region keeps
its normal loop. Above 250,000,000 bytes, Site uses
labelled IEM fallback for the rest of the UTC day. Policy changes
supersede a Site acquisition as a whole, so a tile key never changes renderer;
Site-only tier/ceiling transitions do not invalidate Region acquisitions.
In attention shadow mode, tiers gate neither native access nor Auto evaluation.
The intent watcher also detects UTC rollover and resumes the eligible variant.

`/health.radar.native` and `radar.nativeBudget` contain `day` (UTC `YYYY-MM-DD`),
`bytesToday`, `ceilingState` (`normal`, `newest-only`, or `paused`), and
`ledgerState` (`ok` or `retrying`). The page shows
`v2 accounting retrying · bytes counted in memory` for ledger failure and
`v2 paused · daily data limit` only when the byte ceiling is paused. N0H shares this ledger and both ceiling policies.

**Outages and logging.** A failed Level III transport attempt selects automatic
IEM fallback for 120 seconds. An already open Level III breaker also selects
IEM until a recovery probe is eligible. With a whole-network outage, a
v2 attempt takes the Level III path once per 120-second fallback window, resets
the local failure streak and schedules a retry in 2 seconds. Subsequent IEM
attempts follow the ordinary local exponential backoff. At expiry the next
eligible native attempt can reset the streak again; this bounded exception is
not a claim that a dead local network leaves IEM reachable. Discovery itself
may fail first, in which case its normal local backoff applies without trying
Level III. Existing pixels remain while both routes are unavailable.
All per-site input failures remain counted in `/health.radar.mosaic.siteFailures`.
Warnings are limited to transport/local/ambiguous/circuit failures, or a Level
III scan still unpublished more than 600 seconds after its advertised volume
minute. The typed unpublished outcome survives the negative cache. Routine
IEM-to-S3 publication lag and other validation errors do not warn. Eligible
warnings retain the existing per-site rate limit and suppressed count.

**Scan identity.** IEM names a scan by its volume start floored to the minute;
the S3 key carries the seconds (`ATX_N0B_2026_09_25_03_42_24` is IEM's 03:42).
The emitter lists the hour prefix (`?list-type=2&prefix=ATX_N0B_YYYY_MM_DD_HH`),
takes the key inside the IEM stamp's minute, and downloads it through the radar
transport under its own dependency, `noaa-level3-n0b`: it shares the global rate
gate, byte accounting and tier, but has its own host breaker and cooldown, so an
AWS outage never blocks v1. A product counts as a success only after it decodes
and its volume time falls inside that minute; a listing only after it parses as
an untruncated `ListBucketResult`. Admission prices a v2 frame by the listings
and products it still needs, not by the tiles it will draw. One download per scan serves
every tile thread; up to 36 N0B scans (~1.3 MB each) and 36 N0H scans (~0.43 MB each) stay in memory, with independent per-product eviction. A scan
S3 lacks, or that fails validation, fails fast for 60 s (10 s after a transport
error) instead of once per tile, keeping the failure's class (local,
ambiguous or provider) so backoff stays right. Waiters share the owner's verdict
and give up at their own deadline.

**Decoding** (`lib/radar_level3.py`) refuses: a size over 2 MB; a WMO header
that isn't two CRLF lines; any product other than the requested 153 or 165; a message length that disagrees
with the bytes; a site more than 0.05 degrees from the listed radar; thresholds
other than (−320, 5, 254) for N0B, or nonzero words 21–23 / word 26 other than 255 for N0H; an elevation outside [−1, 2] degrees; a bzip2 body
whose expanded size disagrees with the header; a packet other than 16; radial
headers out of bounds; more than 2 % of bearings uncovered. Gate code n ≥ 2 is
(n−2)/2 − 32 dBZ; 0 (below threshold) and 1 (range folded) draw nothing. Gates
at or above the 15 dBZ display floor with fewer than two such neighbours are
cleared (aircraft, birds, interference).

**Drawing.** Each pixel centre is taken to ground distance and bearing from the
radar, then to slant range on the 4/3-earth beam at the scan's elevation, and
reads gate `floor(range / 250 m)` of the radial covering that bearing. At zoom 7
a pixel takes the strongest of a 2×2 sample. Colour is the shared display LUT
applied by the same step rule as the IEM remap, so equal reflectivity draws the
same colour in v1 and v2 (tested code by code). Native selection is per pixel,
as specified below; v1 keeps its painter order.

**Measured on the Pi 4** (KATX in rain, 2026-09-24): products 251–333 KB and
0.2–1.6 s each; decode and despeckle 149 ms; a new tile 28 ms at zoom 9–10;
opening a cached tile 2.7 ms. A cold switch at zoom 8 with four radars
downloaded 31 products (9 MB) and filled all eight frames in 59 s, with the
first new frame at 13 s.

### Shared reflectivity palette

`lib/radar_palette.py` pins complete native colour inverses in `lib/data/`, with
byte-identical provenance fixtures in `tests/fixtures/`. MRMS's indexed formula
is `i/2−32`, N0B's is `i/2−33` with reserved codes 0/1 transparent. RainViewer
uses the published Universal Blue table; tiles request scheme 2 with options
`0_0`. These lookup tables supply intensity only, never precipitation type.

All sources draw the same bands from the same remapped (or, for v2, rendered)
tiles on both themes, and every source publishes the same legend: the designed
ramp clipped at the 15 dBZ display floor.

```jsonc
"legend": {
  "id":"almanac-reflectivity-v3", "floorDbz":15, "remapped":true,
  "bands":[
    {"lo":15,"hi":20,"start":"#66A678","end":"#43A05D"},
    {"lo":20,"hi":25,"start":"#43A05D","end":"#209143"},
    {"lo":25,"hi":35,"start":"#088A34","end":"#11672D"},
    {"lo":35,"hi":40,"start":"#C79C14","end":"#B0870D"},
    {"lo":40,"hi":45,"start":"#E5871A","end":"#D2700F"},
    {"lo":45,"hi":50,"start":"#DE5C17","end":"#C94C0C"},
    {"lo":50,"hi":60,"start":"#DD4530","end":"#BC2A1A"},
    {"lo":60,"hi":70,"start":"#CE4E88","end":"#A9389B"},
    {"lo":70,"hi":75,"start":"#8A46C2","end":"#8A46C2"}
  ]
}
```

The designed ramp keeps nine bands from 10 dBZ; the 10 to 15 dBZ part is never
drawn (its LUT stops carry alpha 0). v3 (2026-09-25) changed only the three
green bands. The greens now descend in lightness without reversing, L* 63 at
15 dBZ to 41 at 32.5, with a deliberate step at 25 dBZ. v2's greens saw-toothed
between L* 58 and 62, so at native resolution light and moderate rain read as
one sheet: the nearest colour pair across 25 dBZ rose from 4.5 to 6.2 CIEDE2000
and the smallest step between drawn greens from 2.0 to 2.8. The 35+ bands are
unchanged. There is no precipitation-type key, inference or secondary ramp.
RainViewer's caption ends `· reflectivity only`.

The 26 LUT entries are sRGB samples every 2.5 dBZ, 10 through 72.5, with an
open-ended last stop. Every sample, drawn or not, meets ≥2:1 on page paper
`#F2EDE2` and tinted plate paper `#EBE6DB`, and ≥3:1 on night `#0B0D11`;
the minima are 2.03 on plate paper, 2.17 on page paper and 3.13 at night.
Stale echo opacity remains .66; the canvas has no CSS filter or theme-dependent
recolouring. Site mode's grey 5 to 10 dBZ clear-air band stopped drawing with
the display floor and was removed with v3, with its legend swatch, note and
`--rad-clear-air` tokens.

The legend stays 414px wide, right:12px, with 8px left padding, a 34px unit cell
and a 372px ramp. Band widths are proportional to `(hi−lo)` across that full
372px; its shared 1px border overlays the segments without consuming scale
width. Ticks are de-duplicated `[floorDbz,10,20,30,40,50,60,70]` at or above
the floor, positioned at `(dBZ−floorDbz)/(75−floorDbz)*372`, each with a 1×3px
hairline. The legend rebuild key includes the floor even when source and legend
id agree. The ramp has `role="img"` and
`aria-label="Reflectivity scale, 15 to 75 dBZ."`.

IEM XYZ tiles actually arrive as RGBA with antialiased colours. After existing
PNG/size/placeholder validation, the compositor remaps each distinct colour:
exact RGBA, then exact RGB, then nearest native RGB within Euclidean distance 3.
Visible output uses `round(nativeCoverage * targetAlpha / 255)`, with the
alpha-zero suppression path unchanged. Opaque rain stops preserve their exact
pre-v3.2 RGBA bytes in indexed and RGBA tiles. Repeated and tolerated RGB matches
retain their candidate dBZ range; crossing a target stop sets `remapped:false`
and counts `ambiguousPixels`. Verified provider-indexed PNG palettes preserve
numeric indices rather than losing repeated-colour intensity information.
Unknown opaque colours become transparent and are counted.

For at most 256 native RGBA colours, verify an adaptive palette's exact RGBA
round trip before swapping its palette. Pillow's RGBA octree is not always
lossless even below 256 colours; if verification fails, verified exact RGB
median-cut and a separately rounded coverage/target-alpha product perform
the remap in C. More than
256 colours uses channel masks, bounded at 1024 colours; larger inputs are
rejected. There are supersede checkpoints before and after every tile.

### Source rendering and playback

Site selection, real scan listings, nearest reporting station-timeline primary,
230km spherical range intersection and the four-site cap survive. In v1,
farthest-first stacking and real secondary scans no later than the primary and
no more than 900s older remain. Tile failures preserve other successful tiles;
the page combines aligned v1 site tiles using one 256px scratch for plain or
Smooth, never four viewport layers. v2 instead uses the single native mosaic
layer, QC and time rule specified above.

The site picker always names the closest site (`nexrad.id`), falling back to the
caption's primary only when `nexrad` is null. It retains the number of actual
other contributors from the displayed frame (`KATX +2`); that count is relative
to the drawn primary, not the closest site's reporting state. A dark KATX never
renames the button KLGX. The caption independently names the reporting timeline
owner while its tiles are late (`KATX loading`); an explicitly non-reporting
primary can yield to an actual contributor with the not-reporting suffix.
The visible segments are `Region` and the closest callsign (`KATX`, `KATX +2`).
The region segment has `aria-label="Region: many radars blended"`; the site
segment expands to `KATX: Camano Island radar, high resolution, 39 mi NE` or
`KATX and 2 nearby: Camano Island radar, high resolution, 39 mi NE`.
Accessible distance describes the closest site; caption distance only describes
the drawn site when it is `nexrad.id`.

Caption copy (v5.1, retaining v4.3b vocabulary) names the subject, arrival
cadence, and provider:

- MRMS: `Many radars blended · new image every 2 min · IEM / NOAA`.
- RainViewer: `Worldwide blend · new image every 10 min · RainViewer · reflectivity only`.
- Nearest single site (when width permits): `Camano Island radar, high resolution · 39 mi NE · precipitation mode · new scan every ~4 min · IEM / NOAA`.
- Neighbours: `Camano Island radar + 2 nearby · precipitation mode · new scan every ~4 min · IEM / NOAA`.
- Closest dark: `Langley Hill radar + 2 nearby · precipitation mode · new scan every ~4 min · IEM / NOAA · KATX not reporting`.

The drawn site's site-table name wins, then `nexrad.name` only for the nearest
site, then its callsign. KLGX's catalog name is `Langley Hill`. Nearest metadata
never supplies a neighbour's name.
Only `nexrad.distanceDisp` plus `bearing` supplies the station-relative distance.
Visible distance yields to the neighbour count; the accessible name retains
it when valid. Mosaic cadence stays derived from `cadenceSec/60`; the site's
approximate volume cadence keeps its tilde. `#rad-status` alone states freshness.

Site-only operating metadata (independent of the nominal `cadenceSec` poll interval):

```json
{"scanCadenceSec":240,"scanMode":"precipitation","scanModeSource":"cadence","scanningSlowly":false}
```

`scanCadenceSec` is the median of the last three gaps in the **primary site's
listing**, even when fewer frames have been acquired. Neighbour timestamps,
tile completion, and `frameSpacingSec` cannot change it. One or two gaps still
produce a measured median but `scanMode:null`; zero gaps produces a null cadence
and omits the entire cadence phrase. A single site scan carries `latestOnly:true`.
With three gaps, ≤390 seconds means `precipitation`, 390–540 exclusive means null,
540–900 inclusive means `clear-air`, and >900 means null. `scanningSlowly` is true
only for a measured median >900 seconds, including a short listing. Ten-minute
clear-air operation is normal and never gets that exception.

`scanModeSource` is `cadence` when a mode is inferred, otherwise null. No `vcp`
field exists. `vcp` and source `vcp` are reserved for an actual upstream VCP
number; they must never be synthesized from timestamps. Future mapping, only
when such data exists: 12/112/212/215 → precipitation, 31/32/35 → clear-air,
unknown → null; the interval remains measured.

Caption: `precipitation mode · new scan every ~4 min`, `clear-air mode · new scan
every ~10 min`, or `new scan every ~7 min` without a mode word. Minutes are
`max(1, round(scanCadenceSec/60))` in the page. There is no fixed site interval.
Mosaic and RainViewer keep their existing `new image every N min` copy.
The Fable N5 example `[6,6,7] → null` conflicts with M2's median/≤390 rule;
M2 governs, so that fixture yields precipitation at six minutes. A `[7,7,7]`
fixture tests the ambiguous range explicitly.

No caption contains NEXRAD, MRMS, mosaic, volumes, or dBZ. Provider remains a
visible `#rad-attrib` text-only anchor without href, including during switching.

Exception wording and ordering after attribution remain as before (including
`reflectivity only`, `latest only`, `scanning slowly`, `KXXX loading`,
`scan unavailable`, `deferred`, `out of view`, `not reporting`,
`palette incomplete`, `wider than KXXX reaches`). This follows Fable's exact
RainViewer and dark-neighbour assertions; `· switching` is always last.
Site subjects add `, high resolution` when width permits. Rendered overflow
drops that phrase first, then distance, then shortens `new scan every ~N min` to
`every ~N min`, then removes the interval if a mode is present, then drops
`+ n nearby`. The mode outlives both the interval and nearby count. Without
a mode, `every ~N min` survives the nearby drop. Provider and exception text is never
removed. The caption uses `overflow:hidden; text-overflow:ellipsis; white-space:nowrap`
for any remaining overflow. Its outer maximum is 530px (516px text plus the
existing 14px padding), satisfying Fable's explicit bounding-box gate; this is
stricter than the spec's 530px content / 544px outer prose budget. The plate's accessible name
lists contributing IDs. With at least two contributors, `#rad-over` draws their
geodesic 230 km arcs in `--rule-faint` (1px, dash 2 3), clipped to the plate.
Site centres inside the viewport have 2px `--ink-soft` dots and 11.5px labels
(dx5, dy13), omitted unless the complete label fits y76–412 and the viewport.
Station marker, rings, scale bar and displaced-station treatment retain their meanings.

A source switch retains the previous visible scan and metadata until four target
frames decode (or all available target frames for a shorter loop). It then
presents matching source/legend/time together, choosing a decoded frame even
when newest is pending. Outgoing and staged plates share the graphics budget;
abandonment closes staged plates. A camera pan resets their pixels while
preserving the target manifest and acquisition ownership.
Same-source backwards times retain the newer scan and its true age. Each frame's
actual drawn site list and remap quality drive its caption; a dark or missing
site is not relabelled clear.

The plate fills the 956×490 body at x34,y76 on the 1024×600 tabbed artboard.
The 25px secondary header and 34px gutters remain. The masthead subtitle is
restored; alert cases use an inline subtitle and compact masthead/alert band.
No rail, Range, Updated or Frames rows remain. Source picker/caption live at
top-left, continuous 372px horizontal legend at top-right, loop/track at
bottom-left, zoom at bottom-right. The single legend stays 372px for every source. Controls have >=44px
hit areas. Chrome is confined to y0–72/y418–490, clear of the r150 station disc.
The single note begins at y418 so it obeys the protected-band invariant.
Scrims use flat paper .82 / night .78 alpha, never filters; chrome has no ramp pigment.
The plate scopes secondary ink to 78% `--ink` / 22% `--paper`, so note, caption
and reads meet 4.5:1 even over worst-case echoes in both themes. The displaced
station accent is unchanged. Ramp colours are confined to echo data and its scale.

Playback draws pre-decoded ImageBitmaps onto one canvas. The visible canvas has
no `src`; a tick neither fetches nor decodes. A monotonic requestAnimationFrame
clock uses **350ms** steps and an **1100ms** newest hold. RainViewer short loops
use `clamp(2400/frameCount,110,180) * 350/110` ms, scaling the existing formula.
Each transition (including loop wrap) blends two real cached scans linearly for
120ms: previous weight `1-a`, next weight `a`. Two `drawImage` calls per composite
paint use nearest-neighbour sampling. Premultiplied additive composition preserves
translucent echo alpha; ordinary source-over would not give a linear mixture.
The blend is **temporal**, never spatial smoothing or a fabricated scan. The read
names the frame whose weight is ≥0.5 (next wins the tie). Reduced motion uses
hard cuts and an opt-in single sweep.

Late animation callbacks schedule the next interval from actual presentation.
An elapsed blend is completed before admitting the next transition, so sustained
callbacks slower than the frame interval still advance the loop rather than
restarting the same blend at zero opacity.

**v4.7 manifest continuity (no new wire fields):** A scheduled stamp advance
at unchanged source, site timeline, station, viewport bounds/zoom, render revision
and legend retains the previously published frames whose timestamps are within
3600 seconds of the incoming newest (inclusive). After discovery is validated,
the engine lists the newest as pending before native tile I/O: a warm 31-frame
MRMS hour becomes 31 listed / 30 complete, with only the expired oldest removed.
Every partial-tile publish carries that window; completion updates the newest in
place. Backfill retains historical `siteScans` exactly, even if fresh per-site
listings would choose different scan pairs. Site windows slide by actual scan
stamps. The shared 25-second pass deadline may leave newest pending for a
retry, but does not collapse history. Cold start and changed geometry still
publish their first measurement with a new window.

**v4.6 playback state machine, with v4.7 reconciliation:** `loaded` contains the
latest eight listed scans plus previously held scans omitted by a truncated
manifest while they remain within the incoming newest's hour. An omission alone
cannot close a composite or remove it from the decoded inventory. A station
change releases the old window. Source/site switches stage their existing
source transition. Actual page-camera/native-level, render revision and legend
changes stage a replacement window while keeping playable outgoing composites
(v5.9). Engine coverage-grid/camera acknowledgements do not invalidate composites
already rendered for the page camera. Source staging captures that actual camera
as well; stamp/site-scan identity changes still replace the affected frames.
When the complete manifest returns, normal latest-eight selection applies again.
`readyFrames` is the latest eight decoded incoming composites in time order.
During geometry or Smooth-preference acquisition `radarReady()` reports the retained playable window
until four replacements decode, so readiness cannot become zero while old imagery
is drawable. `cycle`
is the fixed playable snapshot for the underway pass. A missing middle
frame does not exclude older decoded scans. `good` names acquisition's newest;
`current` names the drawn scan. Acquisition remains newest-first.

| State / event | Display and next transition | Inventory / lifetime |
| --- | --- | --- |
| Cold acquisition | Paint newest as it decodes; start when `min(4, total)` scans are decoded (at least two to animate). | Count decoded composites, not tile coverage or a contiguous suffix. |
| Playing / manifest or decode arrives | Preserve current scan, blend and deadline; finish the fixed cycle and its old-newest 1100ms hold. | Stage the latest window and decoded arrivals; retain composites with the same frame key. |
| Geometry change | Continue the current cycle through the camera, without resetting its deadline. Keep naming the displayed scan; corner note says `Playing previous view · sharpening N of M` (v6.0). | Stage newest-first replacements; wait for four, then adopt at wrap. At four, free already played outgoing prefix scans oldest first (preserve current/blend and the remaining sequence); close the remainder on adoption. Never free a future scan merely to make room. |
| Wrap | With four replacements ready (otherwise repeat the outgoing cycle), snapshot all currently decoded scans in the latest window; blend old-newest → slid-oldest, then progress to new-newest and its hold. | Late frames join here. Close aged-out composites once neither cycle, current nor blend needs them. |
| Paused / manifest arrives | Keep the displayed scan; update the window silently. Resume from that scan if retained, otherwise wrap to the slid-oldest. | Retain a displayed aged-out composite until playback leaves it. |
| Reduced motion | Same wrap adoption, no crossfade. One requested sweep stops at that cycle's newest; another Play uses the updated window. | Same decoded inventory and bitmap lifetime rules. |

`AS OF` and the loop read name the displayed measurement, including retained
geometry/source imagery; acquisition progress belongs in the corner note. Frame identity is render revision +
source + the frame's own stamp + site-scan identities; it excludes the manifest's
newest stamp. Source/revision are captured on each loaded frame so mutable manifest
fallbacks cannot re-key retained composites. Aging removes only the composite;
resident native tiles remain subject to the existing LRU. A wrap performs no fetch
or decode; a resident-native new scan requires compositing only. Newly unavailable
native inputs still require normal newest-first acquisition.

The v6.0 geometry note uses no new payload field: `N` counts bitmaps in the
incoming `loaded` window and `M` is its advertised inventory, capped at eight.
The prefix separately names retained playback (`Previous view` when playback is
stopped or paused); zero sharpening progress cannot
be confused with zero playing frames. After adoption ordinary refresh copy
resumes. Failure/retry notes retain their precedence and the read/AS OF continue
to name the actual painted scan. A dirty camera/tile paint coinciding with a
playback deadline yields to that deadline: reproject the successor once, then
admit at most one native decode in that RAF. Incoming composite assembly remains
limited to one per RAF on frames without a paint. Ordinary same-camera temporal
blends retain their two weighted draws; neither entire window is reprojected
on every animation frame.

The loop cluster at left:12px/bottom:12px keeps its box through play, clear,
buffering, zoom, source swaps and frame staging; only an inactive radar tab or
no radar hides it. The 220×1px rail is permanent. Its 2px marker has `display:none`
with zero ready frames, appears at left:218px with one, and tracks frame index
with two or more. The existing .11s left transition remains the entire marker
animation, suppressed for reduced motion.

Play stays visible and **never disabled while the cluster is shown**. This
2026-09-14 user direction explicitly overrides Fable v3.2 K13/K14/K16's
content-based disabled-control rules. The 44×44px control uses ▶ and
`aria-label="Play radar loop"` for paused intent, or ❙❙ and
`aria-label="Pause radar loop"` for play intent, including buffering/fetching.
Every press changes the glyph and read immediately and gives a 120ms button
opacity dip (suppressed for reduced motion). Transport, stale and
visibility fences still control actual animation, independently of intent.

Without a displayed composite, before the start threshold the read is
`Buffering · N of M` for play intent or `Paused · N of M` for paused intent,
including zero. Any retained or decoded displayed composite keeps its time read
while acquisition continues; there is no buffering read over playable imagery. `M` is the actual window
inventory (capped at eight), not a hard eight; six scans per hour count as six.
Once playing, the read follows the drawn `HH:MM · newest` / `HH:MM · −N min`
without returning to buffering when a new scan arrives. Paused scan reads have
`Paused · ` prefixed. Complete clear history reads `No echoes · clear`, likewise
prefixed when paused. The tick tracks position in the cycle's ready set; newly
decoded frames change those positions only at the wrap. While paused it tracks
the updated decoded set, clamped to the oldest position if the held scan aged out.
The read has neither status role nor aria-live; the corner note describes pass
progress and owns that live region and its existing 600ms suppression. No scans
are duplicated or padded. The browser normally retains eight window composites,
plus outgoing frames still needed by a cycle/display/blend. A truncated manifest
also retains omitted in-hour composites (eight old plus one pending scan in the
stamp-advance regression); they remain subject to the existing 40MiB graphics
admission cap and are closed on expiry or release. One supplied frame is
static; clear data keeps the cluster visible and enabled. Hidden pages idle.
The loop never changes `#rad-base`.

The loop never changes geography. Leaving the tab or hiding the document closes
all echo bitmaps and cancels gesture/idle work; decoded basemap tiles remain.
Basemap: Natural Earth (public domain). Artifact provenance and reproducible
build: [tools/RADAR_BASEMAP.md](../../tools/RADAR_BASEMAP.md).

### Verification and hardware boundary (R)

`tests/test_radar_basemap.py` covers deterministic raster goldens, fixed palette,
exact theme pixels, antialiasing, even-odd holes, zoom membership, prewarm order,
immutable HTTP and the independent geography disk cap. Radar emitter suites
cover per-tile bytes/metadata, coverage masks, wrapped grids, budgets, providers
and security. `RADAR_NET_TEST=1` adds Seattle/Aberdeen live tile sets compared
byte-for-byte with remapping of their native provider responses.

`tests/verify_radar_headless.py` uses the real loopback handler in both themes.
It measures 200px pans, focal pinch/snap, tile request/decode activity, cached
first paint, ancestor identity, graticule pixels and absence of hatch pixels,
real capture/cancellation,
source transitions, failed-report recovery, overlay mutations, palette/legend
purity, playback and 60-second memory sessions. Its `chrome` checks cover all
27 Fable v4.3b copy/accessibility/geometry assertions in paper and night, plus
font-measured overflow drop order, name fallbacks, shared-timer delivery,
acquisition acknowledgement and stale-versus-new refresh failures.
`tests/verify_radar_v44.py` also runs the v4.5 pixel oracle in
`tests/verify_radar_v45.py`: all missing fractions, transparent acquired tiles,
site/MRMS boundaries, RainViewer, unknown masks, cached composites, and delayed
mid-acquisition stills in both themes must show no hatch element or pigment.
`tests/verify_radar_v46.py` drives the real renderer with a deterministic monotonic
clock in both themes: manifest during a blend, old/new newest holds, slid wrap,
resident-native stamp advances with zero tile fetches, bitmap retention/closure,
late middle decode, paused updates/resume, cold newest, read/tick/AS OF and reduced
motion single sweeps. `tests/test_radar_v46.py` covers identity and readiness in
the offline suite. Existing v4.4/v4.5 blend/pixel checks remain in force.
`tests/verify_radar_picker.py` preserves the real touch, click, keyboard, immediate
intent, 20-second polling-bound and integrated warm emitter-to-canvas checks,
including a Region return that refills the decoded loop with zero additional
native provider tile requests;
v5.7 supersedes its former delayed-caption assertion: pending copy is required
in the first animation frame, with four decoded frames and advancing playback. Independent instrumentation
tracks canvas/bitmap allocation and draw calls. Headless wall times are reported,
not treated as Pi acceptance. The cold-outage fixture has no measurement frames.
Stills and timing logs default to `/tmp/wfp-radar-v41/`.

Run `python3 tools/benchmark_radar_kiosk.py > radar-v41-pi.json` on the panel
with the kiosk's CDP port at 127.0.0.1:9222. It evaluates the real page and prints
per-frame pan/pinch draw time/count, bilinear basemap draw time, cached first
paint, memory and GPU information. It restores the original camera afterward.
The GPU-enabled Pi measurements, not development-machine timings, decide the
4ms performance target. Renderer generation likewise needs Pi median/p95 evidence.

The v4.7 regression runner (`tests/verify_radar_v47.py`) uses only a 127.0.0.1
fixture server. In both themes it verifies zero-fetch same-stamp truncation,
bitmap lifetime and identity/expiry boundaries, then holds new-scan native HTTP
responses for 25 seconds and prints loaded/ready/read once per second. The eight
retained scans keep cycling and the new scan joins at a later wrap. Engine tests
(`tests/test_radar_v47.py`) inspect the first and every partial publish for warm
MRMS and per-site windows, including primary-deadline exit and retry.

### Radar v5.9 local verification

`tests/test_radar_v49.py::test_three_bad_tiles_real_warm_connections` barriers
three silent/header-only primaries and seven returned healthy leases before a
separate fake inactivity clock advances. The real TLS body-stall path cannot
finish before rescue. Its unchanged nine-second batch must deliver ten tiles,
three winning hedges, zero retries/discards and thirteen wire requests.
`tests/test_radar_v59.py` proves every header/body chunk postpones the hedge and
only a full two-second inactivity interval admits it.

`tests/verify_radar_v59.py` exercises stepper zoom, pan and two-level zoom with
a deterministic Chromium clock in both themes: exact outgoing scan sequence,
four newest-first decoded replacements, wrap adoption, ordinary paint intervals
280–420ms (350ms cadence ±20%; the existing 1100ms newest hold is separate),
and oldest-first bitmap closes. A loopback-only 8→7→6→7→8 sequence at three-second
spacing measures actual paints, decode admission per RAF and independent bitmap
ownership against 40MiB. No panel CPU or process RSS claim follows from these
local raster-storage and paint measurements.

### Radar v6.0 local verification

`tests/test_radar_v60.py` covers unknown/empty/old/fresh/failed listings, publication
age versus fixed check time, Region discovery cadence/reserve, immediate refusal
without transport or hysteresis, continuing Region updates, and single-listing
recovery. `tests/verify_radar_v60.py` uses the v5.9 loopback fixture in paper/night
for before-tap evidence, synchronous and matching-poll refusal, generation fencing,
stale/error distinctions, retained playback pixels and a forced dirty/deadline
collision with one echo paint per RAF. The v5.9 zoom/pan/two-step scenarios and
real loopback acquisition remain required.

`PYTHONPATH=. ./venv-test/bin/python tools/benchmark_radar_v60.py -o FILE.json`
measures 8→7→8 with the same local fixture and headless Chromium (`--disable-gpu`),
starting from an eight-scan cycle with its next deadline at +150ms,
recording per-RAF echo/reprojection/native-decode/composite counts plus CDP
renderer TaskDuration/ScriptDuration over each 0.6-second step. These local task
clock values do not measure all Chromium process/thread CPU or establish Pi
performance. `--html PATH` selects a local baseline page without changing the
working tree. Fixture traffic is restricted to its 127.0.0.1 origin.
