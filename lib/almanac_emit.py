""" Additive JSON data-emitter for the "almanac" overlay layout.

This module is NEW and does not alter the classic console data path in any
way. It periodically reads the same DictProperties the classic screen already
populates (`CurrentConditions.Obs` / `.Astro` / `.Met` / `.Sager` / `.System`,
plus `app.config['Station']`) and writes them out as a flat, display-ready
JSON file that an external HTML overlay polls (design/almanac/console.html).
The exact shape is documented in design/almanac/DATA_CONTRACT.md - read that
file before changing any key here.

Wiring: panels/almanac.py's AlmanacConditions.add_panels() calls
`AlmanacEmitter(self).start()` once. Nothing in main.py's classic path
(CurrentConditions) imports this module, so the classic screen is completely
unaffected.

Copyright (C) 2018-2025 Peter Davis (classic console) / almanac add-on.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
"""

from kivy.logger import Logger
from kivy.clock  import Clock

from collections import namedtuple
from datetime import datetime, timedelta, timezone
import json
import os
import re
import threading
import time
import pytz

from lib.almanac_shared import (
    ALERTS_UA_FALLBACK, _cfg, _clock, _clock_style, _clean_str, _num,
    _json_safe, _snapshot_field, _station_tz,
)
from lib.provider_runtime import ProviderRuntime
from lib.radar_engine import RadarEngine

# ==============================================================================
# CONFIGURATION
# ==============================================================================
# Default output path. This is a deployment concern for the HTML overlay
# (which is served/polled independently of the console), not a user-facing
# console setting, so it is kept as a plain module constant rather than wired
# into lib/config.py. Override by editing this constant.
OUTPUT_PATH   = '/tmp/wfp_data/wx.json'
EMIT_INTERVAL = 2.0     # seconds, per DATA_CONTRACT.md ("~every 2 s")
VERSION_CHECK_INTERVAL = 900   # seconds (15 min) — how often we poll GitHub for a newer release
AQI_CHECK_INTERVAL     = 600   # seconds (10 min) — refresh air quality; short enough to recover fast
ALERTS_CHECK_INTERVAL  = 900   # seconds (15 min) — NWS alerts change slowly; be gentle on api.weather.gov
FORECAST_CHECK_INTERVAL = 3600 # seconds (1 h) — the daily outlook barely moves intra-hour
FORECAST_RETRY_SEC      = 120  # seconds — boot retry cadence until the FIRST forecast succeeds
CARRY_MAX_SEC = 6 * 3600   # a restarted engine republishes its last observations no older than this
CARRY_WINDOW_SEC = 600     # ... and fills still-unfetched fields (forecast, AQI, Sager) from them this long after start
CARRY_SKIP = frozenset(('radar', 'alerts', 'alertCount', 'alertsAgeSec', 'alertsAsOf', 'alertsStale',
                        'ts', 'time', 'date', 'obsTs', 'obsAgeSec', 'carried',
                        'updateAvailable', 'latestVersion', 'currentVersion'))
RAIN_START_HOLD_SEC = 300  # evt_precip shows 'Rain Starting' until the next obs_st, never longer than this

FC_STALE_SEC           = 86400 # seconds (24 h) without a successful forecast fetch -> fcStale (band hides)
RAIN_WINDOW_SEC        = 600   # seconds (10 min) — light rain is bridged across the sensor's dry minutes
ALERTS_TIMEOUT         = 20    # seconds — socket timeout for the alerts fetch
ALERT_STALE_SEC        = 3600  # seconds (1 h) without a successful alerts fetch -> mark alertsStale
AQI_STALE_SEC          = 3600  # seconds (1 h) without a successful AQI fetch -> mark aqiStale
ALERT_MAX              = 3     # cap the alerts array (the HTML strip renders only the lead)
# NWS product level parsed from the LAST word of the event name — a controlled
# vocabulary that stays reliable even when CAP severity/urgency are 'Unknown'.
# 'Alert' products (e.g. Air Quality Alert) bucket as advisory-tier. Higher = more urgent.
_ALERT_LEVEL     = {'emergency': 4, 'warning': 4, 'watch': 3, 'advisory': 2, 'alert': 2, 'danger': 2, 'statement': 1, 'outlook': 0}
_ALERT_LEVELNAME = {4: 'warning', 3: 'watch', 2: 'advisory', 1: 'statement', 0: 'outlook'}
# Hazard family — for the label/nuance only; the banner colour is derived from the level.
_EVENT_CLASS_MAP = [
    ('tornado', 'severe'), ('thunderstorm', 'severe'), ('hurricane', 'severe'), ('tsunami', 'severe'),
    ('air quality', 'air'), ('smoke', 'air'), ('red flag', 'air'), ('fire', 'air'), ('heat', 'heat'),
    ('winter', 'winter'), ('snow', 'winter'), ('ice', 'winter'), ('freeze', 'winter'),
    ('wind', 'wind'), ('flood', 'water'), ('coastal', 'water'), ('fog', 'water'),
    ('gale', 'water'), ('surf', 'water'),
]
                               # from a transient boot-time network failure on the flaky USB wifi

# Strips Kivy markup, e.g. '[color=ff8837ff]Rising[/color]' -> 'Rising'
_MARKUP_RE = re.compile(r'\[/?color[^\]]*\]')

# Single-glyph degree symbols produced by observation_format.format(...,'Temp')
# (u'\N{DEGREE FAHRENHEIT}' / u'\N{DEGREE CELSIUS}') normalised to the plain
# two-character form the HTML/JSON contract expects.
_DEGREE_GLYPHS = {'℉': '°F', '℃': '°C'}

_COMPASS_16 = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
               'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']


# ==============================================================================
# SAFE ACCESSORS
# ==============================================================================
# The console's Obs/Astro/Met/Sager DictProperties hold values that are lists
# produced by observation_format.format()/units() (e.g. ['64.0', 'F']), but
# early in the app lifecycle - or after a station/device change resets a
# DictProperty - individual entries can still be the bare placeholder string
# ('-', '--', ...) or, in principle, missing/None. These helpers make every
# lookup below crash-proof.
def _idx(seq, i, default=None):
    """ Safely index into a value that SHOULD be a list/tuple, tolerating a
    bare placeholder scalar, None, or a too-short list. Never raises. """
    if seq is None:
        return default
    if isinstance(seq, (list, tuple)):
        if 0 <= i < len(seq):
            val = seq[i]
            return default if val is None else val
        return default
    # Not a list - a bare scalar/placeholder string. Only index 0 applies.
    return seq if i == 0 else default


def _get(d, key, default=None):
    """ Safely fetch `key` from a dict-like object that might not be one. """
    try:
        return d[key]
    except (KeyError, TypeError, IndexError):
        return default


class _RainWindow:
    """ Time-weighted mean rain rate over the last `span` seconds. Each sample
    holds until the next one, so sampling on the 2 s emit tick reproduces the
    sensor's per-minute steps faithfully; the mean over the window is the
    rain that actually fell, expressed as a rate. effective() returns
    max(raw, mean): a drizzle's dry minutes are bridged, a downpour is never
    understated, and the window drains linearly once rain stops. """

    def __init__(self, span):
        self.span    = span
        self.samples = []            # [(epoch_s, mm_per_hr), ...] oldest first

    def effective(self, now, rate_mm_hr):
        if rate_mm_hr is not None:
            self.samples.append((now, float(rate_mm_hr)))
        cutoff = now - self.span
        self.samples = [s for s in self.samples if s[0] >= cutoff]
        if rate_mm_hr is None or not self.samples:
            return None
        total = 0.0
        for (t0, r), (t1, _) in zip(self.samples, self.samples[1:]):
            total += r * (t1 - t0)
        t_last, r_last = self.samples[-1]
        total += r_last * (now - t_last)
        covered = max(now - self.samples[0][0], 1.0)
        mean = total / covered
        return round(max(float(rate_mm_hr), mean), 4)


def _clock_case(text):
    """ Upstream clock strings: Sager writes "6:53 pm" (%P) where every other
    string says "PM", and all of them put a breaking space before the meridiem
    ("Clear until 1 / AM on Saturday"). One case and a no-break space for the
    page. None and non-clock text pass through. """
    if not isinstance(text, str):
        return text
    return re.sub(r'(\d) ([AaPp][Mm])\b', lambda m: m.group(1)+'\u00a0'+m.group(2).upper(), text)


def _range_mid(value, default=None):
    """ Midpoint of a formatted uncertainty range. The console core renders the
    last strike distance as a +/-3 km band ("13-17"), never a bare number, so
    every numeric consumer of it needs the middle of that band. Accepts a plain
    number/numeric string too. Never raises. """
    text = _text(value)
    if text is None:
        return default
    parts = text.replace(u'\u2013', '-').split('-')
    nums = [_num(p) for p in parts if _num(p) is not None]
    if not nums:
        return default
    return sum(nums) / len(nums)


def _text(value, default=None):
    """ Coerce a value into a clean display string: strips Kivy colour markup
    and blanks known placeholders. Never raises. """
    if value is None:
        return default
    if not isinstance(value, str):
        return str(value)
    text = _clean_str(value)
    if text is None:
        return default
    if '[' in text and ']' in text:
        text = _MARKUP_RE.sub('', text).strip()
    return text or default


