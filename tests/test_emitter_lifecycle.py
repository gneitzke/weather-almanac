""" Regression coverage for the emitter's scheduling lifecycle and for the
atomic publication of off-thread provider results.

Two seams are faked so the scenarios are instant and deterministic:
  * FakeClock replaces kivy.clock.Clock (conftest's stub is a no-op that hands
    back an uncancellable handle, so it cannot show accumulation). Driving it
    with advance() runs a full day of failed fetches in milliseconds.
  * lib.almanac_emit.threading is swapped for a stand-in whose Thread runs the
    worker inline (or, where an in-flight fetch is the subject, never runs it),
    so no test depends on real thread timing.
"""

import json
import os
import time
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from lib import almanac_emit as ae
from tests.fixtures import obs_scenarios as scn


# ------------------------------------------------------------------ fake clock
class FakeEvent:
    def __init__(self, clock, callback, timeout, interval):
        self.clock    = clock
        self.callback = callback
        self.timeout  = timeout
        self.interval = interval
        self.due      = clock.now + timeout

    def cancel(self):
        if self in self.clock.events:
            self.clock.events.remove(self)


class FakeClock:
    """ Kivy Clock API, hand-driven. `events` is every live handle, which is
    exactly what "pending scheduled work" means for these tests. """

    def __init__(self):
        self.now    = 0.0
        self.events = []

    def schedule_once(self, callback, timeout=0):
        return self._add(callback, timeout, interval=False)

    def schedule_interval(self, callback, timeout):
        return self._add(callback, timeout, interval=True)

    def _add(self, callback, timeout, interval):
        event = FakeEvent(self, callback, float(timeout), interval)
        self.events.append(event)
        return event

    def advance(self, seconds):
        """ Run the timeline forward, firing every due handle in time order
        (including handles scheduled by the callbacks we fire). """
        target = self.now + seconds
        while True:
            due = [event for event in self.events if event.due <= target]
            if not due:
                break
            event = min(due, key=lambda e: e.due)
            self.now = event.due
            if event.interval:
                event.due = self.now + event.timeout
            else:
                event.cancel()
            if event.callback(0) is False:
                event.cancel()
        self.now = target


class InlineThread:
    """ Runs the worker on the caller's stack: the fetch is finished by the
    time start() returns. """
    def __init__(self, target=None, daemon=None):
        self.target = target

    def start(self):
        self.target()


class HangingThread:
    """ Never runs the worker: a fetch that is started and still in flight. """
    started = []

    def __init__(self, target=None, daemon=None):
        self.target = target

    def start(self):
        HangingThread.started.append(self.target)


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(ae, 'Clock', fake)
    return fake


def _inline_threads(monkeypatch):
    monkeypatch.setattr(ae, 'threading', SimpleNamespace(Thread=InlineThread))


def _response(payload):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(payload).encode()

    return lambda *args, **kwargs: Response()


# ------------------------------------------------------------------- lifecycle
def test_start_is_idempotent_and_stop_cancels_every_handle(make_emitter, clock):
    emitter = make_emitter(scn.all_none())
    emitter.start()
    # 8 intervals (emit, version, aqi, alerts, forecast, intent, geo, warnings)
    # plus 6 boot one-shots.
    assert len(clock.events) == 14

    emitter.start()
    assert len(clock.events) == 14       # re-armed, not stacked

    emitter.stop()
    assert clock.events == []            # one-shots included


def test_a_stopped_emitter_does_no_work(make_emitter, clock, monkeypatch):
    monkeypatch.setattr(ae, 'threading', SimpleNamespace(Thread=HangingThread))
    HangingThread.started = []
    emitter = make_emitter(scn.all_none())
    emitter.start()
    emit_tick = clock.events[0].callback          # already handed to the Clock
    emitter.stop()

    # A callback that was already due when stop() ran must not emit, and it
    # unschedules itself by returning False.
    assert emit_tick(0) is False
    assert not os.path.exists(emitter.output_path)
    emitter._check_aqi()
    emitter._check_alerts()
    assert HangingThread.started == []


def test_a_day_of_forecast_failures_stays_one_retry_chain(make_emitter, clock, monkeypatch):
    """ Every hourly failure used to start its own two-minute retry chain, so a
    day offline meant thousands of attempts and dozens of live chains. """
    _inline_threads(monkeypatch)
    # The scheduler and radar rate/backoff gates must share simulated time.
    # Real monotonic time can otherwise make a whole simulated day spin at a
    # nearly expired cooldown without advancing that cooldown.
    epoch = time.time()
    monkeypatch.setattr(ae.time, 'time', lambda: epoch + clock.now)
    monkeypatch.setattr(ae.time, 'monotonic', lambda: clock.now)
    import urllib.request

    def refuse(*args, **kwargs):
        raise OSError('network is down')

    monkeypatch.setattr(urllib.request, 'urlopen', refuse)
    monkeypatch.setattr(ae.RadarSession, 'open', refuse)
    emitter = make_emitter(scn.all_none())
    attempts = []
    # This simulates a full day of provider retries; raster correctness and the
    # real independent geo worker are exercised in test_radar_v42.
    monkeypatch.setattr(emitter, '_radar_geo_work',
                        lambda token=None: setattr(emitter, '_radar_geo_idle', token))
    fetch = emitter._do_forecast
    monkeypatch.setattr(emitter, '_do_forecast', lambda: (attempts.append(clock.now), fetch())[1])

    emitter.start()
    clock.advance(24 * 3600)

    # One chain retrying every FORECAST_RETRY_SEC, plus the hourly poll; never
    # a chain per failure.
    ceiling = 24 * 3600 // ae.FORECAST_RETRY_SEC + 24 + 2
    assert 600 < len(attempts) <= ceiling         # still retrying, not multiplying
    # one retry chain PER provider, never a chain per failure: with the network
    # down, radar (also fetched) legitimately keeps its own single retry too
    assert sorted(emitter._retries) == ['forecast', 'radar']
    assert len(clock.events) == 11                # 8 intervals + forecast/radar retries + discovery

    emitter.stop()
    assert clock.events == [] and emitter._retries == {}


