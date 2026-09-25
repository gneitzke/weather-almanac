"""Attention tiers for radar acquisition.

Spend radar bandwidth when a person is likely to look and weather is worth
looking at; stop when neither is true. Pure decisions over signals the engine
already has; the emitter applies the resulting knobs and publishes the tier.

Tiers, lowest first:
  dormant  night or away, quiet weather: one listing an hour, no tiles
  rest     quiet weather, nobody around: listings every 15 min plus a 4-tile
           zoom-5 "sentinel" at home every hour by day (two at night) to see
           rain approaching while the local gauge is dry
  watch    weather present, or conditions unknown: newest frame at the scan
           cadence (Site: primary only); Region keeps eight by day, one at night
  warm     a person touched the device or looked at radar recently: a four-frame
           loop (the newest and three before it), so a tap opens on a moving loop
  live     the Radar tab is open now: today's behaviour, prefetch included

Promotion is immediate. Demotion waits: live drops to warm the moment the tab
closes (warm itself holds 45 min), every other step down needs 10 minutes in
the tier and every stronger hold expired. Weather holds: rain 60 min after the
last wet observation, lightning 30 min after the last strike (each strike
extends), echo 60 min after the last echo-positive footprint. A forecast is
"positive" at 50 % or when the conditions text names rain, showers, snow or
thunder, and stays positive until under 30 % with none of those words. An
observation older than five minutes is unknown, not dry: unknown keeps at
least watch. A LAN browser polling is weak evidence: watch at most, never live.
"""
from collections import Counter
import json
import os
import re
import time

TIERS = ('dormant', 'rest', 'watch', 'warm', 'live')
RANK = {tier: i for i, tier in enumerate(TIERS)}
PRECIP_WORDS = re.compile(r'\b(rain|shower|drizzle|snow|sleet|hail|thunder|storm|flurr)', re.I)
PERCENT = re.compile(r'(\d{1,3})\s*%')

WARM_HOLD_SEC = 45 * 60
UNATTENDED_SEC = 30 * 60   # an open Radar tab with no touch this long is on display, not in use
WET_HOLD_SEC = 60 * 60
LIGHTNING_HOLD_SEC = 30 * 60
ECHO_HOLD_SEC = 60 * 60
OBS_FRESH_SEC = 5 * 60
LAN_VIEWER_SEC = 15 * 60
DEMOTE_DWELL_SEC = 10 * 60
REST_TO_DORMANT_SEC = 2 * 3600
AWAY_SEC = 3 * 86400
NIGHT_START, NIGHT_END = 23, 6
FORECAST_ON_PCT, FORECAST_OFF_PCT = 50, 30

KNOBS = {
    #            frames by day/night, tiles, listing floor (s), sentinel by day/night (s), prefetch
    'live':    dict(frames=(8, 8), tiles=True,  listing=0,    sentinel=(0, 0),        prefetch=True),
    'warm':    dict(frames=(4, 4), tiles=True,  listing=0,    sentinel=(0, 0),        prefetch=False),
    'watch':   dict(frames=(8, 1), tiles=True,  listing=0,    sentinel=(0, 0),        prefetch=False),
    'rest':    dict(frames=(0, 0), tiles=False, listing=900,  sentinel=(3600, 7200),  prefetch=False),
    'dormant': dict(frames=(0, 0), tiles=False, listing=3600, sentinel=(0, 0),        prefetch=False),
}


def is_night(local_hour):
    if local_hour is None:
        return False
    return local_hour >= NIGHT_START or local_hour < NIGHT_END


class Signals:
    """Everything the decision reads. Ages are seconds; None means unknown."""
    __slots__ = ('now', 'local_hour', 'viewing', 'viewed_age', 'touch_age', 'lan_viewer_age',
                 'obs_age', 'rain_rate_mm', 'rain_wet', 'rain_starting', 'lightning_age', 'precip_pct', 'conditions',
                 'echo', 'echo_age', 'sentinel_echo', 'sentinel_age', 'expected_glance')

    def __init__(self, now, **values):
        self.now = now
        for name in self.__slots__[1:]:
            setattr(self, name, values.get(name))
        self.viewing = bool(self.viewing)
        self.expected_glance = bool(self.expected_glance)


