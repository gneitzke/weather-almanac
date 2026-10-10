"""Additional emitter <-> RadarEngine contract coverage.

The checked-in parity fixture is deliberately regenerated only with:

    RADAR_CONTRACT_REGENERATE=1 RADAR_NET_TEST=0 python3 -m pytest -q \
        -p no:cacheprovider --basetemp .pytest-tmp/ct \
        tests/test_radar_engine_contract2.py::test_seeded_wx_and_health_payload_match_golden

That opt-in writes ``tests/fixtures/radar_engine_contract_payload.json``.  Review
the resulting diff before keeping it: fixture data is synthetic and must never
contain an address, home path, or personal contact detail.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import threading

import pytest

from lib import almanac_emit as ae, radar_engine
from tests.fixtures import obs_scenarios as scn
from tests.fixtures.config import make_config
from tests.test_emitter_lifecycle import FakeClock


FIXED_NOW = 1_735_732_000  # 2025-01-01 11:46:40 UTC
GOLDEN = Path(__file__).with_name('fixtures') / 'radar_engine_contract_payload.json'


class _FrozenDateTime(datetime):
    """A datetime replacement that leaves parsing/formatting class methods intact."""

    @classmethod
    def now(cls, tz=None):
        instant = cls.fromtimestamp(FIXED_NOW, timezone.utc)
        return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)


class _TimedOrderedLock:
    """A bounded RLock that detects a forbidden radar -> lifecycle acquire."""

    def __init__(self, name, registry, limit=1.0):
        self._lock = threading.RLock()
        self.name, self.registry, self.limit = name, registry, limit

    def acquire(self, blocking=True, timeout=-1):
        held = self.registry.held.setdefault(threading.get_ident(), [])
        if self.name == 'runtime' and 'radar' in held:
            self.registry.violations.append((threading.current_thread().name, tuple(held), self.name))
        if self.name == 'runtime' and threading.current_thread().name == 'provider-radar':
            self.registry.provider_cleanup_waiting.set()
        if not blocking:
            acquired = self._lock.acquire(False)
        else:
            wait = self.limit if timeout is None or timeout < 0 else min(timeout, self.limit)
            acquired = self._lock.acquire(timeout=wait)
        if not acquired:
            self.registry.timeouts.append((threading.current_thread().name, self.name, tuple(held)))
            raise AssertionError(f'timed out acquiring {self.name} while holding {held}')
        held.append(self.name)
        return True

    def release(self):
        held = self.registry.held[threading.get_ident()]
        held.reverse()
        held.remove(self.name)
        held.reverse()
        self._lock.release()

    __enter__ = acquire

    def __exit__(self, *exc):
        self.release()


def test_provider_completion_during_stop_restart_is_fenced_and_ordered(make_emitter, monkeypatch):
    """A worker completion waits behind restart's lifecycle lock, then drains cleanly.

    The barriers/events force the completion cleanup to contend with ``stop()`` /
    ``start()``.  Every lock acquisition has a finite timeout, so an inversion
    reports a failed test instead of leaving pytest hung.
    """
    clock = FakeClock()
    monkeypatch.setattr(ae, 'Clock', clock)
    emitter = make_emitter()
    engine, runtime = emitter.radar, emitter._runtime
    registry = type('LockRegistry', (), dict(
        held={}, violations=[], timeouts=[], provider_cleanup_waiting=threading.Event()))()
    runtime.lock = _TimedOrderedLock('runtime', registry)
    engine._lock = _TimedOrderedLock('radar', registry)

    # Do not start cache I/O: this test owns the only provider worker it needs.
    restart_phase = threading.Event()
    restart_inside_engine = threading.Event()
    let_restart_continue = threading.Event()

    def inventory():
        if restart_phase.is_set():
            restart_inside_engine.set()
            assert let_restart_continue.wait(1.0), 'restart did not receive its release signal'

    monkeypatch.setattr(engine, '_start_inventory', inventory)
    emitter.start()

    worker_started = threading.Event()
    allow_worker_finish = threading.Event()
    workers = []

    def worker():
        threading.current_thread().name = 'provider-radar'
        worker_started.set()
        assert allow_worker_finish.wait(1.0), 'worker was never released'

    def thread_factory(**kwargs):
        thread = threading.Thread(**kwargs)
        workers.append(thread)
        return thread

    runtime.thread_factory = thread_factory
    engine._spawn('radar', worker)
    assert worker_started.wait(1.0)
    assert runtime.inflight == {'radar'}

    lifecycle_errors = []
    restart_barrier = threading.Barrier(2)

    def stop_then_restart():
        try:
            emitter.stop()
            restart_phase.set()
            restart_barrier.wait(timeout=1.0)
            emitter.start()
        except BaseException as error:  # asserted on the parent thread
            lifecycle_errors.append(error)

    lifecycle = threading.Thread(target=stop_then_restart, name='emitter-restart', daemon=True)
    lifecycle.start()
    restart_barrier.wait(timeout=1.0)
    assert restart_inside_engine.wait(1.0), 'restart did not enter RadarEngine.start'

    # start() holds the documented outer lifecycle lock here.  Releasing the
    # provider makes ProviderRuntime._run() attempt its completion cleanup
    # under that lock, proving it cannot reverse the order or escape the fence.
    allow_worker_finish.set()
    assert registry.provider_cleanup_waiting.wait(1.0), 'provider did not attempt completion cleanup'
    let_restart_continue.set()

    lifecycle.join(2.0)
    for thread in workers:
        thread.join(2.0)
    assert not lifecycle.is_alive() and not any(thread.is_alive() for thread in workers)
    assert not lifecycle_errors
    assert not registry.timeouts and not registry.violations
    assert runtime.running and not runtime.inflight
    assert runtime.events, 'restart must re-arm the emitter and radar cadence'
    assert not engine._input_pool._shutdown and not engine._hca_pool._shutdown

    emitter.stop()
    assert not runtime.events and not runtime.inflight
    assert engine._input_pool._shutdown and engine._hca_pool._shutdown


def _seeded_emitter(make_emitter, monkeypatch):
    """Build one realistic, entirely synthetic emitter state at a fixed instant."""
    monkeypatch.setattr(ae, 'Clock', FakeClock())
    monkeypatch.setattr(ae.time, 'time', lambda: FIXED_NOW)
    monkeypatch.setattr(ae.time, 'monotonic', lambda: float(FIXED_NOW))
    monkeypatch.setattr(ae, 'datetime', _FrozenDateTime)

    scenario = scn.clear_day()
    scenario['Obs'].update({
        'obsTs': FIXED_NOW - 75,
        'outTemp': ['72.0', 'F'], 'Humidity': ['41', '%'],
        'WindSpd': ['8.0', 'mph', '3', '3', 'Gentle Breeze'],
        'WindDir': ['180', '', 'S'], 'RainRate': ['0.00', 'in/hr', 'Dry', 0.0],
        'UVIndex': ['6', '', 'High'], 'Radiation': ['450', 'W/m2'],
    })
    scenario['Met'].update({
        'UpdatedTs': FIXED_NOW - 30, 'Conditions': 'Clear', 'Valid': 'This afternoon',
        'lowTemp': ['45', 'F'], 'highTemp': ['74', 'F'], 'PrecipPercnt': ['5', '%'],
        'PrecipDay': ['10', '%'], 'WindSpd': ['9', 'mph'], 'WindDir': ['190', '', 'S'],
    })
    scenario['Astro'].update({
        'Sunrise': ['', '07:51'], 'Sunset': ['', '16:32'],
        'Phase': ['', 'Waxing Crescent', '12'], 'Moonrise': ['', '10:04'],
        'Moonset': ['', '20:11'], 'FullMoon': ['2025-01-13'],
    })
    scenario['Sager'] = {'Forecast': 'Fair', 'Issued': 'Morning'}
    config = make_config(Station={
        'Name': 'Fixture Station', 'Latitude': '47.61', 'Longitude': '-122.33',
        'Timezone': 'America/Los_Angeles', 'TempestID': '111', 'TempestSN': 'ST-00000111',
        'OutAirID': '', 'OutAirSN': '', 'Elevation': '100', 'TempestHeight': '2', 'OutAirHeight': '2',
    })
    emitter = make_emitter(scenario=scenario, config=config)
    emitter._aqi_result = ae._AqiResult(27, 'Good', 6.2, FIXED_NOW - 120,
                                        ((FIXED_NOW + 3600, 31),), 31, '1 PM', 'Good', 'steady', 'Steady')
    emitter._fc_result = ae._FcResult(({'day': '2025-01-02', 'hi': 74, 'lo': 45, 'code': 'clear', 'pp': 5},),
                                      ((FIXED_NOW + 3600, 70),), FIXED_NOW - 60)
    emitter._alerts_result = ae._AlertsResult(None, (), FIXED_NOW - 90)
    emitter._ver_result = ae._VerResult(False, 'v1.0', 'v1.0')
    emitter.radar._cache_ready.set()
    # Snapshotting diagnostics must not start the native ledger's asynchronous
    # writer; the fixture deliberately represents a clean empty ledger instead.
    monkeypatch.setattr(emitter.radar._native_budget, 'snapshot',
                        lambda: dict(day='2025-01-02', bytesToday=0, ceilingState='normal', ledgerState='ok'))
    return emitter


def test_seeded_wx_and_health_payload_match_golden(make_emitter, monkeypatch):
    """The public wx.json and radar-health.json shape remains byte-for-byte stable."""
    emitter = _seeded_emitter(make_emitter, monkeypatch)
    emitter._emit(0)
    actual = {
        'wx.json': json.loads(Path(emitter.output_path).read_text()),
        'radar-health.json': json.loads(Path(emitter.output_path).with_name('radar-health.json').read_text()),
    }
    encoded = json.dumps(actual, indent=2, sort_keys=True) + '\n'
    forbidden = ('192.168.', '10.0.0.', '/Users/', '/home/', '@')
    assert not any(value in encoded for value in forbidden), 'golden fixture must remain synthetic and portable'
    if os.environ.get('RADAR_CONTRACT_REGENERATE') == '1':
        GOLDEN.write_text(encoded)
        pytest.fail(f'regenerated {GOLDEN}; review and rerun without RADAR_CONTRACT_REGENERATE')
    assert actual == json.loads(GOLDEN.read_text())
