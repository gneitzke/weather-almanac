"""Third review: real viewport reuse, resource bounds and pass-thread isolation."""
import math
import os
import threading
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from PIL import Image  # noqa: F401 - exclude Pillow startup from render allocation samples

from lib import almanac_emit as ae, radar_level3 as l3, radar_mosaic as mosaic
from lib import radar_engine
from lib.radar_palette import source_palette
from tests.test_radar_mosaic import scan, classes, SOURCE, classified  # noqa: F401
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401
from tests.test_radar_level3 import native, tile_of  # noqa: F401
from tools.benchmark_radar_mosaic import viewport_grid, background_grids


@pytest.fixture
def candidates():
    return [scan(lat=a, lon=b, height=h) for a, b, h in
            [(47.68, -122.50, 196), (46.12, -122.43, 500),
             (47.12, -124.1, 80), (48.3, -121.0, 1000)]]


@pytest.mark.parametrize('z', [7, 8, 9, 10])
def test_real_viewport_eight_frames_survive_margin_and_prefetch(candidates, z):
    ctx = dict(zoom=z, camera_zoom=z, center=dict(lat=47.6, lon=-122.3))
    grid = [(x, y) for x, y, _, _ in radar_engine._radar_grid(ctx)]
    assert grid == viewport_grid(z)
    assert len(grid) == (15 if z == 7 else 12)
    mosaic.clear_geometry_cache()
    hits = misses = 0
    for frame in range(8):
        before = mosaic.geometry_cache_info()
        for x, y in grid:
            mosaic.mosaic_codes(candidates, z, x, y, 512 if z == 7 else 256)
        after = mosaic.geometry_cache_info()
        hits += after['hits'] - before['hits']
        misses += after['misses'] - before['misses']
        retained = tuple(mosaic._GEOMETRY)
        if frame < 7:
            for warm_z, tiles in background_grids(z):
                for x, y in tiles:
                    mosaic.mosaic_codes(candidates, warm_z, x, y, 512 if warm_z < 8 else 256,
                                        cache_geometry=False)
        assert tuple(mosaic._GEOMETRY) == retained, 'background must not admit or promote'
        assert mosaic.geometry_cache_info()['bytes'] == after['bytes']
    assert hits / (hits + misses) >= .85
    assert 0 < after['bytes'] <= mosaic.GEOMETRY_MAX_BYTES
    if z == 7:
        assert after['bytes'] < 36*1024**2
    mosaic.clear_geometry_cache()


def test_nonintersecting_sites_never_project_or_cache(monkeypatch):
    x, y = map(int, tile_of(47.6, -122.3, 10))
    distant = scan(lat=30, lon=-80)
    mosaic.clear_geometry_cache()
    def forbidden(*args, **kwargs):
        pytest.fail('off-disc site reached projection')
    monkeypatch.setattr(mosaic, '_geometry', forbidden)
    assert not mosaic.mosaic_codes([distant], 10, x, y).any()
    assert mosaic.geometry_cache_info() == dict(bytes=0, hits=0, misses=0, entries=0)