class Attention:
    def __init__(self, now=None, tier='watch'):
        now = time.time() if now is None else now
        self.tier = tier
        self.since = now
        self.started = now
        self.reason = 'startup: conditions unknown'
        self.wet_until = 0.0
        self.lightning_until = 0.0
        self.echo_until = 0.0
        self.forecast_on = False
        self.rest_since = now if tier in ('rest', 'dormant') else None
        self.transitions = []          # (ts, from, to, reason), bounded
        self.forced = None
        self.unattended = False

    # ---- weather holds ---------------------------------------------------
    def _weather(self, s):
        fresh = s.obs_age is not None and 0 <= s.obs_age <= OBS_FRESH_SEC
        unknown = not fresh
        if fresh and ((s.rain_rate_mm or 0) > 0 or s.rain_wet):
            self.wet_until = max(self.wet_until, s.now - s.obs_age + WET_HOLD_SEC)
        if s.rain_starting:  # the emitter independently bounds this event's age
            self.wet_until = max(self.wet_until, s.now + WET_HOLD_SEC)
        if s.lightning_age is not None and 0 <= s.lightning_age <= LIGHTNING_HOLD_SEC:
            self.lightning_until = max(self.lightning_until, s.now + LIGHTNING_HOLD_SEC - s.lightning_age)
        for echo, age in ((s.echo, s.echo_age), (s.sentinel_echo, s.sentinel_age)):
            if echo and age is not None and 0 <= age <= ECHO_HOLD_SEC:
                self.echo_until = max(self.echo_until, s.now + ECHO_HOLD_SEC - age)
        # Conditions text: "Showers until 4 PM" is a definite call; "Chance of
        # rain 10%" carries its own probability and is judged by it.
        words = bool(s.conditions and PRECIP_WORDS.search(s.conditions))
        pct = s.precip_pct
        spoken = PERCENT.search(s.conditions or '')
        if words and spoken:
            words = False
            spoken_pct = int(spoken.group(1))
            pct = max(pct, spoken_pct) if pct is not None else spoken_pct
        if (pct is not None and pct >= FORECAST_ON_PCT) or words:
            self.forecast_on = True
        elif pct is not None and pct < FORECAST_OFF_PCT and not words:
            self.forecast_on = False
        holds = []
        if self.wet_until > s.now: holds.append('rain')
        if self.lightning_until > s.now: holds.append('lightning')
        if self.echo_until > s.now: holds.append('echo')
        if self.forecast_on: holds.append('forecast')
        return holds, unknown

    # ---- decision ----------------------------------------------------------
    def decide(self, s):
        holds, unknown = self._weather(s)
        attention_age = min(a for a in (s.viewed_age, s.touch_age) if a is not None) if any(
            a is not None for a in (s.viewed_age, s.touch_age)) else None
        # A tab left open on a kitchen panel is on display, not being studied:
        # keep the 8-frame loop current, drop the zoom/mode prefetch that only
        # pays while fingers are on the map. The next touch restores it.
        self.unattended = bool(s.viewing) and (s.touch_age is None or s.touch_age >= UNATTENDED_SEC)
        if s.viewing:
            want, why = 'live', 'radar tab open, unattended' if self.unattended else 'radar tab open'
        elif attention_age is not None and attention_age < WARM_HOLD_SEC:
            want, why = 'warm', f'attention {int(attention_age/60)} min ago'
        elif holds:
            want, why = 'watch', 'weather: ' + ', '.join(holds)
        elif unknown:
            want, why = 'watch', 'observations unknown'
        elif s.expected_glance:
            want, why = 'watch', 'usual glance hour'
        elif s.lan_viewer_age is not None and s.lan_viewer_age < LAN_VIEWER_SEC:
            want, why = 'watch', 'LAN viewer'
        else:
            want, why = 'rest', 'quiet weather, nobody around'
            # An installation with no markers still accumulates absence. Start
            # conservatively at process startup, never at the Unix epoch.
            away = (attention_age if attention_age is not None else s.now - self.started) >= AWAY_SEC
            resting = self.rest_since is not None and s.now - self.rest_since >= REST_TO_DORMANT_SEC
            if away or (resting and is_night(s.local_hour)):
                want, why = 'dormant', 'away' if away else 'quiet night'

        if self.forced in TIERS:                                  # a test/ops override: no dwell
            self.unattended = False
            if self.forced != self.tier:
                self._move(s.now, self.forced, f'forced {self.forced}')
            else:
                self.reason = f'forced {self.forced}'
            return self.tier
        current = self.tier
        if RANK[want] > RANK[current]:
            self._move(s.now, want, why)
        elif RANK[want] < RANK[current]:
            immediate = current == 'live'                    # the tab closed; warm holds
            if immediate or s.now - self.since >= DEMOTE_DWELL_SEC:
                self._move(s.now, want, why)
        else:
            self.reason = why
        return self.tier

    def _move(self, now, tier, why):
        if tier not in ('rest', 'dormant'):
            self.rest_since = None
        elif self.tier not in ('rest', 'dormant'):
            self.rest_since = now
        self.transitions.append((now, self.tier, tier, why))
        del self.transitions[:-64]
        self.tier, self.since, self.reason = tier, now, why

    def weather(self, now):
        """True when weather is present for the Radar tab's dot (holds only, never 'unknown')."""
        return self.forecast_on or max(self.wet_until, self.lightning_until, self.echo_until) > now

    def knobs(self, local_hour):
        k = KNOBS[self.tier]
        night = is_night(local_hour)
        return dict(tier=self.tier, frames=k['frames'][1 if night else 0], tiles=k['tiles'],
                    listing=k['listing'], sentinel=k['sentinel'][1 if night else 0],
                    prefetch=k['prefetch'] and not (self.tier == 'live' and self.unattended))

    def telemetry(self, now):
        return dict(tier=self.tier, reason=self.reason, since=self.since, forced=self.forced, unattended=self.unattended,
                    weather=self.weather(now), holds=dict(
                        rain=max(0, int(self.wet_until - now)), lightning=max(0, int(self.lightning_until - now)),
                        echo=max(0, int(self.echo_until - now)), forecast=self.forecast_on),
                    transitions=[dict(at=t, **{'from': a, 'to': b}, reason=r) for t, a, b, r in self.transitions[-32:]])


