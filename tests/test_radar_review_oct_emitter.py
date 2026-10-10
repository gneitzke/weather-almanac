"""October radar review, emitter side (implementer A): lock order, contributor
time, coverage, cache boot, radar-health.json, discovery cadence, boot expiry,
forecast evidence validity and the low-severity items. Local fixtures only."""
import json
import os
import threading
import time as real_time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from lib import almanac_emit as ae
from lib import radar_engine
from lib import radar_attention as ra
from lib import radar_basemap as bm
from lib import radar_level3 as l3
from lib import radar_mosaic as mosaic
from lib.radar_cache import TileInventory
from lib.radar_discovery import DiscoverySchedule
from lib.radar_level3 import Scan
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401
from tests.test_radar_level3 import native, product, gate_code  # noqa: F401


# ---------------------------------------------------------------- A1: lock order
class LockTimeout(AssertionError):
    pass


class CheckedLock:
    """An RLock whose blocking acquire gives up after `limit` seconds (a
    deadlock fails the test instead of hanging it) and which records any
    acquisition that breaks the documented order health -> radar."""

    def __init__(self, name, registry, limit=3.0):
        self._lock = threading.RLock()
        self.name, self.registry, self.limit = name, registry, limit

    def acquire(self, blocking=True, timeout=-1):
        held = self.registry.held.setdefault(threading.get_ident(), [])
        if self.name == 'health' and 'radar' in held and 'health' not in held:
            self.registry.violations.append(threading.current_thread().name)
        self.registry.attempts.append((threading.current_thread().name, self.name))
        self.registry.attempted.set()
        if not blocking:
            ok = self._lock.acquire(False)
        else:
            wait = self.limit if timeout is None or timeout < 0 else min(timeout, self.limit)
            ok = self._lock.acquire(timeout=wait)
            if not ok and (timeout is None or timeout < 0):
                self.registry.timeouts.append((threading.current_thread().name, self.name, list(held)))
                raise LockTimeout(f'{self.name} not acquired in {self.limit} s while holding {held}')
        if ok:
            held.append(self.name)
        return ok

    def release(self):
        held = self.registry.held[threading.get_ident()]
        held.reverse(); held.remove(self.name); held.reverse()
        self._lock.release()

    __enter__ = acquire

    def __exit__(self, *exc):
        self.release()


def checked(emitter):
    registry = SimpleNamespace(held={}, violations=[], timeouts=[], attempts=[], attempted=threading.Event())
    emitter.radar._lock = CheckedLock('radar', registry)
    emitter.radar._health.lock = CheckedLock('health', registry)
    emitter.radar._n0h_health.lock = CheckedLock('health', registry)
    return registry


class Offline:
    def open(self, req, timeout):
        raise ConnectionRefusedError('offline test transport')

    def close(self):
        pass