def test_only_one_fetch_per_provider_is_in_flight(make_emitter, clock, monkeypatch):
    monkeypatch.setattr(ae, 'threading', SimpleNamespace(Thread=HangingThread))
    HangingThread.started = []
    emitter = make_emitter(scn.all_none())
    emitter.start()

    emitter._check_aqi()
    emitter._check_aqi()          # first is still running - must not stack
    emitter._check_alerts()
    assert len(HangingThread.started) == 2

    # the slow AQI fetch finishes; the next poll is allowed again
    emitter._inflight.discard('aqi')
    emitter._check_aqi()
    assert len(HangingThread.started) == 3


# ------------------------------------------------------- atomic publication
def test_aqi_publishes_one_complete_snapshot(make_emitter, monkeypatch):
    """ The emit tick must never see a new AQI beside the previous peak/trend. """
    _inline_threads(monkeypatch)
    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen', _response(
        {'current': {'us_aqi': 20, 'pm2_5': 3.1}, 'hourly': {}}))

    stale = ae._AqiResult(160, 'Unhealthy', 31.0, time.time(), (),
                          160, '5\u00a0PM', 'Unhealthy', 'rising', 'Unhealthy by 5\u00a0PM')
    emitter = make_emitter(scn.all_none(), _aqi_result=stale)

    mid_fetch = []

    def summarize(hourly, now, tz, aqi_now, style='12 hr'):
        mid_fetch.append(emitter._build_payload())
        return [], 20, None, 'steady', None, 'Good'

    monkeypatch.setattr(ae.AlmanacEmitter, '_aqi_forecast_summary', staticmethod(summarize))
    emitter._do_aqi()

    during = mid_fetch[0]
    assert (during['aqi'], during['aqiPeak'], during['aqiTrend'], during['aqiStale']) \
        == (160, 160, 'rising', False)            # wholly the old reading
    after = emitter._build_payload()
    assert (after['aqi'], after['aqiPeak'], after['aqiTrend'], after['aqiPm25']) \
        == (20, 20, 'steady', 3.1)                # wholly the new one


def test_forecast_publishes_rows_and_freshness_together(make_emitter, monkeypatch):
    _inline_threads(monkeypatch)
    import urllib.request
    days = [(date.today() + timedelta(days=i)).isoformat() for i in range(3)]
    monkeypatch.setattr(urllib.request, 'urlopen', _response({'daily': {
        'time': days, 'temperature_2m_max': [70, 71, 72],
        'temperature_2m_min': [50, 51, 52], 'weather_code': [0, 0, 0],
        'precipitation_probability_max': [0, 0, 0], 'precipitation_sum': [0, 0, 0],
        'wind_gusts_10m_max': [10, 10, 10]}}))

    emitter = make_emitter(scn.all_none())
    mid_fetch = []
    shape = ae.AlmanacEmitter._fc_daily_from

    def observe(daily):
        mid_fetch.append(emitter._build_payload())
        return shape(daily)

    monkeypatch.setattr(ae.AlmanacEmitter, '_fc_daily_from', staticmethod(observe))
    emitter._do_forecast()

    during = mid_fetch[0]
    assert during['fcDaily'] == [] and during['fcStale'] is True     # no rows, and known stale
    after = emitter._build_payload()
    assert len(after['fcDaily']) == 3 and after['fcStale'] is False


def test_alerts_publish_features_and_freshness_together(make_emitter, monkeypatch):
    """ Features used to land before the timestamp, so a tick in between showed
    freshly fetched alerts flagged as stale. """
    _inline_threads(monkeypatch)
    import urllib.request
    ends = ae.datetime.fromtimestamp(time.time() + 3600, ae.timezone.utc).isoformat()
    monkeypatch.setattr(urllib.request, 'urlopen', _response({'features': [
        {'properties': {'event': 'Wind Advisory', 'areaDesc': 'King, WA', 'ends': ends}}]}))

    emitter = make_emitter(scn.all_none())
    mid_fetch = []
    process = ae.AlmanacEmitter._process_alerts

    def observe(self, feats, now, tz):
        if not mid_fetch:      # the emit tick re-processes too; only the fetch matters
            mid_fetch.append(emitter._build_payload())
        return process(self, feats, now, tz)

    monkeypatch.setattr(ae.AlmanacEmitter, '_process_alerts', observe)
    emitter._do_alerts()

    during = mid_fetch[0]
    assert during['alertCount'] == 0 and during['alertsStale'] is True
    after = emitter._build_payload()
    assert after['alertCount'] == 1 and after['alertsStale'] is False