class GlanceHistory:
    """When does this household look at radar? Counts of view starts by weekday
    and local hour. Inert until it holds 14 days and 30 glances; then an hour
    with at least three glances and three times the mean cell rate is an
    'expected glance' hour, and the tier rises to watch ten minutes before it."""
    MIN_DAYS, MIN_GLANCES, RATIO, MIN_COUNT = 14, 30, 3.0, 3

    def __init__(self, path=None):
        self.path = path
        self.counts = Counter()          # (weekday, hour) -> n
        self.first = None
        self.total = 0
        self.load()

    def load(self):
        if not self.path:
            return
        try:
            with open(self.path) as f:
                data = json.load(f)
            self.first = data.get('first')
            self.total = int(data.get('total', 0))
            self.counts = Counter({tuple(int(x) for x in k.split(',')): int(v) for k, v in data.get('counts', {}).items()})
        except (OSError, ValueError, TypeError, AttributeError):
            self.first, self.total, self.counts = None, 0, Counter()

    def save(self):
        if not self.path:
            return
        data = dict(first=self.first, total=self.total,
                    counts={f'{k[0]},{k[1]}': v for k, v in self.counts.items()})
        tmp = f'{self.path}.tmp.{os.getpid()}'
        try:
            with open(tmp, 'w') as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def record(self, local_dt):
        self.counts[(local_dt.weekday(), local_dt.hour)] += 1
        self.total += 1
        if self.first is None:
            self.first = local_dt.timestamp()
        self.save()

    def ready(self, now):
        return self.first is not None and now - self.first >= self.MIN_DAYS * 86400 and self.total >= self.MIN_GLANCES

    def expected(self, local_dt):
        if not self.ready(local_dt.timestamp()) or not self.counts:
            return False
        mean = self.total / (7 * 24)
        # this hour, or the next one when within ten minutes of it
        hours = [(local_dt.weekday(), local_dt.hour)]
        if local_dt.minute >= 50:
            nxt_hour = (local_dt.hour + 1) % 24
            nxt_day = (local_dt.weekday() + (1 if nxt_hour == 0 else 0)) % 7
            hours.append((nxt_day, nxt_hour))
        return any(self.counts.get(h, 0) >= max(self.MIN_COUNT, self.RATIO * mean) for h in hours)

    def telemetry(self, now, local_dt=None):
        return dict(total=self.total, days=int((now - self.first) / 86400) if self.first else 0,
                    ready=self.ready(now), expectedNow=self.expected(local_dt) if local_dt else None)