def _wind_desc(value):
    """ Beaufort description word, trimmed for a status label
    ('Calm Conditions' -> 'Calm'). """
    text = _text(value)
    return text.replace(' Conditions', '') if text else text


def _temp_unit(value):
    """ Normalise the single-glyph degree unit (u'\N{DEGREE FAHRENHEIT}' etc.)
    produced by observation_format into the plain "°F"/"°C" the contract
    shows, falling back to whatever text is present. """
    text = _text(value)
    if text is None:
        return None
    return _DEGREE_GLYPHS.get(text, text)


def _cardinal_from_degrees(deg):
    if deg is None:
        return None
    try:
        return _COMPASS_16[int(round((float(deg) % 360) / 22.5)) % 16]
    except (TypeError, ValueError):
        return None


def _age_sec(ts, now):
    """ Whole seconds since `ts`, or None when that source has never reported.
    Clamped at 0 - a sensor clock a little ahead of ours must not produce a
    negative age. Never raises. """
    if ts is None:
        return None
    try:
        return max(0, int(now - float(ts)))
    except (TypeError, ValueError, OverflowError):
        return None


def _ago_text(sec):
    """ "just now" / "12 minutes ago" / "5 hours ago" / "3 days ago" from an age
    in seconds, matching the vocabulary observation_format's 'TimeDelta' uses. """
    if sec is None:
        return None
    sec = int(sec)
    for span, unit in ((86400, 'day'), (3600, 'hour'), (60, 'minute')):
        if sec >= span:
            count = sec // span
            return f'{count} {unit}{"" if count == 1 else "s"} ago'
    return 'just now'


def _since_ago_text(strike_delta_t):
    """ Build a "3 days ago" / "5 hours ago" / "12 minutes ago" style string
    from the ['d','days','h','hours', epoch] shape that
    observation_format.format(..., 'TimeDelta') produces for StrikeDeltaT.
    Returns None if the underlying value is a placeholder (no strikes seen). """
    n1, u1 = _text(_idx(strike_delta_t, 0)), _text(_idx(strike_delta_t, 1))
    if n1 is None or u1 is None:
        return None
    return f'{n1} {u1} ago'


# Each provider publishes one immutable result. The emit tick reads that
# reference once, so fields cannot mix two off-thread fetches.
_AqiResult = namedtuple('_AqiResult',
                        'aqi category pm25 ts forecast peak peak_time fc_cat trend trend_text')
_AQI_NONE = _AqiResult(None, None, None, None, (), None, None, None, None, None)

_FcResult = namedtuple('_FcResult', 'daily hourly ts')
_FC_NONE  = _FcResult((), (), None)

_AlertsResult = namedtuple('_AlertsResult', 'features alerts ts')
_ALERTS_NONE  = _AlertsResult(None, (), None)

_VerResult = namedtuple('_VerResult', 'available latest current')
_VER_NONE  = _VerResult(False, None, None)


# ==============================================================================
# EMITTER
# ==============================================================================