@pytest.mark.parametrize('lat,lon,z', [(47.6, -122.3, 7), (80, 179.8, 7), (47.6, -122.3, 10)])
def test_compact_projection_preserves_gate_and_disc_boundaries(lat, lon, z):
    source = scan(lat=lat, lon=lon)
    source.codes[:] = np.random.default_rng(31).integers(110, 255, source.codes.shape, dtype=np.uint8)
    cx, cy = map(int, tile_of(lat, lon, z))
    size = 512 if z == 7 else 256
    mosaic.clear_geometry_cache()
    for x, y in [(cx+dx, cy+dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]:
        rows, gates = l3.gate_lookup(source, z, x, y, size)
        la, lo = l3._tile_lonlat(z, x, y, size)
        a = math.radians(lat)
        hav = (np.sin((np.radians(la)[:, None]-a)/2)**2 + math.cos(a)*
               np.cos(np.radians(la)[:, None])*np.sin((np.radians(lo)[None, :]-math.radians(lon))/2)**2)
        covered = 2*l3.EARTH_RADIUS_M*np.arcsin(np.sqrt(np.minimum(1, hav))) <= 230000
        expected = np.where((rows >= 0) & covered, source.codes[np.maximum(rows, 0), gates], 0)
        actual = mosaic.mosaic_codes([source], z, x, y, size)
        np.testing.assert_array_equal(actual, expected)
    for region, bins, gates, altitude in mosaic._GEOMETRY.values():
        assert bins.dtype == gates.dtype == np.dtype('uint16')
        assert bins.max() < 3600 and gates.max() <= 32767
        assert altitude.dtype == np.dtype('float32')
        assert all(a.flags.owndata and not a.flags.writeable for a in (bins, gates, altitude))
    mosaic.clear_geometry_cache()


def test_culled_site_does_not_shift_classification_flags():
    x, y = map(int, tile_of(47.6, -122.3, 10))
    scans = [scan(lat=0), scan(0), scan(160, height=2000)]
    assert not mosaic.mosaic_codes(scans, 10, x, y, filtered=[False, True, False]).any()
    assert (mosaic.mosaic_codes(scans, 10, x, y, filtered=[True, False, True]) == 160).all()


@pytest.mark.parametrize('z', [7, 8, 9, 10])
def test_measured_cold_peak_fits_derived_slots(candidates, z):
    palette = source_palette(SOURCE)
    x, y = map(int, tile_of(47.6, -122.3, z))
    for count in range(1, 5):
        mosaic.clear_geometry_cache()
        tracemalloc.start()
        try:
            image, _ = mosaic.render_mosaic(candidates[:count], z, x, y, palette)
            image.close()
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert peak <= mosaic.render_peak_bytes(512 if z == 7 else 256, count)
    assert mosaic.RENDER_SLOT_COUNT * mosaic.RENDER_PEAK_BYTES <= mosaic.RENDER_TRANSIENT_BYTES
    assert mosaic.RENDER_PEAK_BYTES == mosaic.render_peak_bytes(512, 4)
    with pytest.raises(ValueError, match='four candidates'):
        mosaic.render_mosaic(candidates+[candidates[0]], z, x, y, palette)
    mosaic.clear_geometry_cache()


@pytest.mark.parametrize('prefetch,zoom,camera', [(False, 8, 8), (True, 8, 8), (False, 8, 9)])
def test_emitter_admits_only_foreground_viewport(make_emitter, hybrid, monkeypatch, prefetch, zoom, camera):
    emitter = make_emitter()
    ctx = dict(zoom=zoom, camera_zoom=camera, center=dict(lat=47.6, lon=-122.3),
               native=True, attention='live', prefetch=prefetch, mosaic_scans=[scan()],
               tile_workers=1)
    ctx['tiles'] = radar_engine._radar_grid(ctx, margin=1)
    seen = {}
    def render(scans, z, x, y, *args, **kwargs):
        seen[x, y] = kwargs['cache_geometry']
        # Record the actual batch boundary without exercising PNG persistence.
        raise ValueError('projection inspected')
    monkeypatch.setattr(mosaic, 'render_mosaic', render)
    monkeypatch.setattr(emitter.radar, '_checkpoint', lambda ctx: None)
    try:
        list(emitter.radar._tile_batch(SOURCE, hybrid.latest, ctx, 100, None, 'Mtest'))
        viewport = {(x, y) for x, y, _, _ in radar_engine._radar_grid(ctx)}
        assert len(seen) == len(ctx['tiles'])
        assert {tile for tile, admitted in seen.items() if admitted} == (
            viewport if not prefetch and zoom == camera else set())
    finally:
        emitter.stop()


def test_pass_finishes_while_ledger_fsync_is_stalled(make_emitter, hybrid, multisite, classified, monkeypatch):
    emitter = make_emitter()
    emitter.radar._acquire()
    ledger = emitter.radar._native_budget
    assert ledger.flush()
    entered, release = threading.Event(), threading.Event()
    write = ledger._write
    def blocked_write(current):
        if threading.current_thread().name == 'radar-ledger':
            entered.set()
            assert release.wait(5)
        return write(current)
    monkeypatch.setattr(ledger, '_write', blocked_write)
    with ledger.lock:
        ledger.last_write = ledger.monotonic()-3
    ledger.add(1)
    assert entered.wait(2)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        started = time.perf_counter()
        job = pool.submit(emitter.radar._acquire)
        job.result(timeout=2)
        print('Pass with blocked ledger fsync: %.2f ms' % ((time.perf_counter()-started)*1000))
        assert not release.is_set(), 'pass must not wait on the ledger writer'
    finally:
        release.set()
        pool.shutdown(wait=True)
        assert ledger.flush()
        emitter.stop()


def test_frame_executors_reused_and_stop_retires_both(make_emitter, hybrid, monkeypatch):
    emitter = make_emitter()
    pools = (emitter.radar._input_pool, emitter.radar._hca_pool)
    workers = {'N0B': set(), 'N0H': set()}
    lock = threading.Lock()
    def acquire(site, stamp, ctx, deadline, product='N0B', volume_ts=None):
        with lock:
            workers[product].add(threading.get_ident())
        return scan(ts=stamp+24) if product == 'N0B' else classes(60, ts=volume_ts)
    monkeypatch.setattr(emitter.radar, '_level3_scan', acquire)
    try:
        for frame in range(12):
            stamp = hybrid.latest+frame*60
            metadata, inputs = emitter.radar._mosaic_inputs([(s, stamp) for s in 'ABCD'], stamp, {}, 100)
            assert len(inputs) == 4 and not metadata['unfilteredSites']
            assert pools == (emitter.radar._input_pool, emitter.radar._hca_pool)
        assert all(0 < len(ids) <= 8 for ids in workers.values())
        print('12 frames, distinct input/HCA workers:', {p: len(ids) for p, ids in workers.items()})
    finally:
        emitter.stop()
        for pool in pools:
            pool.shutdown(wait=True)
    for pool in pools:
        with pytest.raises(RuntimeError, match='shutdown'):
            pool.submit(lambda: None).result()


def test_stalled_hca_has_next_frame_capacity_and_bounded_saturation(make_emitter, hybrid, monkeypatch):
    emitter = make_emitter()
    release = threading.Event()
    started = threading.Barrier(4)
    # Stalled work is held by events, never by the clock; the budget only has to
    # outlast thread scheduling of FRESH work on a loaded CI runner (a 30 ms
    # budget failed there once, 2026-09-25).
    monkeypatch.setattr(radar_engine, 'RADAR_N0H_FRAME_BUDGET_SEC', 1.0)
    calls = []
    def acquire(site, stamp, ctx, deadline, product='N0B', volume_ts=None):
        if product == 'N0B':
            return scan(ts=stamp+24)
        calls.append(stamp)
        if stamp != hybrid.latest+60:
            started.wait(timeout=2)
            assert release.wait(5)
        return classes(60, ts=volume_ts)
    monkeypatch.setattr(emitter.radar, '_level3_scan', acquire)
    def frame(offset):
        stamp = hybrid.latest+offset
        return emitter.radar._mosaic_inputs([(s, stamp) for s in 'ABCD'], stamp, {}, 100)[0]
    try:
        assert len(frame(0)['unfilteredSites']) == 4
        assert not frame(60)['unfilteredSites'], 'old stalled frame must leave four fresh HCA slots'
        assert len(frame(120)['unfilteredSites']) == 4
        for offset in (180, 240, 300):
            assert len(frame(offset)['unfilteredSites']) == 4
        assert len(calls) == 12, 'saturated work must fail admission, not queue or spawn more threads'
        assert len(emitter.radar._hca_pool._threads) <= 8
    finally:
        release.set()
        emitter.stop()
        emitter.radar._hca_pool.shutdown(wait=True)
        emitter.radar._input_pool.shutdown(wait=True)


def test_executor_saturation_does_not_grow_queue_or_threads():
    pool = radar_engine._RadarInputExecutor(1, 'radar-review3')
    entered, release = threading.Event(), threading.Event()
    def stalled():
        entered.set()
        assert release.wait(3)
    first = pool.submit(stalled)
    try:
        assert entered.wait(1)
        for _ in range(100):
            with pytest.raises(TimeoutError, match='occupied'):
                pool.submit(lambda: None).result()
        assert len(pool._threads) == 1
    finally:
        release.set()
        first.result(timeout=1)
        pool.shutdown(wait=True)


def test_late_cancelled_reflectivity_cannot_launch_hca_for_retired_frame(make_emitter, hybrid, monkeypatch):
    emitter = make_emitter()
    release = threading.Event()
    started = threading.Barrier(4)
    hca_stamps = []
    def acquire(site, stamp, ctx, deadline, product='N0B', volume_ts=None):
        if product == 'N0H':
            hca_stamps.append(stamp)
            return classes(60, ts=volume_ts)
        if stamp == hybrid.latest:
            started.wait(timeout=2)
            assert release.wait(5)
        return scan(ts=stamp+24)
    monkeypatch.setattr(emitter.radar, '_level3_scan', acquire)
    try:
        first, scans = emitter.radar._mosaic_inputs([(s, hybrid.latest) for s in 'ABCD'],
                                                   hybrid.latest, {}, .02)
        assert not scans and not first['siteScans']
        stamp = hybrid.latest+60
        second, scans = emitter.radar._mosaic_inputs([(s, stamp) for s in 'ABCD'], stamp, {}, 100)
        assert len(scans) == 4 and not second['unfilteredSites']
    finally:
        release.set()
        emitter.radar._input_pool.shutdown(wait=True)
        emitter.radar._hca_pool.shutdown(wait=True)
        emitter.stop()
    assert hca_stamps == [hybrid.latest+60]*4


def test_cancelled_wrapper_keeps_admission_until_dequeued(monkeypatch):
    # Freeze the executor's work dispatch, as if all worker threads have not
    # yet been scheduled. Cancel/retry must not grow a queue of dead wrappers.
    submitted = []
    original = radar_engine.ThreadPoolExecutor.submit
    def delayed(self, fn, *args, **kwargs):
        submitted.append(fn)
        return radar_engine.Future()
    monkeypatch.setattr(radar_engine.ThreadPoolExecutor, 'submit', delayed)
    pool = radar_engine._RadarInputExecutor(1, 'radar-review3')
    first = pool.submit(lambda: pytest.fail('cancelled function ran'))
    assert first.cancel()
    for _ in range(100):
        with pytest.raises(TimeoutError, match='occupied'):
            pool.submit(lambda: None).result()
    assert len(submitted) == 1
    submitted[0]()
    monkeypatch.setattr(radar_engine.ThreadPoolExecutor, 'submit', original)
    try:
        assert pool.submit(lambda: 42).result(timeout=1) == 42
    finally:
        pool.shutdown(wait=True)