def test_late_input_worker_pass_logging_and_payload_cannot_deadlock(make_emitter):
    e = make_emitter()
    registry = checked(e)
    e.radar._session = Offline()
    admitted = e.radar._health.admit
    worker_holds_health = threading.Event()

    def admit(source, url, metadata=False):
        # A native input worker that outlived its frame is inside request
        # admission (health held) when the coordinator logs the pass.
        worker_holds_health.set()
        registry.attempted.clear()
        registry.attempted.wait(1)          # the logger reaches for a lock
        real_time.sleep(.05)
        return admitted(source, url, metadata)
    e.radar._health.admit = admit
    errors = []

    def worker():
        try:
            e.radar._request(radar_engine.RADAR_LEVEL3_TRANSPORT, radar_engine.RADAR_LEVEL3_BUCKET + 'NEA_N0B_x', real_time.monotonic() + 30)
        except LockTimeout as error:
            errors.append(error)
        except Exception:                                            # noqa: BLE001
            pass                                                     # the offline transport

    def logger():
        try:
            assert worker_holds_health.wait(5)
            e.radar._log_pass(real_time.monotonic())
        except Exception as error:                                   # noqa: BLE001
            errors.append(error)

    def payload():
        try:
            assert worker_holds_health.wait(5)
            e._build_payload()
            e.radar._health_payload()
        except Exception as error:                                   # noqa: BLE001
            errors.append(error)

    e.radar._begin_log_pass()
    threads = [threading.Thread(target=t, name=t.__name__, daemon=True) for t in (worker, logger, payload)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert not any(t.is_alive() for t in threads), 'deadlocked'
    assert not errors and not registry.timeouts, (errors, registry.timeouts)
    assert not registry.violations, registry.violations


def test_a_full_native_pass_never_takes_health_under_the_radar_lock(make_emitter, hybrid, multisite, native):
    hybrid.view()
    e = make_emitter()
    registry = checked(e)
    e.radar._acquire()
    e._build_payload()
    e.radar._write_health(ae.time.time(), force=True)
    assert e.radar._result.available and e.radar._result.tiles['variant'] == 'native'
    assert not registry.violations and not registry.timeouts


def test_the_lock_order_is_documented_where_the_lock_is_made():
    source = Path(radar_engine.__file__).read_text()
    head = source[:source.index('self._lock = _RLock()')]
    assert 'LOCK ORDER' in head[-1500:] and 'HostHealth.lock' in head[-1500:]


# ---------------------------------------------- A2 / L7 / L9: contributor time
def site_ctx(**scans):
    return dict(native=True, attention='live', site_id='KNEA',
                sites=[dict(id=s, reporting=True) for s in scans], site_scans=scans)


def test_neighbour_blend_limit_follows_the_scan_cadence():
    # The blend limit keeps L9's 2.5-interval rule. The display stale threshold
    # (latency + two intervals) is pinned in test_radar_stale_latency.py.
    assert radar_engine._radar_neighbour_limit_sec(None) == 900
    assert radar_engine._radar_neighbour_limit_sec(120) == 480   # SAILS: never under 8 min
    assert radar_engine._radar_neighbour_limit_sec(270) == 720   # precipitation: 2.5 scans, whole minutes
    assert radar_engine._radar_neighbour_limit_sec(600) == 900   # clear air: never over 15 min


def test_a_stale_neighbour_never_blends_into_the_newest_frame():
    T = 1_800_000_000
    ctx = site_ctx(KNEA=[T-600, T], KMID=[T-420])
    # Fresh as of now (780 s < 900 s for a 10-minute cadence): blended.
    assert radar_engine._radar_site_pairs(ctx, T, now=T+360) == (('KNEA', T), ('KMID', T-420))
    # The neighbour ages past the threshold while its frame is still newest.
    assert radar_engine._radar_site_pairs(ctx, T, now=T+500) == (('KNEA', T),)


def test_an_older_frame_keeps_its_contributors_as_the_clock_runs():
    T = 1_800_000_000
    ctx = site_ctx(KNEA=[T-600, T-300, T], KMID=[T-700])
    first = radar_engine._radar_site_pairs(ctx, T-300, now=T+10)
    # Judged as of its successor's scan (T), not the wall clock.
    assert first == (('KNEA', T-300), ('KMID', T-700))
    assert radar_engine._radar_site_pairs(ctx, T-300, now=T+3000) == first


def native_snapshot(now, contributors, anchor, cadence=600):
    frame = dict(ts=anchor, stamp='x', complete=True, levels={}, mosaicKey='Mabc', siteScans=contributors,
                 acquiredSites=contributors, requestedPairs=[[p['id'], p['ts']] for p in contributors])
    tiles = dict(frames=[dict(frame)], variant='native')
    return radar_engine._RADAR_NONE._replace(available=True, reason=None, frames=(frame,), ts_frame=anchor,
        source_id='iem-nexrad-n0b', source_mode='site', stale_sec=900, tiles=tiles, scan_cadence_sec=cadence,
        units='mi', legend={}, sources=(), sites=())


def test_native_age_is_the_oldest_contributor_not_the_anchor():
    T = 1_800_000_000
    # The reviewer's case: the primary's newest scan failed, a neighbour 8 min
    # older carried the mosaic. The pixels are 22 minutes old, not 14.
    snap = native_snapshot(T+840, [dict(id='KMID', ts=T-456, volumeTs=T-430, filtered=True)], T)
    r = radar_engine.RadarEngine._payload(snap, T+840, timezone.utc)
    assert r['observedTs'] == T                        # the animation key keeps its meaning
    assert r['ageSec'] == 1296 and r['stale'] is True
    assert r['observedRange'] == [T-456, T-456]
    assert r['tiles']['frames'][0]['observedRange'] == [T-456, T-456]
    # Fresh contributors: a range and the oldest age, current.
    snap = native_snapshot(T+120, [dict(id='KNEA', ts=T, volumeTs=T+20, filtered=True),
                                   dict(id='KMID', ts=T-240, volumeTs=T-220, filtered=True)], T, cadence=270)
    r = radar_engine.RadarEngine._payload(snap, T+120, timezone.utc)
    assert r['observedRange'] == [T-240, T] and r['ageSec'] == 360
    assert r['staleSec'] == 840 and r['stale'] is False    # default latency 300 + 2 x 270


def test_region_frames_keep_the_anchor_age():
    T = 1_800_000_000
    frame = dict(ts=T, stamp='x', complete=True, levels={}, siteScans=[])
    snap = radar_engine._RADAR_NONE._replace(available=True, reason=None, frames=(frame,), ts_frame=T,
        source_id='iem-mrms-lcref', source_mode='mosaic', stale_sec=600, tiles=dict(frames=[dict(frame)]),
        units='mi', legend={}, sources=(), sites=())
    r = radar_engine.RadarEngine._payload(snap, T+100, timezone.utc)
    assert r['ageSec'] == 100 and r['observedRange'] is None and r['staleSec'] == 600
    assert 'observedRange' not in r['tiles']['frames'][0]


def test_a_neighbour_only_native_mosaic_reports_the_neighbours_age(make_emitter, hybrid, multisite, native):
    multisite.scans['KMID'] = [hybrid.latest-420]
    native.missing.add(hybrid.latest)          # the primary's newest product never reaches S3
    hybrid.view()
    e = make_emitter()
    e.radar._acquire()
    r = e._build_payload()['radar']
    newest = next(f for f in r['tiles']['frames'] if f['ts'] == r['observedTs'])
    if any(p['id'] == 'KNEA' for p in newest['siteScans']):
        pytest.skip('fixture drew the primary')  # pragma: no cover - guards a fixture change
    assert r['observedTs'] == hybrid.latest
    assert r['observedRange'] == [hybrid.latest-420, hybrid.latest-420]
    assert r['ageSec'] == int(ae.time.time()) - (hybrid.latest-420)
    assert r['partialCoverage'] is True        # an expected contributor is missing


# ------------------------------------------------------ A3: coverage vs echoes
def flat_scan(code, gates=1840, lat=47.61, lon=-122.33):
    codes = np.full((720, gates), code, np.uint8)
    bearing_index = (np.arange(3600) // 5).astype(np.int32)
    return Scan(lat, lon, 100., .5, 215, 1789257600, codes, bearing_index)


def site_tile(z=8, lat=47.61, lon=-122.33):
    n = 2**z
    import math
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def test_missing_gates_are_uncovered_not_clear():
    from lib.radar_palette import source_palette
    palette = source_palette('iem-nexrad-n0b')
    x, y = site_tile(8)
    clear, _ = mosaic.render_mosaic([flat_scan(0)], 8, x, y, palette)
    missing, _ = mosaic.render_mosaic([flat_scan(1)], 8, x, y, palette)
    # Both are transparent: the pixels alone cannot tell "measured clear" from "nothing measured".
    assert list(clear.getdata()) == list(missing.getdata())
    assert clear.info['radarUncoveredPixels'] == 0
    assert missing.info['radarUncoveredPixels'] == 256*256


def test_outside_every_disc_is_uncovered():
    from lib.radar_palette import source_palette
    x, y = site_tile(7)
    # The next tile east at zoom 7: partly beyond the 230 km disc.
    image, _ = mosaic.render_mosaic([flat_scan(gate_code(35))], 7, x+1, y, source_palette('iem-nexrad-n0b'))
    uncovered = image.info['radarUncoveredPixels']
    visible = sum(1 for p in image.getdata() if p)
    assert 0 < uncovered < 256*256 and visible and visible + uncovered == 256*256  # every measured pixel echoes


def test_native_tiles_carry_the_coverage_count_and_a_new_revision(make_emitter, hybrid, multisite, native):
    from PIL import Image
    assert l3.NATIVE_REVISION.endswith('v7')  # v7: radarMeasuredGrid
    hybrid.view()
    e = make_emitter()
    e.radar._acquire()
    records = {k: v for k, v in e.radar._disk_inventory.records.items() if k[-1] == 'native'}
    assert records
    for key, (path, _, meta) in records.items():
        with Image.open(path) as image:
            image.load()
            count = int(image.info['radarUncoveredPixels'])
            grid = image.info['radarMeasuredGrid']
        assert meta['uncoveredPixels'] == count and meta['measuredGrid'] == grid
        read = radar_engine._radar_tile_metadata(Path(path), key[0])
        assert read['uncoveredPixels'] == count and read['measuredGrid'] == grid
    # Region (MRMS) tiles carry no count: fully covered by definition.
    for key, (path, _, _) in e.radar._disk_inventory.records.items():
        if key[0] == 'iem-mrms-lcref':
            with Image.open(path) as image:
                assert 'radarUncoveredPixels' not in image.info


def test_a_tile_without_or_with_a_false_coverage_count_is_refused(make_emitter, hybrid, multisite, native):
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo
    hybrid.view()
    e = make_emitter()
    e.radar._acquire()
    key, (path, _, _) = next((k, v) for k, v in e.radar._disk_inventory.records.items() if k[-1] == 'native')
    with Image.open(path) as image:
        image.load()
        texts = {k: v for k, v in image.info.items() if k.startswith('radar')}
        rebuilt = image.copy()
    for value in (None, '70000', 'x'):
        info = PngInfo()
        for k, v in texts.items():
            if k != 'radarUncoveredPixels':
                info.add_text(k, str(v))
        if value is not None:
            info.add_text('radarUncoveredPixels', value)
        rebuilt.save(path, format='PNG', pnginfo=info)
        with pytest.raises((KeyError, ValueError)):
            radar_engine._radar_tile_metadata(Path(path), key[0])


def test_partial_coverage_for_site_frames():
    # Contributors, not geometry (adversarial review): geometry is in the tiles' grids.
    beyond = dict(n=50.5, s=47.4, w=-122.8, e=-121.9)
    frame = lambda requested, got, expected=(): dict(requestedPairs=requested, expectedSites=list(expected),
        acquiredSites=[dict(id=s, ts=t) for s, t in got], siteScans=[dict(id=s, ts=t) for s, t in got])
    assert not radar_engine._radar_partial_coverage('iem-nexrad-n0b', frame([['KNEA', 1]], [('KNEA', 1)], ['KNEA']), dict(bounds=beyond))
    assert radar_engine._radar_partial_coverage('iem-nexrad-n0b',
        frame([['KMID', 1], ['KNEA', 1]], [('KNEA', 1)]), dict(bounds=beyond))
    assert radar_engine._radar_partial_coverage('iem-nexrad-n0b',
        frame([['KNEA', 1]], [('KNEA', 1)], ['KMID', 'KNEA']), dict(bounds=beyond))
    assert not radar_engine._radar_partial_coverage('rainviewer', None, dict(bounds=beyond))
    assert radar_engine._radar_partial_coverage('iem-mrms-lcref', None, dict(bounds=dict(n=56, s=40, w=-125, e=-110)))


# --------------------------------------------- A4 / L1: cache boot and retries
class Clock:
    def __init__(self):
        self.calls = []

    def schedule_once(self, callback, timeout=0):
        handle = SimpleNamespace(callback=callback, timeout=timeout, cancel=lambda: None)
        self.calls.append(handle)
        return handle

    schedule_interval = schedule_once


def test_a_failed_cache_boot_is_not_ready_is_reported_and_retries_with_backoff(make_emitter, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(ae, 'Clock', clock)
    e = make_emitter()
    e._runtime.running = True
    migrate = e.radar._migrate_cache
    failures = [OSError(28, 'No space left on device')] * 3
    def flaky(*args):
        if failures:
            raise failures.pop(0)
        return migrate(*args)
    monkeypatch.setattr(e.radar, '_migrate_cache', flaky)
    delays = []
    for attempt in range(1, 4):
        e.radar._start_inventory()
        assert e.radar._cache_done.wait(5)
        assert not e.radar._cache_ready.is_set()
        assert e.radar._cache_error['attempts'] == attempt
        delays.append(clock.calls[-1].timeout)
        # The backoff holds a pass's own start attempt back.
        e.radar._start_inventory()
        assert e.radar._cache_thread is None
        e.radar._cache_error['retryMono'] = 0  # the scheduled retry is due
    assert delays == [5, 10, 20]
    health = e.radar._health_payload()
    assert 'No space left' in health['cache']['initError']['error']
    summary = e.radar._health_summary(ae.time.time())
    assert summary['state'] == 'error' and 'No space left' in summary['initError']
    # Storage recovers: the scheduled retry reconciles and becomes ready.
    clock.calls[-1].callback(0)
    assert e.radar._cache_done.wait(5) and e.radar._cache_ready.is_set()
    assert e.radar._cache_error is None
    assert (Path(radar_engine.RADAR_DIR) / '.native-revision').read_text() == radar_engine._radar_render_revision('native')
    assert e.radar._health_summary(ae.time.time())['initError'] is None


def test_the_backoff_is_bounded():
    assert min(radar_engine.RADAR_CACHE_RETRY_MAX_SEC, radar_engine.RADAR_CACHE_RETRY_SEC * 2**20) == radar_engine.RADAR_CACHE_RETRY_MAX_SEC == 300


def test_a_retry_rescans_from_an_empty_inventory(tmp_path):
    root = tmp_path / 't'
    for stamp in ('209901010000', '209901010005'):
        p = root / 'iem-mrms-lcref' / '-' / stamp / '8' / '0' / '0.png'
        p.parent.mkdir(parents=True)
        p.write_bytes(b'x')
    cache = TileInventory()
    cache.add(('ghost', None, '209901010000', 8, 0, 0), tmp_path / 'ghost.png', 999, {})
    cache.scan(root, lambda *a: {})
    assert len(cache) == 2 and cache.bytes == 2 and ('ghost', None, '209901010000', 8, 0, 0) not in cache


def test_a_pass_in_the_lane_while_scanning_is_deferred_not_polled(make_emitter, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(ae, 'Clock', clock)
    e = make_emitter()
    e._runtime.running = True
    release = threading.Event()
    migrate = e.radar._migrate_cache
    def slow(*args):
        release.wait(5)
        return migrate(*args)
    monkeypatch.setattr(e.radar, '_migrate_cache', slow)
    e._runtime.inflight.add('radar')                  # production: always on the radar lane
    e.radar._acquire(intent_triggered=False)
    assert not clock.calls, 'no 100 ms re-arm while the scan runs'
    assert e.radar._cache_deferred
    release.set()
    assert e.radar._cache_done.wait(5) and e.radar._cache_ready.is_set()
    assert [c.callback for c in clock.calls if c.timeout == 0], 'the finished scan runs the deferred pass'
    assert not e.radar._cache_deferred


# ------------------------------------------------------- A5: radar-health.json
def test_wx_json_drops_health_and_radar_health_json_carries_it(make_emitter, hybrid):
    e = make_emitter()
    e._emit(0)
    wx = json.loads(Path(e.output_path).read_text())
    assert 'health' not in wx['radar']
    path = Path(e.output_path).with_name('radar-health.json')
    health = json.loads(path.read_text())
    assert {'breaker', 'cache', 'attention', 'requests', 'phases', 'summary', 'writtenTs'} <= health.keys()
    assert set(health['summary']) == {'state', 'newestObservationTs', 'newestObservationAgeSec', 'lastSuccessTs',
        'source', 'fallbackReason', 'coverage', 'nextAttemptTs', 'attentionTier', 'attentionReason', 'initError'}
    assert health['writtenTs'] == ae.time.time()
    # Throttled: an unchanged state is not rewritten within 15 s ...
    stamp = path.stat().st_mtime_ns
    real_time.sleep(.01)
    hybrid.mono += 5
    assert e.radar._write_health(ae.time.time()) is False and path.stat().st_mtime_ns == stamp
    hybrid.mono += 11
    assert e.radar._write_health(ae.time.time()) is True
    # ... but a state change is written at once.
    e.radar._attention.tier = 'dormant' if e.radar._attention.tier != 'dormant' else 'rest'
    hybrid.mono += 1
    assert e.radar._write_health(ae.time.time()) is True
    assert not list(path.parent.glob('radar-health.json.tmp*'))


def test_summary_states(make_emitter, hybrid, multisite, native):
    e = make_emitter()
    e.radar._boot_mono = ae.time.monotonic()
    e.radar._cache_ready.clear()
    assert e.radar._health_summary(ae.time.time())['state'] == 'starting'
    hybrid.view()
    e = make_emitter()
    e.radar._acquire()
    s = e.radar._health_summary(ae.time.time())
    assert s['state'] == 'current' and s['source'] == 'iem-nexrad-n0b' and s['coverage'] in ('full', 'partial')
    assert s['newestObservationAgeSec'] == int(ae.time.time()) - s['newestObservationTs']
    hybrid.mono += 3600
    assert e.radar._health_summary(ae.time.time())['state'] == 'stale'


# ---------------------------------------------- A6: discovery follows the site
@pytest.mark.parametrize('listing,expected', [(120, 120), (30, 60), (240, 240), (900, 600), (None, 300)])
def test_site_discovery_follows_the_listing_cadence(listing, expected):
    plan = DiscoverySchedule()
    snap = SimpleNamespace(ts_frame=3600, source_id='iem-nexrad-n0b', site_id='KATX', source_mode='site',
                           cadence=300, scan_cadence_sec=listing, frames=[dict(ts=3600)])
    plan.observe(snap, 3601, 300)
    assert plan.expected == 3600 + expected


# --------------------------------------------------- A7: boot expiry unopened
def test_boot_deletes_undisplayable_stamps_without_opening_them(tmp_path):
    root = tmp_path / 't'
    stamps = {'202610090000': 'old', '202610091200': 'new'}
    for stamp in stamps:
        for x in range(3):
            p = root / 'iem-mrms-lcref' / '-' / stamp / '8' / str(x) / '0.png'
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'x')
    opened = []
    cache = TileInventory()
    cache.scan(root, lambda path, source: opened.append(path) or {}, expire_before='202610091100')
    assert opened and all('202610091200' in str(p) for p in opened)
    assert not (root / 'iem-mrms-lcref' / '-' / '202610090000').exists()
    assert cache.startup['expiredStamps'] == 1 and len(cache) == 3


def test_the_retention_covers_everything_a_view_can_show():
    assert radar_engine.RADAR_CACHE_RETENTION_SEC == radar_engine.RADAR_HISTORY_SEC + max(
        radar_engine.RADAR_SITE_MAX_AGE_SEC, radar_engine.RADAR_IEM_STALE_SEC, radar_engine.RADAR_RAINVIEWER_STALE_SEC)


def test_the_emitter_boot_passes_the_retention_cutoff(make_emitter, hybrid, monkeypatch):
    seen = {}
    real = TileInventory.scan_roots
    def spy(self, roots, validate, entry_limit=None, expire_before=None):
        seen['cutoff'] = expire_before
        return real(self, roots, validate, entry_limit, expire_before)
    monkeypatch.setattr(TileInventory, 'scan_roots', spy)
    e = make_emitter()
    e.radar._start_inventory()
    assert e.radar._cache_ready.wait(5)
    expected = datetime.fromtimestamp(int(ae.time.time() - radar_engine.RADAR_CACHE_RETENTION_SEC), timezone.utc).strftime('%Y%m%d%H%M')
    assert seen['cutoff'] == expected


# ------------------------------------------- A9: forecast evidence is bounded
def signals(now, **values):
    base = dict(obs_age=60, rain_rate_mm=0, rain_wet=False, rain_starting=False, lightning_age=None,
                echo=False, echo_age=60, viewed_age=7200, touch_age=7200)
    base.update(values)
    return ra.Signals(now, **base)


def test_missing_forecast_data_holds_only_a_short_grace():
    a = ra.Attention(now=0)
    a.decide(signals(0, precip_pct=60, conditions='Rain until 4 PM', forecast_age=60))
    assert a.weather(0) and 'forecast' in a.reason
    # The fetch fails: forecast.py blanks the values (pct None, conditions '').
    a.decide(signals(600, precip_pct=None, conditions='', forecast_age=660))
    assert a.weather(600)
    a.decide(signals(600 + ra.FORECAST_MISSING_GRACE_SEC, precip_pct=None, conditions='', forecast_age=1560))
    assert not a.weather(600 + ra.FORECAST_MISSING_GRACE_SEC) and not a.forecast_on
    # 24 hours of missing data cannot revive it.
    a.decide(signals(86400, precip_pct=None, conditions='', forecast_age=86400))
    assert not a.forecast_on and 'forecast' not in a.reason


def test_an_unrefreshed_forecast_expires_with_its_last_update():
    a = ra.Attention(now=0)
    a.decide(signals(0, precip_pct=70, conditions='Showers', forecast_age=0))
    assert a.forecast_on
    # The same (frozen) values keep arriving but the forecast never updates.
    a.decide(signals(ra.FORECAST_VALID_SEC - 1, precip_pct=70, conditions='Showers', forecast_age=ra.FORECAST_VALID_SEC - 1))
    assert a.weather(ra.FORECAST_VALID_SEC - 1)
    a.decide(signals(ra.FORECAST_VALID_SEC + 1, precip_pct=70, conditions='Showers', forecast_age=ra.FORECAST_VALID_SEC + 1))
    assert not a.forecast_on
    # A fresh update renews it.
    a.decide(signals(ra.FORECAST_VALID_SEC + 10, precip_pct=70, conditions='Showers', forecast_age=5))
    assert a.forecast_on and a.weather(ra.FORECAST_VALID_SEC + 10)


def test_the_emitter_reads_the_forecast_update_time(make_emitter, hybrid):
    e = make_emitter(scenario=dict(Met={'PrecipPercnt': [80, '%'], 'Conditions': 'Rain', 'UpdatedTs': ae.time.time() - 7300}))
    assert e.radar._forecast_age(ae.time.time()) == pytest.approx(7300)
    assert make_emitter().radar._forecast_age(ae.time.time()) is None


def test_forecast_success_stamps_its_update_time():
    # Acquisition stamps it, not parsing (adversarial review): see
    # tests/test_radar_review_oct_adv.py for the re-parse and failure paths.
    source = Path('lib/forecast.py').read_text()
    success = source[source.index('def success_forecast'):source.index('def fail_forecast')]
    assert 'self.parse_forecast(acquired=int(UNIX.time()))' in success
    failure = source[source.index('def fail_forecast'):source.index('def parse_forecast')]
    assert 'UpdatedTs' not in failure


# ------------------------------------------------ L3: geo cache accounting
def test_geo_admission_does_not_walk_the_cache_per_tile(tmp_path, monkeypatch):
    walks = []
    real = bm.GeoCache._walk
    monkeypatch.setattr(bm.GeoCache, '_walk', lambda self: walks.append(1) or real(self))
    monkeypatch.setattr(bm, 'tile', lambda theme, z, x, y: b'x' * 100)
    for x in range(20):
        bm.cache_tile(tmp_path, 'paper', 8, x, 0)
    assert len(walks) == 1
    cache = bm.geo_cache(tmp_path)
    assert cache.count == 20 and cache.size == 2000


def test_geo_eviction_frees_to_the_low_water_mark(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, 'GEO_MAX_FILES', 10)
    monkeypatch.setattr(bm, 'tile', lambda theme, z, x, y: b'x' * 100)
    walks = []
    real = bm.GeoCache._walk
    monkeypatch.setattr(bm.GeoCache, '_walk', lambda self: walks.append(1) or real(self))
    for x in range(10):
        bm.cache_tile(tmp_path, 'paper', 8, x, 0)
        os.utime(bm.tile_path(tmp_path, 'paper', 8, x, 0), (x + 1, x + 1))
    bm.cache_tile(tmp_path, 'paper', 8, 10, 0)
    files = list((tmp_path / 'geo').rglob('*.png'))
    assert len(files) == 9 and not bm.tile_path(tmp_path, 'paper', 8, 0, 0).exists()
    before = len(walks)
    bm.cache_tile(tmp_path, 'paper', 8, 11, 0)
    assert len(walks) == before, 'the eviction pass left headroom'