class AlmanacEmitter:
    """ Periodically snapshots the console's live Obs/Astro/Met/Sager/System
    DictProperties into a flat JSON file for the almanac HTML overlay.

    This is purely a read-side tap: it never writes back to the app's Kivy
    properties, so it cannot perturb the classic (or almanac) display path.
    """

    def __init__(self, screen, output_path=OUTPUT_PATH, interval=EMIT_INTERVAL):
        self.screen      = screen                      # AlmanacConditions instance
        self.app         = screen.app
        self.output_path = output_path
        self.interval    = interval
        self._event      = None
        self._runtime = ProviderRuntime(lambda: Clock, lambda **kwargs: threading.Thread(**kwargs))
        self.radar = RadarEngine(
            output_path, runtime=self._runtime,
            config=lambda: getattr(self.app, 'config', None),
            forecast_updated=lambda: (getattr(self.screen, 'Met', {}) or {}).get('UpdatedTs'),
            refresh_alerts=lambda: self._check_alerts(), logger=Logger,
        )
        self._warned     = False
        # barograph 24h SLP series cache (refreshed every BARO_SERIES_TTL s so we
        # don't re-parse the 1440-point REST payload on every 2 s emit tick)
        self._baro_series_cache = []
        self._baro_series_t     = 0.0
        # rolling rain-rate window: the Tempest's haptic sensor reports drizzle
        # as an occasional 0.01 in minute with zeros between, so the raw
        # per-minute rate flickers 0 <-> trace and the gauge went dry mid-drizzle
        self._rain_win = _RainWindow(RAIN_WINDOW_SEC)
        self._started_at = time.time()   # obsAgeSec counts from here until the first observation
        self._carried = self._load_previous_payload()  # the last run's wx.json, republished until live data lands

    # Provider results, each published as ONE snapshot by its worker thread.
    # Class-level so the "no data yet" state needs no instance setup.
    _aqi_result    = _AQI_NONE      # air quality, Open-Meteo or WAQI by lat/lon
    _fc_result     = _FC_NONE       # 7-day outlook, Open-Meteo by lat/lon
    _alerts_result = _ALERTS_NONE   # NWS alerts, api.weather.gov by lat/lon
    _ver_result    = _VER_NONE      # GitHub release check

    # Snapshot fields, readable/writable one at a time (see _snapshot_field).
    _aqi            = _snapshot_field('_aqi_result', 'aqi')
    _aqi_category   = _snapshot_field('_aqi_result', 'category')
    _aqi_pm25       = _snapshot_field('_aqi_result', 'pm25')
    _aqi_ts         = _snapshot_field('_aqi_result', 'ts')          # last SUCCESSFUL fetch (staleness)
    _aqi_forecast   = _snapshot_field('_aqi_result', 'forecast')    # [[epoch, us_aqi], ...] next hours
    _aqi_peak       = _snapshot_field('_aqi_result', 'peak')        # max us_aqi over the next 6 h
    _aqi_peak_time  = _snapshot_field('_aqi_result', 'peak_time')   # station-local hour of the peak
    _aqi_fc_cat     = _snapshot_field('_aqi_result', 'fc_cat')      # AQI category at the peak
    _aqi_trend      = _snapshot_field('_aqi_result', 'trend')       # 'rising' | 'falling' | 'steady'
    _aqi_trend_text = _snapshot_field('_aqi_result', 'trend_text')  # "Moderate by 5 PM" | "Improving"
    _fc_daily       = _snapshot_field('_fc_result', 'daily')        # [{day,hi,lo,code,pp}, ...]
    _fc_hourly      = _snapshot_field('_fc_result', 'hourly')       # [[epoch, temp], ...] hero curve
    _fc_ts          = _snapshot_field('_fc_result', 'ts')
    _alert_features = _snapshot_field('_alerts_result', 'features') # last-good NWS properties
    _alerts         = _snapshot_field('_alerts_result', 'alerts')   # processed + collapsed + sorted
    _alerts_ts      = _snapshot_field('_alerts_result', 'ts')
    _update_available = _snapshot_field('_ver_result', 'available')
    _latest_version   = _snapshot_field('_ver_result', 'latest')
    _current_version  = _snapshot_field('_ver_result', 'current')

    def start(self):
        """ Schedule the periodic emit and the provider polls. Idempotent -
        calling twice (e.g. if add_panels() is re-invoked by a PanelCount
        change) cancels every handle from the previous run rather than
        stacking a second set of timers. """
        with self._runtime.lock:
            self.stop()
            self._runtime.running = True
            try:
                os.makedirs(os.path.dirname(self.output_path), exist_ok=True)
            except OSError as error:
                Logger.warning(f'almanac_emit: could not create output directory - {error}')
            self._event = self._runtime.schedule(self._emit, self.interval, interval=True)
            # update check: soon after start, then periodically (off the main thread)
            self._runtime.schedule(self._check_version, 8)
            self._runtime.schedule(self._check_version, VERSION_CHECK_INTERVAL, interval=True)
            # air quality: after the USB wifi has settled post-boot, then periodically
            self._runtime.schedule(self._check_aqi, 30)
            self._runtime.schedule(self._check_aqi, AQI_CHECK_INTERVAL, interval=True)
            # weather alerts: staggered a little after AQI, then periodically
            self._runtime.schedule(self._check_alerts, 40)
            self._runtime.schedule(self._check_alerts, ALERTS_CHECK_INTERVAL, interval=True)
            # 7-day outlook: staggered after alerts, then hourly
            self._runtime.schedule(self._check_forecast, 50)
            self._runtime.schedule(self._check_forecast, FORECAST_CHECK_INTERVAL, interval=True)
            self.radar.start()
            return self._event


    def stop(self):
        """Fence callbacks and cancel all providers under the lifecycle lock."""
        with self._runtime.lock:
            self._runtime.running = False
            self.radar.stop()
            self._runtime.cancel_all()
            self._event = None

    def _load_previous_payload(self):
        """ The previous run's wx.json, if it holds a timed observation. A restart
        used to publish ~20 s of nulls before the first live observation (every
        value on the panel blinked to a dash); the kiosk wipes the browser profile
        on every start, so the page cannot bridge that. The engine can. """
        try:
            with open(self.output_path) as previous:
                data = json.load(previous)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and isinstance(data.get('obsTs'), (int, float)) else None

    def _carry_forward(self, payload, now):
        """ Fill still-null top-level fields from the previous run for the first
        CARRY_WINDOW_SEC after start, and while no live observation has arrived
        keep the previous obsTs so obsAgeSec is the REAL age: the page's
        freshness mark, not a dash, says how old the numbers are. Radar (its own
        'starting' state), alerts (they expire) and the clock are never carried. """
        carried = self._carried
        payload['carried'] = False
        if carried is None:
            return payload
        age = now - carried['obsTs']
        if not (0 <= age <= CARRY_MAX_SEC) or now - self._started_at > CARRY_WINDOW_SEC:
            self._carried = None
            return payload
        live = payload.get('obsTs') is not None
        for key, value in carried.items():
            if key in CARRY_SKIP or value is None or payload.get(key) is not None:
                continue
            payload[key] = value
            payload['carried'] = True
        if not live:
            payload['obsTs'] = int(carried['obsTs'])
            payload['obsAgeSec'] = int(age)
            payload['carried'] = True
        return payload

    # ---- attention tiers -------------------------------------------------


                # The executor drains active requests, whose successes enter LRU
                # even when the main worker has already detected supersession.


    def _check_version(self, _dt=None):
        """ Kick off a non-blocking GitHub version check on a daemon thread so a
        slow/failed request never stalls the Kivy main loop or the emit tick. """
        self._runtime.spawn('version', self._do_version_check)

    def _do_version_check(self):
        """ Compare the installed version to the latest GitHub release tag and
        cache the result. Never raises. """
        try:
            from lib.request_api import github_api
            from packaging import version as _v
            config = getattr(self.app, 'config', None)
            current = _cfg(config, 'System', 'Version')
            resp = github_api.version(config)
            if not github_api.verify_response(resp, 'tag_name'):
                return
            latest = resp.json()['tag_name']
            available = bool(current and latest) and (
                _v.parse(str(latest).lstrip('vV')) > _v.parse(str(current).lstrip('vV')))
            self._ver_result = _VerResult(available, latest, current)
        except Exception:                                                 # noqa: BLE001
            pass

    def _check_forecast(self, _dt=None):
        """ Kick off a non-blocking 7-day forecast fetch on a daemon thread. """
        self._runtime.spawn('forecast', self._do_forecast)

    def _do_forecast(self):
        """ Fetch the 7-day daily outlook from Open-Meteo for the station's
        lat/lon: hi/lo, WMO weather code, max precipitation probability. The
        temperature unit follows the console's own Units/Temp setting so the
        strip always matches the observed readings. The SAME call also carries
        hourly temperature (forecast_hours, no extra round trip) for the hero
        curve's forward trajectory. Off-thread, never raises; on failure the
        previous outlook is kept. """
        try:
            import urllib.request
            config = getattr(self.app, 'config', {}) or {}
            lat = _cfg(config, 'Station', 'Latitude')
            lon = _cfg(config, 'Station', 'Longitude')
            if not lat or not lon:
                return
            unit = 'fahrenheit' if (_cfg(config, 'Units', 'Temp') or 'c').lower() == 'f' else 'celsius'
            # Ask for precipitation in the unit the console already displays,
            # so the amount needs no conversion and cannot disagree with the
            # rainfall panel's rainUnit.
            precip_unit = 'inch' if (_cfg(config, 'Units', 'Precip') or 'mm').lower() == 'in' else 'mm'
            url = ('https://api.open-meteo.com/v1/forecast'
                   f'?latitude={lat}&longitude={lon}'
                   '&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,wind_gusts_10m_max'
                   # Hourly temperature for the hero curve. forecast_hours bounds
                   # the HOURLY block only (daily still spans forecast_days), and
                   # 48 h is the most the curve can want: at station-local
                   # midnight, "through the end of tomorrow" is exactly 48 hours.
                   '&hourly=temperature_2m&forecast_hours=48'
                   '&wind_speed_unit=kmh'
                   f'&precipitation_unit={precip_unit}'
                   f'&temperature_unit={unit}&forecast_days=7&timezone=auto')
            req = urllib.request.Request(url, headers={'User-Agent': 'WeatherAlmanac'})
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            days = self._fc_daily_from(data.get('daily') or {})
            hours = self._fc_hourly_from(data.get('hourly') or {}, time.time(),
                                         self._station_tz(config))
            if days:
                self._fc_result = _FcResult(days, hours, time.time())
        except Exception as error:                                        # noqa: BLE001
            Logger.warning(f'almanac_emit: forecast fetch failed - {error}')
        finally:
            # Boot resilience: the hourly interval is far too slow to recover
            # from a failed FIRST fetch (the USB wifi is often still settling
            # when the t+50s attempt fires - the same failure AQI's delayed
            # start works around). Until one fetch has succeeded, retry every
            # 2 minutes; after that the hourly cadence is plenty. One chain
            # only - _schedule_retry drops the request if one is already armed.
            if self._fc_ts is None:
                self._runtime.schedule_retry('forecast', self._check_forecast, FORECAST_RETRY_SEC)

    @staticmethod
    def _fc_daily_from(daily):
        """ Shape Open-Meteo's parallel daily arrays into display-ready rows:
        [{day:'MON', hi:93, lo:56, code:3, pp:20, qpf:0.34}, ...]. Rows with a missing
        hi or lo are dropped (a partial bar lies on the shared scale). Pure;
        never raises. """
        times = daily.get('time') or []
        his   = daily.get('temperature_2m_max') or []
        los   = daily.get('temperature_2m_min') or []
        codes = daily.get('weather_code') or []
        pps   = daily.get('precipitation_probability_max') or []
        # Amount, already in the console's precipitation unit (see the
        # precipitation_unit above), so no conversion here.
        qpfs  = daily.get('precipitation_sum') or []
        gusts = daily.get('wind_gusts_10m_max') or []
        out = []
        for i, t in enumerate(times[:7]):
            hi = _num(his[i]) if i < len(his) else None
            lo = _num(los[i]) if i < len(los) else None
            if hi is None or lo is None:
                continue
            try:
                day = datetime.fromisoformat(t).strftime('%a').upper()
            except (ValueError, TypeError):
                continue
            code = _num(codes[i]) if i < len(codes) else None
            pp   = _num(pps[i])   if i < len(pps)   else None
            qpf  = _num(qpfs[i]) if i < len(qpfs) else None   # _num: never raises on junk
            gust = _num(gusts[i]) if i < len(gusts) else None
            out.append({'day':  day,
                        'date': t[:10],
                        'hi':   int(round(hi)),
                        'lo':   int(round(lo)),
                        'code': int(round(code)) if code is not None else None,
                        'pp':   int(round(pp)) if pp is not None else None,
                        # two decimals covers both units; the board hides
                        # anything below what the unit can print
                        'qpf':  round(qpf, 2) if qpf is not None else None,
                        'gust': int(round(gust)) if gust is not None else None})   # km/h, fixed unit
        return out

    @staticmethod
    def _fc_hourly_from(hourly, now, tz):
        """ Shape Open-Meteo's hourly temperature into the hero curve's forward
        trajectory: [[epoch_seconds, temp], ...] from the CURRENT station-local
        hour through the end of TOMORROW (48 points at most). Temperatures are
        already in the console's unit (see temperature_unit on the request), so
        no conversion here - only a round to 1 decimal, matching the observed
        readings the curve is drawn against.

        Under timezone=auto the times come back local-naive, so the station tz
        is what turns them into epochs; without a tz there is no honest epoch to
        publish and the list stays empty (the console then draws no forecast
        segment at all rather than one an hour out of place). Pure; never raises. """
        times = hourly.get('time') or []
        temps = hourly.get('temperature_2m') or []
        if tz is None:
            return []
        try:
            ref = datetime.fromtimestamp(now, tz).replace(tzinfo=None)    # station wall clock
        except (OverflowError, OSError, ValueError):
            return []
        # Window on the wall clock, not on epochs: DST cannot make "the end of
        # tomorrow" a fixed number of hours away.
        first = ref.replace(minute=0, second=0, microsecond=0)
        last  = ref.date() + timedelta(days=1)
        out = []
        for t, v in zip(times, temps):
            v = _num(v)                               # _num: never raises on junk
            if v is None:
                continue
            try:
                dt = datetime.fromisoformat(t)
            except (ValueError, TypeError):
                continue
            if dt.tzinfo is not None:                 # not what timezone=auto sends; don't mix clocks
                dt = dt.astimezone(tz).replace(tzinfo=None)
            if dt < first or dt.date() > last:
                continue
            try:
                epoch = tz.localize(dt).timestamp()
            except Exception:                                             # noqa: BLE001
                continue                              # ambiguous/nonexistent local hour (DST edge)
            out.append([int(round(epoch)), round(v, 1)])
        return out[:48]

    WINDY_GUST_KMH = 45   # ~28 mph gusts: the day is a wind story

    @classmethod
    def _tomorrow_hint(cls, fc_rows):
        """ One quiet line about tomorrow, only when tomorrow is a story:
        "Thunderstorms tomorrow" / "Snow tomorrow" / "Rain tomorrow" /
        "Windy tomorrow" / "Fog tomorrow". Ordinary days say nothing -
        absence is information. Selects the tomorrow row by its station-local
        calendar date, never its position in a partially shaped array. Never raises. """
        try:
            today = next((row for row in fc_rows if row.get('today')), None)
            if today is None:
                return None
            today_date = today.get('date')
            if today_date:
                tomorrow_date = (datetime.fromisoformat(today_date).date() + timedelta(days=1)).isoformat()
                t = next((row for row in fc_rows if row.get('date') == tomorrow_date), None)
            else:
                # Compatibility for callers that predate the date field: select
                # by weekday label, not adjacent list position.
                names = ('MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN')
                try:
                    tomorrow_day = (names.index(today.get('day')) + 1) % len(names)
                except ValueError:
                    return None
                t = next((row for row in fc_rows if row.get('day') == names[tomorrow_day]), None)
            if t is None:
                return None
            code, gust = t.get('code'), t.get('gust')
            if code is not None and code >= 95:
                return 'Thunderstorms tomorrow'
            windy = gust is not None and gust >= cls.WINDY_GUST_KMH
            if code in (71, 73, 75, 77, 85, 86):
                return 'Blowing snow tomorrow' if windy else 'Snow tomorrow'
            if code in (56, 57, 66, 67):
                return 'Freezing rain tomorrow'   # the one rain worth distinguishing: it's a hazard
            if code in (51, 53, 55, 61, 63, 65, 80, 81, 82):
                return 'Wind-driven rain tomorrow' if windy else 'Rain tomorrow'
            if gust is not None and gust >= cls.WINDY_GUST_KMH:
                return 'Windy tomorrow'
            if code in (45, 48):
                return 'Fog tomorrow'
        except Exception:                                                 # noqa: BLE001
            pass
        return None

    # The core's barometer outlook sentences are too long for the ledger row
    # ("Becoming clearer and cooler" wrapped, then overran). The vocabulary is
    # closed, so map to the compact editorial forms the design contract always
    # showed ("Unchanged"); unknown strings pass through untouched.
    _OUTLOOK_COMPACT = {
        'Conditions unchanged':        'Unchanged',
        'Fair conditions likely':      'Fair conditions',
        'Rainy conditions likely':     'Rain likely',
        'Stormy conditions likely':    'Storm likely',
        'Becoming clearer and cooler': 'Clearer, cooler',
        'Becoming cloudy and warmer':  'Cloudier, warmer',
    }

    @staticmethod
    def _rain_starting(status, precip_start, obs_ts, now, received=None):
        """ The Tempest's evt_precip arrives the moment its sensor feels rain,
        up to a minute before the next obs_st carries any. Between the event
        and that observation a dry status reads 'Rain Starting'; the
        observation then governs, even if it measured nothing. Bounded by
        RAIN_START_HOLD_SEC if observations stop. Never raises. """
        try:
            if status != 'Currently Dry' or precip_start is None:
                return status
            if obs_ts is not None and obs_ts >= precip_start:
                return status
            # Ordering uses the station's clock; expiry uses the Pi's clock.
            # Older Obs holders without receipt metadata retain epoch expiry.
            if not 0 <= now - (received if received is not None else precip_start) <= RAIN_START_HOLD_SEC:
                return status
            return 'Rain Starting'
        except TypeError:
            return status

    @staticmethod
    def _snowify_status(status, temp, temp_unit, fc_rows):
        """ The Tempest's haptic rain sensor cannot register snowfall, so in
        freezing weather with snow in today's forecast, 'Currently Dry' is
        the sensor's truth but not the sky's. Rewrites ONLY the dry status -
        any measured rain always wins. Never raises. """
        if status != 'Currently Dry' or temp is None:
            return status
        try:
            freezing = float(temp) <= (34.0 if (temp_unit or '').endswith('F') else 1.0)
        except (TypeError, ValueError):
            return status
        if not freezing:
            return status
        today = next((r for r in (fc_rows or []) if r.get('today')), None)
        if today and today.get('code') in (71, 72, 73, 74, 75, 76, 77, 85, 86):
            return 'Snow Likely'   # forecast-derived; the haptic sensor cannot see snow
        return status

    @staticmethod
    def _rain_rate_display(Obs, config, eff_mm=None):
        """ Numeric rain rate in display units. Index [0] is the core's
        FORMATTED display value, which for a trace rate is the STRING
        '<0.01' (in/hr) / '<0.1' (mm/hr) - unparseable, so the overlay
        showed a dash and hid the water while it was actually drizzling.
        Fall back to converting the raw mm/hr at [3] by the configured
        precip unit. When the rolling window lifts the rate above the raw
        minute (eff_mm > raw), that effective rate is what gets converted,
        so the readout agrees with the gauge. Never raises. """
        raw_mm = _num(_idx(Obs.get('RainRate'), 3))
        bridged = eff_mm is not None and raw_mm is not None and eff_mm > raw_mm
        if not bridged:
            rate = _num(_idx(Obs.get('RainRate'), 0))
            if rate is not None:
                return rate
            if raw_mm is None:
                return None
        mm = eff_mm if bridged else raw_mm
        unit = (_cfg(config, 'Units', 'Precip') or 'mm').lower()
        per_mm = {'in': 1 / 25.4, 'cm': 0.1, 'mm': 1.0}.get(unit, 1.0)
        return round(mm * per_mm, 4)

    @staticmethod
    def _rain_status_for(eff_mm, core_status):
        """ The core's intensity word, except that a minute the sensor calls
        'Currently Dry' inside a drizzle (window rate > 0) gets the word for
        the windowed rate - the same bands derived_variables.rain_rate uses. """
        if core_status != 'Currently Dry' or eff_mm is None or eff_mm <= 0:
            return core_status
        if eff_mm < 0.25: return 'Very Light Rain'
        if eff_mm < 1.0:  return 'Light Rain'
        if eff_mm < 4.0:  return 'Moderate Rain'
        if eff_mm < 16.0: return 'Heavy Rain'
        if eff_mm < 50.0: return 'Very Heavy Rain'
        return 'Extreme Rain'

    @staticmethod
    def _feels_desc(text):
        """ The core's feels-like descriptors all begin "Feeling ..."; the hero
        line already says "Feels like 62°", so the prefix doubles up on glass
        ("Feels like 62° · Feeling warm"). Drop it and keep the sentence case. """
        if not text:
            return text
        stripped = re.sub(r'^\s*Feeling\s+', '', text)
        return stripped[:1].upper() + stripped[1:] if stripped else text

    @staticmethod
    def _unify_today(fc_rows, fc_low, fc_high):
        """ The hero's LOW/HIGH come from WeatherFlow while the outlook band's
        rows come from Open-Meteo, and the two providers disagree by a degree
        or two - which reads as a contradiction when both sit on one screen
        labelled "today". One provider owns today: the band's TODAY row takes
        the WeatherFlow figures whenever they are known. Other days untouched. """
        for r in fc_rows:
            if r.get('today'):
                if fc_low is not None:
                    r['lo'] = int(round(fc_low))
                if fc_high is not None:
                    r['hi'] = int(round(fc_high))
                lo, hi = r.get('lo'), r.get('hi')
                if lo is not None and hi is not None and lo > hi:
                    r['lo'], r['hi'] = hi, lo
        return fc_rows

    def _fc_daily_current(self, today_iso, fc_daily=None):
        """ The stored outlook with any already-past days dropped, so a stale
        forecast (wifi out for a day+) can never mislabel yesterday as TODAY.
        As rows age out the band naturally shrinks below the HTML's 3-day
        minimum and hides itself - no separate staleness flag needed.
        `fc_daily` defaults to the live snapshot; the emit tick passes the one
        it already read so the rows and the staleness flag agree. """
        rows = [dict(r) for r in (self._fc_daily if fc_daily is None else fc_daily)
                if not r.get('date') or r['date'] >= today_iso]
        for r in rows:
            r['today'] = (r.get('date') == today_iso)
        return rows

    def _check_aqi(self, _dt=None):
        """ Kick off a non-blocking air-quality fetch on a daemon thread. """
        self._runtime.spawn('aqi', self._do_aqi)

    @staticmethod
    def _aqi_cat(aqi):
        """ US EPA AQI category name. """
        if aqi <= 50:   return 'Good'
        if aqi <= 100:  return 'Moderate'
        if aqi <= 150:  return 'Sensitive'          # "Unhealthy for Sensitive Groups"
        if aqi <= 200:  return 'Unhealthy'
        if aqi <= 300:  return 'Very Unhealthy'
        return 'Hazardous'

    def _do_aqi(self):
        """ Fetch US AQI for the station's lat/lon.

        When [AirQuality] WaqiToken is set in wfpiconsole.ini, uses the WAQI
        API (aqicn.org) which aggregates the nearest EPA/AirNow monitoring
        station — the same reading shown on airnow.gov.

        Falls back to Open-Meteo (CAMS satellite model, no token required)
        when no token is configured.  Off-thread, never raises. """
        try:
            import urllib.request
            config = getattr(self.app, 'config', None)
            lat   = _cfg(config, 'Station', 'Latitude')
            lon   = _cfg(config, 'Station', 'Longitude')
            if not lat or not lon:
                return
            token = (_cfg(config, 'AirQuality', 'WaqiToken') or '').strip()
            if token:
                # WAQI: nearest EPA/AirNow monitoring station
                url = f'https://api.waqi.info/feed/geo:{lat};{lon}/?token={token}'
                req = urllib.request.Request(url, headers={'User-Agent': 'WeatherAlmanac'})
                with urllib.request.urlopen(req, timeout=25) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                if data.get('status') != 'ok':
                    Logger.warning(f'almanac_emit: WAQI status={data.get("status")} '
                                   f'msg={data.get("data")}')
                    return
                d   = data.get('data') or {}
                aqi = _num(d.get('aqi'))
                if aqi is None:
                    return                  # station reports '-' when sensor is offline
                aqi = int(round(aqi))
                fc_pm25 = ((d.get('forecast') or {}).get('daily') or {}).get('pm25') or []
                series, peak, peak_time, trend, trend_text, fc_cat = \
                    self._waqi_trend(aqi, fc_pm25, self._station_today(config))
                # WAQI's iaqi.pm25.v is a pollutant AQI, not a concentration, so
                # there is no PM2.5 reading to publish from this provider.
                self._aqi_result = _AqiResult(aqi, self._aqi_cat(aqi), None, time.time(),
                                              series, peak, peak_time, fc_cat, trend, trend_text)
            else:
                # Open-Meteo fallback: CAMS model, no token required
                url = ('https://air-quality-api.open-meteo.com/v1/air-quality'
                       f'?latitude={lat}&longitude={lon}&current=us_aqi,pm2_5'
                       '&hourly=us_aqi,pm2_5&forecast_days=1&timezone=auto')
                req = urllib.request.Request(url, headers={'User-Agent': 'WeatherAlmanac'})
                with urllib.request.urlopen(req, timeout=25) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                cur = data.get('current') or {}
                aqi = _num(cur.get('us_aqi'))
                if aqi is None:
                    return
                aqi = int(round(aqi))
                series, peak, peak_time, trend, trend_text, fc_cat = \
                    self._aqi_forecast_summary(data.get('hourly') or {}, time.time(),
                                               self._station_tz(config), aqi, _clock_style(config))
                self._aqi_result = _AqiResult(aqi, self._aqi_cat(aqi), _num(cur.get('pm2_5')),
                                              time.time(), series, peak, peak_time,
                                              fc_cat, trend, trend_text)
        except Exception as error:                                        # noqa: BLE001
            Logger.warning(f'almanac_emit: air-quality fetch failed - {error}')

    @staticmethod
    def _hour_label(dt, style='12 hr'):
        """ "5 PM" / "5:30 PM" (12 hr) or "17:00" / "17:30" (24 hr) from a datetime. """
        return _clock(dt, style, sparse=True)

    @staticmethod
    def _aqi_forecast_summary(hourly, now, tz, aqi_now, style='12 hr'):
        """ From Open-Meteo hourly us_aqi (local-naive ISO times + the station tz),
        build the next-hours series, the 6 h peak, and a rising/falling/steady
        trend (5-AQI deadband, band-crossing required). Pure; never raises.
        Returns (series, peak, peak_time, trend, trend_text, peak_cat). """
        times = hourly.get('time') or []
        vals  = hourly.get('us_aqi') or []
        pts = []
        for t, v in zip(times, vals):
            v = _num(v)
            if v is None:
                continue
            try:
                dt = datetime.fromisoformat(t)
            except (ValueError, TypeError):
                continue
            if dt.tzinfo is None and tz is not None:
                try:
                    dt = tz.localize(dt)
                except Exception:                                         # noqa: BLE001
                    dt = dt.replace(tzinfo=timezone.utc)
            epoch = dt.timestamp() if dt.tzinfo else None
            pts.append((epoch, dt, int(round(v))))
        future = [(e, d, v) for (e, d, v) in pts if e is None or e >= now - 1800][:12]
        if not future:
            return [], None, None, None, None, None
        series = [[int(e), v] for (e, d, v) in future if e is not None]
        window = [(e, d, v) for (e, d, v) in future if e is None or e <= now + 6 * 3600] or future
        _, peak_dt, peak = max(window, key=lambda x: x[2])
        peak_cat  = AlmanacEmitter._aqi_cat(peak)
        peak_time = AlmanacEmitter._hour_label(peak_dt, style)
        base = aqi_now if aqi_now is not None else future[0][2]
        low  = min(v for (_, _, v) in future)
        if peak - base >= 5 and peak_cat != AlmanacEmitter._aqi_cat(base):
            trend, trend_text = 'rising', f'{peak_cat} by {peak_time}'
        elif base - low >= 5 and AlmanacEmitter._aqi_cat(low) != AlmanacEmitter._aqi_cat(base):
            trend, trend_text = 'falling', 'Improving'
        else:
            trend, trend_text = 'steady', None
        return series, peak, peak_time, trend, trend_text, peak_cat

    @staticmethod
    def _waqi_trend(aqi_now, fc_pm25_daily, today_iso):
        """ Derive rising/falling/steady from WAQI's dated daily PM2.5 forecast.
        Returns the same 6-tuple as _aqi_forecast_summary so callers are
        unchanged.  No hourly series, so aqiForecast sparkline is empty. """
        if not fc_pm25_daily or aqi_now is None or not today_iso:
            return [], None, None, None, None, None
        try:
            tomorrow_iso = (datetime.fromisoformat(today_iso).date() + timedelta(days=1)).isoformat()
        except (TypeError, ValueError):
            return [], None, None, None, None, None
        today = next((row for row in fc_pm25_daily if row.get('day') == today_iso), {})
        tomorrow = next((row for row in fc_pm25_daily if row.get('day') == tomorrow_iso), {})
        peak_raw = _num(today.get('max'))
        if peak_raw is None:
            return [], None, None, None, None, None
        peak     = int(round(peak_raw))
        peak_cat = AlmanacEmitter._aqi_cat(peak)
        cur_cat  = AlmanacEmitter._aqi_cat(aqi_now)
        nxt_avg  = _num(tomorrow.get('avg'))
        if peak - aqi_now >= 5 and peak_cat != cur_cat:
            return [], peak, None, 'rising',  f'{peak_cat} today', peak_cat
        if nxt_avg is not None:
            nxt = int(round(nxt_avg))
            if aqi_now - nxt >= 5 and AlmanacEmitter._aqi_cat(nxt) != cur_cat:
                return [], peak, None, 'falling', 'Improving', peak_cat
        return [], peak, None, 'steady', None, peak_cat

    # --------------------------------------------------------------------
    # Weather alerts (NWS api.weather.gov, by station lat/lon)
    # --------------------------------------------------------------------
    def _check_alerts(self, _dt=None):
        """ Kick off a non-blocking NWS alerts fetch on a daemon thread. """
        self._runtime.spawn('alerts', self._do_alerts)

    def _do_alerts(self):
        """ Fetch active NWS alerts for the station's lat/lon. Off-thread, never
        raises; keeps the last-good list on a transient failure (staleness is
        flagged in the payload rather than silently shown as fresh).

        NWS (api.weather.gov) only covers the US and its territories. A point
        outside that coverage returns HTTP 400 "out of bounds" (400/404) — that
        is NOT a failure, it just means there are no NWS alerts here, so we clear
        the list and mark it freshly-fetched (no repeated warnings, never stale).
        Non-US stations therefore simply show no alert strip; the AQI block still
        works worldwide (Open-Meteo computes us_aqi globally). """
        try:
            import urllib.request
            import urllib.error
            config = getattr(self.app, 'config', None)
            lat = _cfg(config, 'Station', 'Latitude')
            lon = _cfg(config, 'Station', 'Longitude')
            if not lat or not lon:
                return
            contact = (_cfg(config, 'Station', 'Contact')
                       or os.environ.get('ALMANAC_CONTACT') or ALERTS_UA_FALLBACK)
            url = f'https://api.weather.gov/alerts/active?point={lat},{lon}'
            req = urllib.request.Request(url, headers={
                'User-Agent': contact, 'Accept': 'application/geo+json'})
            try:
                with urllib.request.urlopen(req, timeout=ALERTS_TIMEOUT) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
            except urllib.error.HTTPError as http_error:
                if http_error.code in (400, 404):
                    # outside NWS coverage (non-US) — benign: no alerts here
                    self._alerts_result = _AlertsResult([], [], time.time())
                    return
                raise
            feats = [f.get('properties') or {} for f in (data.get('features') or [])]
            now = time.time()
            self._alerts_result = _AlertsResult(
                feats, self._process_alerts(feats, now, self._station_tz(config)), now)
        except Exception as error:                                        # noqa: BLE001
            Logger.warning(f'almanac_emit: alerts fetch failed - {error}')

    # --------------------------------------------------------------------
    # Warning polygons for the radar map (NWS storm-based warnings by area)
    # --------------------------------------------------------------------


    @staticmethod
    def _alert_level(event):
        """ NWS product level from the last word of the event name. 'Alert' products
        bucket as advisory-tier; unknown products as statement-tier. Returns
        (level_int, level_str) with higher int = more urgent. """
        words = (event or '').strip().lower().split()
        last = words[-1] if words else ''
        if last in _ALERT_LEVEL:
            lvl = _ALERT_LEVEL[last]
        else:
            # products like "Small Craft Advisory for Hazardous Seas" or
            # "911 Telephone Outage Emergency" carry their tier mid-name;
            # take the most urgent tier word found anywhere, else statement
            found = [_ALERT_LEVEL[w] for w in words if w in _ALERT_LEVEL]
            lvl = max(found) if found else 1
        return lvl, _ALERT_LEVELNAME[lvl]

    @staticmethod
    def _alert_tone(event, level):
        """ Banner colour token, derived from the level with a 2-rule hazard override:
        (A) air-quality/smoke/red-flag/fire-weather pin to amber at any level;
        (B) routine water advisories (wind/flood/winter/fog/coastal/gale/surf,
        level <= advisory) cool to blue instead of shouting amber. """
        e = (event or '').lower()
        if any(k in e for k in ('air quality', 'smoke', 'red flag', 'fire weather')):
            return 'brass'                              # override A
        if level <= 2 and any(k in e for k in ('wind', 'flood', 'winter', 'fog', 'coastal', 'gale', 'surf')):
            return 'water'                              # override B
        if level >= 4:
            return 'accent'                             # warning
        if level >= 2:
            return 'brass'                              # watch / advisory / alert
        return 'verdigris'                              # statement / outlook / unknown

    @staticmethod
    def _event_class(event):
        """ Hazard family for the label/nuance; colour comes from the level. """
        e = (event or '').lower()
        for sub, cls in _EVENT_CLASS_MAP:
            if sub in e:
                return cls
        return 'default'

    @staticmethod
    def _to_epoch(iso):
        """ ISO-8601 (with or without offset) -> epoch seconds; None if unparsable. """
        if not iso:
            return None
        try:
            dt = datetime.fromisoformat(iso)
        except (ValueError, TypeError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()

    @staticmethod
    def _until_text(epoch, tz, style='12 hr'):
        """ Glanceable station-local end time, "Wed 5 PM" or "Wed 17:00". None if unknown. """
        if epoch is None or tz is None:
            return None
        try:
            dt = datetime.fromtimestamp(epoch, tz)
        except (ValueError, OSError, OverflowError):
            return None
        return f"{dt.strftime('%a')} {AlmanacEmitter._hour_label(dt, style)}"

    @staticmethod
    def _split_counties(area_desc):
        """ "King, WA; Kitsap, WA; …" -> ['King', 'Kitsap', …] (deduped, ordered). """
        out, seen = [], set()
        for seg in (area_desc or '').split(';'):
            name = seg.split(',')[0].strip()
            if name and name not in seen:
                seen.add(name)
                out.append(name)
        return out

    @staticmethod
    def _areas_short(counties):
        """ ['King','Kitsap','Pierce','Snohomish','Thurston'] -> 'King, Kitsap, Pierce +2'. """
        if not counties:
            return None
        head = counties[:3]
        extra = len(counties) - len(head)
        return ', '.join(head) + (f' +{extra}' if extra > 0 else '')

    @staticmethod
    def _extract_reason(desc):
        """ Best-effort "…for wildfire smoke has been issued…" -> "Wildfire smoke".
        Returns None when not confidently extractable. """
        if not desc:
            return None
        match = re.search(r'\bfor ([a-z][a-z \-]{2,40}?) (?:has been|have been|is|are) '
                          r'(?:issued|in effect)', desc, re.I)
        if not match:
            return None
        reason = match.group(1).strip()
        return reason[:1].upper() + reason[1:]

    def _process_alerts(self, feats, now, tz):
        """ Raw NWS `properties` dicts -> the wx.json alert list: drop expired,
        classify by product level, COLLAPSE identical events (union of counties,
        soonest end), sort by level then soonest end, cap the count. Pure given its
        inputs (no network); never raises. """
        style = _clock_style(getattr(getattr(self, 'app', None), 'config', {}) or {})
        groups = {}
        for prop in feats:
            end = prop.get('ends') or prop.get('expires')
            expires = self._to_epoch(end)
            if expires is not None and expires < now:              # expired
                continue
            event = prop.get('event') or 'Weather Alert'
            level, level_name = self._alert_level(event)
            key = event.strip().lower()                            # collapse only IDENTICAL products
            cand = {
                'event': event, 'eventClass': self._event_class(event),
                'level': level_name, 'tone': self._alert_tone(event, level), 'priority': level,
                'short': self._extract_reason(prop.get('description')),
                'onset': self._to_epoch(prop.get('onset')),
                'until': expires, 'untilText': self._until_text(expires, tz, style),
                'headline': (prop.get('headline') or '')[:160],
                '_areaset': self._split_counties(prop.get('areaDesc') or ''),
            }
            group = groups.get(key)
            if group is None:
                groups[key] = cand
            else:
                for county in cand['_areaset']:
                    if county not in group['_areaset']:
                        group['_areaset'].append(county)
                group['short'] = group['short'] or cand['short']
                if cand['until'] is not None and (group['until'] is None or cand['until'] < group['until']):
                    group['until'] = cand['until']
                    group['untilText'] = cand['untilText']
        out = []
        for group in groups.values():
            group['areaShort'] = self._areas_short(group.pop('_areaset'))
            out.append(group)
        out.sort(key=lambda a: (-a['priority'], a['until'] if a['until'] is not None else 9e18))
        return out[:ALERT_MAX]

    # --------------------------------------------------------------------
    def _emit(self, dt):
        """ Clock callback. Never allowed to raise - a broken/late DictProperty
        must not crash the almanac timer loop. """
        self.radar.before_emit()
        try:
            payload = self._build_payload()
            self._write_atomic(payload)
            self._warned = False                     # recovered: re-arm the failure log
        except Exception as error:                                       # noqa: BLE001
            if not self._warned:
                Logger.warning(f'almanac_emit: emit failed - {error}')
                self._warned = True
        self.radar.write_health(time.time())

    def _write_atomic(self, payload):
        """ Write temp file + os.replace so the HTML reader never observes a
        partially written file. """
        directory = os.path.dirname(self.output_path) or '.'
        os.makedirs(directory, exist_ok=True)
        tmp_path = f'{self.output_path}.tmp.{os.getpid()}'
        with open(tmp_path, 'w') as tmp_file:
            json.dump(payload, tmp_file, allow_nan=False)
        os.replace(tmp_path, self.output_path)

    # --------------------------------------------------------------------
    _SLP_FROM_MB = {'mb': (1.0, 1), 'hpa': (1.0, 1), 'inhg': (0.0295301, 3), 'mmhg': (0.750063, 2)}

    def _baro_series(self):
        """ 24 h sea-level-pressure trace for the barograph, downsampled to
        ~48 points [[epoch_s, slp], ...] oldest->newest, in the station's
        configured pressure unit (the same unit as 'slp', so the trace's hi/lo
        numerals and the big reading can never be in two unit systems).

        Sourced from the core's cached WeatherFlow REST 24 h obs
        (app.obsParser.api_data[device]['24Hrs']) — the same payload the core
        already uses for SLPTrend/Max/Min, so no extra network calls. Fully
        self-contained (no upstream files touched) and guarded so a missing or
        malformed payload just yields [] (HTML then hides the barograph).
        Cached for BARO_SERIES_TTL s: the 24 h data changes slowly and the JSON
        is large, so we must not re-parse it every 2 s emit. """
        BARO_SERIES_TTL = 300.0
        now = time.time()
        if self._baro_series_cache and (now - self._baro_series_t) < BARO_SERIES_TTL:
            return self._baro_series_cache
        series = []
        try:
            from lib import derived_variables as derive
            config   = self.app.config
            parser   = getattr(self.app, 'obsParser', None)
            api_data = getattr(parser, 'api_data', None) or {}
            st       = config['Station']
            device, idx = None, None
            for dev, blob in api_data.items():
                if not isinstance(blob, dict) or not blob.get('24Hrs'):
                    continue                     # None = the REST call failed; ordinary, not an error
                if str(dev) in (st['OutAirID'], st['OutAirSN']):
                    device, idx = dev, 1                    # AIR pressure bucket
                    break
                if str(dev) in (st['TempestID'], st['TempestSN']):
                    device, idx = dev, 6                    # TEMPEST pressure bucket
                    break
            if device is not None:
                obs = (api_data[device]['24Hrs'].json() or {}).get('obs') or []
                raw = [(ob[0], ob[idx]) for ob in obs
                       if ob and ob[0] is not None and len(ob) > idx and ob[idx] is not None]
                raw.sort(key=lambda p: p[0])
                # derive.SLP always answers in mb; the core's observation_format
                # converts to the configured unit with these factors/precisions
                unit = (_cfg(config, 'Units', 'Pressure') or 'mb').lower()
                factor, places = self._SLP_FROM_MB.get(unit, (1.0, 1))
                for t, p in raw:
                    slp = derive.SLP([p, 'mb'], device, config)[0]
                    slp = _num(slp)
                    timestamp = _num(t)
                    if slp is not None and timestamp is not None:
                        value = _num(slp * factor)
                        if value is not None:
                            series.append([int(timestamp), round(value, places)])
                target = 48
                if len(series) > target:
                    step   = (len(series) - 1) / (target - 1)
                    series = [series[int(round(i * step))] for i in range(target)]
        except Exception as error:                                            # noqa: BLE001
            if not getattr(self, '_baro_warned', False):
                Logger.warning(f'almanac_emit: barograph series unavailable - {error}')
                self._baro_warned = True         # one line per outage, not one per refresh
            series = []
        if series:
            self._baro_warned = False
        self._baro_series_cache = series
        self._baro_series_t     = now
        return series

    # --------------------------------------------------------------------
    def _build_payload(self):
        Obs    = getattr(self.screen, 'Obs', {})    or {}
        Astro  = getattr(self.screen, 'Astro', {})  or {}
        Met    = getattr(self.screen, 'Met', {})    or {}
        Sager  = getattr(self.screen, 'Sager', {})  or {}
        config = getattr(self.app, 'config', {})    or {}

        tz = self._station_tz(config)
        style = _clock_style(config)
        now_local = datetime.now(pytz.utc).astimezone(tz) if tz else datetime.now()

        sunrise_txt = _clock_case(_text(_idx(Astro.get('Sunrise'), 1)))
        sunset_txt  = _clock_case(_text(_idx(Astro.get('Sunset'), 1)))
        sun_frac, daylight_txt, till_sunset_txt = self._sun_fraction(sunrise_txt, sunset_txt, now_local, tz)

        rapid_dir = Obs.get('rapidDir')
        wind_dir  = Obs.get('WindDir')
        wind_dir_deg = _num(_idx(rapid_dir, 0))
        if wind_dir_deg is None:
            wind_dir_deg = _num(_idx(wind_dir, 0))
        wind_cardinal = _text(_idx(rapid_dir, 2)) or _text(_idx(wind_dir, 2)) \
            or _cardinal_from_degrees(wind_dir_deg)

        met_wind_dir = Met.get('WindDir')
        fc_wind_spd  = _num(_idx(Met.get('WindSpd'), 0))
        fc_wind_unit = _text(_idx(Met.get('WindSpd'), 1))
        fc_wind_card = _text(_idx(met_wind_dir, 2)) or _cardinal_from_degrees(_num(_idx(met_wind_dir, 0)))
        fc_wind = None
        if fc_wind_spd is not None:
            parts = [f'{fc_wind_spd:g}']
            if fc_wind_unit:
                parts.append(fc_wind_unit)
            if fc_wind_card:
                parts.append(fc_wind_card)
            fc_wind = ' '.join(parts)

        now = time.time()
        # FRESHNESS. 'ts' below is only the engine heartbeat: it proves the emit
        # tick ran, nothing about the data it carried. An observation's own
        # epoch is the only evidence the station is still reporting, and a
        # strike's epoch the only evidence the lightning readout is current -
        # the formatted display values carry neither, so they are read here from
        # the raw epochs the parser publishes alongside them.
        obs_ts  = _num(Obs.get('obsTs'))
        # A station that has never reported since the engine started is as
        # silent as one that stopped: age it from engine start, so a restart
        # cannot reset a dead sensor to "healthy" (obsTs stays null).
        obs_age = _age_sec(obs_ts if obs_ts is not None else self._started_at, now)

        strike_delta_t = Obs.get('StrikeDeltaT')
        strike_ts = _num(Obs.get('strikeTs'))
        if strike_ts is not None:
            lightning_since_sec = _age_sec(strike_ts, now)
            lightning_last      = _ago_text(lightning_since_sec)
        else:
            # No epoch published (a parser reset, or a build without it): the
            # core's formatted delta is frozen at the moment it was calculated,
            # but with no epoch it is the only source there is.
            lightning_since_sec = _num(_idx(strike_delta_t, 4))
            lightning_last      = _since_ago_text(strike_delta_t)
        lightning_active = self._lightning_active(config, lightning_since_sec)

        temp_val  = _num(_idx(Obs.get('outTemp'), 0))
        temp_unit = _temp_unit(_idx(Obs.get('outTemp'), 1))
        rain_raw_mm = _num(_idx(Obs.get('RainRate'), 3))
        rain_eff_mm = self._rain_win.effective(now, rain_raw_mm)
        fc_low    = _num(_idx(Met.get('lowTemp'), 0))
        fc_high   = _num(_idx(Met.get('highTemp'), 0))
        # One read of each provider snapshot per tick: every field below comes
        # from the same fetch, so the payload can never mix old and new.
        aqi_snap    = self._aqi_result
        fc_snap     = self._fc_result
        alerts_snap = self._alerts_result
        radar_payload = self.radar.payload_snapshot(now, tz, style)
        ver_snap    = self._ver_result
        fc_rows   = self._unify_today(
                        self._fc_daily_current(now_local.strftime('%Y-%m-%d'), fc_snap.daily),
                        fc_low, fc_high)

        # Alerts expire between fetches, so the last-good raw features are
        # re-filtered here rather than trusted as processed at fetch time.
        alerts = (alerts_snap.alerts if alerts_snap.features is None
                  else self._process_alerts(alerts_snap.features, now, tz))

        payload = {
            'radar': radar_payload,
            'ts':      int(now),                     # engine heartbeat ONLY - see obsAgeSec
            'obsTs':     int(obs_ts) if obs_ts is not None else None,
            'obsAgeSec': obs_age,                    # age of the newest OUTDOOR observation
            'station': _text(_cfg(config, 'Station', 'Name')),
            'locationLine': self._location_line(config),
            'updateAvailable': ver_snap.available,
            'latestVersion':   ver_snap.latest,
            'currentVersion':  ver_snap.current,
            'date':    now_local.strftime('%a, %d %b %Y'),
            'time':    _clock(now_local, style),
            # station-local midnight as an epoch: the hero curve maps hourly epochs
            # onto the day's axis with this, exact to the second (HH:MM cannot be)
            'dayStartTs': int(now_local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()),

            # Temperature
            'temp':            temp_val,
            'tempUnit':        temp_unit,
            'feelsLike':       _num(_idx(Obs.get('FeelsLike'), 0)),
            'feelsDesc':       self._feels_desc(_text(_idx(Obs.get('FeelsLike'), 2))),
            'tempTrendPerHr':  _num(_idx(Obs.get('outTempTrend'), 0)),
            'temp24hDelta':    _num(_idx(Obs.get('outTempDiff'), 0)),
            'obsLow':          _num(_idx(Obs.get('outTempMin'), 0)),
            'obsLowTime':      _clock_case(_text(_idx(Obs.get('outTempMin'), 2))),
            'obsHigh':         _num(_idx(Obs.get('outTempMax'), 0)),
            'obsHighTime':     _clock_case(_text(_idx(Obs.get('outTempMax'), 2))),
            'fcLow':           fc_low,
            'fcHigh':          fc_high,
            'humidity':        _num(_idx(Obs.get('Humidity'), 0)),
            'dewPoint':        _num(_idx(Obs.get('DewPoint'), 0)),

            # Conditions / short-term forecast
            'conditions':      _clock_case(_text(Met.get('Conditions'))),
            'conditionsNote':  self._tomorrow_hint(fc_rows),   # "Rain tomorrow" etc; None on quiet days
            'fcHour':          _clock_case(_text(Met.get('Valid'))),
            'fcWind':          fc_wind,
            'fcPrecipPct':     _num(_idx(Met.get('PrecipPercnt'), 0)),
            'fcDailyPct':      _num(_idx(Met.get('PrecipDay'), 0)),
            'fcDaily':         fc_rows,   # outlook, past days dropped at emit time
            # Hourly trajectory for the hero curve. Same snapshot as fcDaily, so
            # the curve and the band can never come from different fetches. The
            # console drops points that are no longer in the future, which is
            # also what makes a stale list (wifi out) draw nothing at all.
            'fcHourly':        list(fc_snap.hourly or ()),
            'fcStale':         (fc_snap.ts is None) or (now - fc_snap.ts) > FC_STALE_SEC,
            'fcAgeSec':        _age_sec(fc_snap.ts, now),   # since the last SUCCESSFUL fetch

            # Wind
            'windSpd':      _num(_idx(Obs.get('WindSpd'), 0)),
            'windUnit':     _text(_idx(Obs.get('WindSpd'), 1)),
            'windAvg':      _num(_idx(Obs.get('AvgWind'), 0)),
            'windGust':     _num(_idx(Obs.get('WindGust'), 0)),
            'windMax':      _num(_idx(Obs.get('MaxGust'), 0)),
            'windDir':      wind_dir_deg,
            'windCardinal': wind_cardinal,
            'windStatus':   _wind_desc(_idx(Obs.get('WindSpd'), 4)),   # Beaufort description, not the force number at [2]

            # Barometer
            'slp':            _num(_idx(Obs.get('SLP'), 0)),
            'slpUnit':        _text(_idx(Obs.get('SLP'), 1)),
            'slpTrendPerHr':  _num(_idx(Obs.get('SLPTrend'), 0)),
            'slpTrendDesc':   _text(_idx(Obs.get('SLPTrend'), 2)),
            'slp24High':      _num(_idx(Obs.get('SLPMax'), 0)),
            'slp24HighTime':  _clock_case(_text(_idx(Obs.get('SLPMax'), 2))),
            'slp24Low':       _num(_idx(Obs.get('SLPMin'), 0)),
            'slp24LowTime':   _clock_case(_text(_idx(Obs.get('SLPMin'), 2))),
            'slpOutlook':      self._OUTLOOK_COMPACT.get(
                                   _text(_idx(Obs.get('SLPTrend'), 3)) or '',
                                   _text(_idx(Obs.get('SLPTrend'), 3))),
            'slpSeries':      self._baro_series(),   # 24h [[t,slp],...] for the barograph

            # Rainfall
            'rainToday':    _num(_idx(Obs.get('TodayRain'), 0)),
            'rainYest':     _num(_idx(Obs.get('YesterdayRain'), 0)),
            'rainMonth':    _num(_idx(Obs.get('MonthRain'), 0)),
            'rainYear':     _num(_idx(Obs.get('YearRain'), 0)),
            'rainUnit':     _text(_cfg(config, 'Units', 'Precip')),
            'rainRate':     self._rain_rate_display(Obs, config, rain_eff_mm),
            'rainRateMm':   rain_eff_mm,    # mm/hr, max(raw minute, 10-min mean) - drives the gauge
            'rainRateInstMm': rain_raw_mm,  # the sensor's raw minute, for the record
            'rainStatus':   self._rain_starting(self._snowify_status(
                                self._rain_status_for(rain_eff_mm, _text(_idx(Obs.get('RainRate'), 2))),
                                temp_val, temp_unit, fc_rows), _num(Obs.get('precipStartTs')), obs_ts, now,
                                _num(Obs.get('precipStartReceivedTs'))),
            'drySpellDays': None,   # not reliably sourced - see report
            'lastRainDate': None,   # not sourced - no last-rain date/amount is tracked
            'lastRainAmt':  None,   # not sourced

            # Sun & UV
            'uvIndex':   _num(_idx(Obs.get('UVIndex'), 0)),
            'uvDesc':    _text(_idx(Obs.get('UVIndex'), 2)),
            'radiation': _num(_idx(Obs.get('Radiation'), 0)),
            'radUnit':   _text(_idx(Obs.get('Radiation'), 1)),
            'sunrise':   sunrise_txt,
            'sunset':    sunset_txt,
            'sunFrac':   sun_frac,
            'daylight':  daylight_txt,
            'tillSunset': till_sunset_txt,
            'peakSun':   _num(_idx(Obs.get('peakSun'), 0)),

            # Air quality (US AQI from Open-Meteo, by station lat/lon; off-thread)
            'aqi':         aqi_snap.aqi,
            'aqiCategory': aqi_snap.category,
            'aqiPm25':     _num(aqi_snap.pm25),
            'aqiForecast':    aqi_snap.forecast,
            'aqiPeak':        aqi_snap.peak,
            'aqiPeakTime':    aqi_snap.peak_time,
            'aqiForecastCat': aqi_snap.fc_cat,
            'aqiTrend':       aqi_snap.trend,
            'aqiTrendText':   aqi_snap.trend_text,
            'aqiStale':       (aqi_snap.ts is None) or (now - aqi_snap.ts) > AQI_STALE_SEC,
            'aqiAgeSec':      _age_sec(aqi_snap.ts, now),

            # Weather alerts (NWS, by station lat/lon)
            'alerts':      alerts,
            'alertCount':  len(alerts),
            'alertsStale': (alerts_snap.ts is None) or (now - alerts_snap.ts) > ALERT_STALE_SEC,
            'alertsAgeSec': _age_sec(alerts_snap.ts, now),
            'alertsAsOf':  (_clock(datetime.fromtimestamp(alerts_snap.ts, tz), style)
                            if (alerts_snap.ts and tz) else None),

            # Moon
            'moonPhase':  _text(_idx(Astro.get('Phase'), 1)),
            'moonIllum':  _num(_idx(Astro.get('Phase'), 2)),
            'moonrise':   _clock_case(_text(_idx(Astro.get('Moonrise'), 1))),
            'moonset':    _clock_case(_text(_idx(Astro.get('Moonset'), 1))),
            'nextFull':   _text(_idx(Astro.get('FullMoon'), 0)),
            'nextNew':    _text(_idx(Astro.get('NewMoon'), 0)),

            # Lightning
            'lightningActive':   lightning_active,
            'lightningDist':     _text(_idx(Obs.get('StrikeDist'), 0)),   # the core's +/-3 km RANGE text, e.g. "13-17"
            'lightningDistNum':  _range_mid(_idx(Obs.get('StrikeDist'), 0)),  # its midpoint, for ring geometry / big-number readouts
            'lightningDistUnit': _text(_idx(Obs.get('StrikeDist'), 1)),
            'lightningSinceSec': lightning_since_sec,   # from the strike EPOCH, not the core's frozen delta
            'lightningTs':       int(strike_ts) if strike_ts is not None else None,
            # The core tracks strike FREQUENCY (/min) and a rolling 3-HOUR count -
            # there is no 3-min/30-min bucket anywhere in the data path, so the
            # panel reports what the station actually measures.
            'lightningRate':     _num(_idx(Obs.get('StrikeFreq'), 0)),
            'lightning3hr':      _num(_idx(Obs.get('Strikes3hr'), 0)),
            'lightningToday':    _num(_idx(Obs.get('StrikesToday'), 0)),
            'lightningLast':     lightning_last,

            # Sager (the Weathercaster forecast text + when it was issued; the
            # module exposes only these two, so the card's other slots stay null)
            'sagerCode':     None,   # not sourced - no single composite dial code is exposed
            'sagerText':     _text(Sager.get('Forecast')),
            'sagerIssued':   _clock_case(_text(Sager.get('Issued'))),
            'sagerPressure': None,   # not sourced - no composed "<value> <trend>" string exists
            'sagerWind':     None,   # not sourced
            'sagerSky':      None,   # not sourced
        }
        payload = self._carry_forward(payload, now)
        self.radar.tick(payload, now, tz)
        return _json_safe(payload)

    # --------------------------------------------------------------------
    _station_tz = staticmethod(_station_tz)

    @classmethod
    def _station_today(cls, config):
        tz = cls._station_tz(config)
        now = datetime.now(pytz.utc).astimezone(tz) if tz else datetime.now()
        return now.strftime('%Y-%m-%d')

    @staticmethod
    def _lightning_active(config, since_sec):
        """ True only while lightning is RECENT — mirrors the core console, which
        swaps Panel Six from Rainfall to Lightning on a strike and reverts after
        Display/lightning_timeout minutes (the core also flags the bolt icon for
        strikes < 360 s). This emitter is poll-based, so we show Lightning while
        the last strike is inside that window and Rainfall otherwise. Window =
        lightning_timeout if configured, else 30 min. Never raises. """
        if since_sec is None:
            return False
        try:
            timeout_min = int(_cfg(config, 'Display', 'lightning_timeout') or 0)
        except Exception:                                                 # noqa: BLE001
            timeout_min = 0
        window_sec = timeout_min * 60 if timeout_min > 0 else 1800
        return since_sec < window_sec

    @staticmethod
    def _location_line(config):
        """ Footer location for THIS station, e.g. "Seattle / 47.61d N / 122.33d W",
        built from the local station config. Returns '' when unknown so the HTML
        keeps its generic committed placeholder (no home location ever in git).
        Never raises (the emitter must not crash the app). """
        try:
            name = _cfg(config, 'Station', 'Name')
            lat = _cfg(config, 'Station', 'Latitude')
            lon = _cfg(config, 'Station', 'Longitude')
            if lat in (None, '') or lon in (None, ''):
                return name or ''
            latf, lonf = float(lat), float(lon)
            coords = u'%.2f° %s · %.2f° %s' % (
                abs(latf), 'N' if latf >= 0 else 'S',
                abs(lonf), 'E' if lonf >= 0 else 'W')
            return (u'%s · %s' % (name, coords)) if name else coords
        except Exception:                                                 # noqa: BLE001
            return ''

    @staticmethod
    def _parse_hhmm(text, today, tz):
        """ Parse a formatted sunrise/sunset label ("05:43", "8:12 PM", with an
        optional " (+1)"/" (-1)" day-offset suffix that this simplified
        calculation ignores) into a timezone-aware datetime on `today`.
        Returns None if the label is a placeholder or unparsable. """
        if not text:
            return None
        clean = re.sub(r'\s*\([+-]1\)\s*$', '', text).strip()
        for fmt in ('%H:%M', '%I:%M %p', '%#I:%M %p', '%-I:%M %p'):
            try:
                parsed = datetime.strptime(clean, fmt)
                return tz.localize(datetime.combine(today, parsed.time()))
            except ValueError:
                continue
        return None

    @classmethod
    def _sun_fraction(cls, sunrise_txt, sunset_txt, now_local, tz):
        """ Best-effort elapsed-fraction-of-daylight and daylight-length,
        computed from the already-formatted HH:MM sunrise/sunset labels (the
        Astro DictProperty does not expose raw epoch sun-transit times to the
        display layer). Returns (None, None) if either label is unavailable
        or unparsable. Returns (frac, daylight_txt, till_sunset_txt). """
        if tz is None:
            return None, None, None
        sunrise_dt = cls._parse_hhmm(sunrise_txt, now_local.date(), tz)
        sunset_dt  = cls._parse_hhmm(sunset_txt, now_local.date(), tz)
        if sunrise_dt is None or sunset_dt is None or sunset_dt <= sunrise_dt:
            return None, None, None
        span = (sunset_dt - sunrise_dt).total_seconds()
        elapsed = (now_local - sunrise_dt).total_seconds()
        frac = max(0.0, min(1.0, elapsed / span)) if span > 0 else None
        hours, remainder = divmod(int(span), 3600)
        minutes = remainder // 60
        daylight_txt = f'{hours}h {minutes}m'
        # time remaining until today's sunset (0h 0m once the sun has set)
        till_sec = max(0, int((sunset_dt - now_local).total_seconds()))
        till_h, till_rem = divmod(till_sec, 3600)
        till_sunset_txt = f'{till_h}h {till_rem // 60}m'
        return frac, daylight_txt, till_sunset_txt
