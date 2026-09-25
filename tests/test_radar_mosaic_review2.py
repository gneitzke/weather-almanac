"""Second mosaic review regressions. All products/transports are local fixtures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from lib import almanac_emit as ae, radar_mosaic as mosaic, radar_native_budget as budget
from lib.radar_palette import DISPLAY_FLOOR_DBZ
from lib.radar_level3 import floor_code
from tests.test_radar_mosaic import scan, classes, classified, SOURCE  # noqa: F401
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401
from tests.test_radar_level3 import native, tile_of  # noqa: F401
from tests.test_radar_mosaic_review import frame_context


@pytest.mark.parametrize('restart', [False, True])
def test_missing_reflectivity_upgrades_immutable_frame(make_emitter, hybrid, multisite, classified, monkeypatch, restart):
    hybrid.now = hybrid.latest + 60
    hybrid.view()  # Keep this multi-site/backfill scenario attended after moving the clock.
    for site in multisite.scans:
        multisite.scans[site] = [hybrid.latest]
    emitter = make_emitter()
    acquire = emitter._radar_level3_scan
    def unavailable(site, *args, **kwargs):
        if site == 'KMID' and kwargs.get('product', args[3] if len(args) > 3 else 'N0B') == 'N0B':
            raise TimeoutError('one reflectivity outage')
        return acquire(site, *args, **kwargs)
    monkeypatch.setattr(emitter, '_radar_level3_scan', unavailable)
    emitter._do_radar()
    partial = emitter._radar_result.frames[-1]
    assert 'KMID' not in {p['id'] for p in partial['siteScans']}
    assert emitter._radar_hca_due(partial)
    assert emitter._radar_mosaic_cached(partial['requestedPairs'], partial['ts'], frame_context(emitter)) is None
    if restart:
        emitter = make_emitter()
    else:
        monkeypatch.setattr(emitter, '_radar_level3_scan', acquire)
    hybrid.mono += 21
    emitter._do_radar(discovery=not restart, intent_triggered=restart)
    full = emitter._radar_result.frames[-1]
    assert full['complete'] and {p['id'] for p in full['siteScans']} == set(multisite.scans)
    assert full['mosaicKey'] != partial['mosaicKey']
    assert not emitter._radar_hca_due(full)


def test_missing_pair_negative_memory_and_window(make_emitter, hybrid):
    emitter = make_emitter(); hybrid.now = hybrid.latest+60
    frame = dict(requestedPairs=[['A', hybrid.latest]], siteScans=[])
    assert emitter._radar_hca_due(frame)
    emitter._radar_remember_level3_failure(('A', hybrid.latest), 10, 'timeout', TimeoutError)
    assert not emitter._radar_hca_due(frame)
    hybrid.mono += 11
    assert emitter._radar_hca_due(frame)
    hybrid.mono += ae.RADAR_N0H_UPGRADE_SEC
    assert not emitter._radar_hca_due(frame)
    frame['requestedPairs'] = [['A', hybrid.latest+180]]
    assert emitter._radar_hca_due(frame), 'a different volume is independent'


def test_cached_tile_eviction_never_stores_blank(make_emitter, hybrid, multisite, classified, monkeypatch):
    emitter = make_emitter(); emitter._do_radar()
    frame = emitter._radar_result.frames[-1]
    ctx = dict(emitter._radar_idle_context[1], builds=0, prefetch=True, deadline=100)
    victim = next(k for k, v in emitter._radar_disk_inventory.records.items()
                  if k[1] == frame['mosaicKey'] and v[2]['weatherPixels'] > 0)
    cached = emitter._radar_mosaic_cached
    def evict_after_check(*args):
        metadata = cached(*args)
        if metadata is not None:
            emitter._radar_invalidate_tile(victim)
        return metadata
    monkeypatch.setattr(emitter, '_radar_mosaic_cached', evict_after_check)
    layers = [(s, t, None) for s, t in frame['requestedPairs']]
    incomplete = emitter._radar_fill_frame(SOURCE, frame['ts'], ctx, 100, None, layers=layers)
    assert not incomplete['complete'] and victim not in emitter._radar_disk_inventory
    monkeypatch.setattr(emitter, '_radar_mosaic_cached', cached)
    repaired = emitter._radar_fill_frame(SOURCE, frame['ts'], ctx, 100, None, layers=layers)
    assert repaired['complete'] and repaired['mosaicKey'] == frame['mosaicKey']
    assert emitter._radar_disk_inventory.records[victim][2]['weatherPixels'] > 0


@pytest.mark.parametrize('code', [0, 1, floor_code(DISPLAY_FLOOR_DBZ)-1])
def test_filtered_clear_blocks_only_unfiltered_higher_echo(code):
    # Code 1 is missing, not a measurement of clear sky.
    x, y = map(int, tile_of(47.61, -122.33, 10))
    low, high = scan(code), scan(160, height=2000)
    result = mosaic.mosaic_codes([high, low], 10, x, y, filtered=[False, True])
    assert (result == (160 if code == 1 else 0)).all()
    assert (mosaic.mosaic_codes([high, low], 10, x, y, filtered=[True, True]) == 160).all()
    assert (mosaic.mosaic_codes([high, low], 10, x, y, filtered=[False, False]) == 160).all()


def test_filtered_floor_and_coverage_do_not_suppress_valid_fallback():
    x, y = map(int, tile_of(47.61, -122.33, 10))
    floor = floor_code(DISPLAY_FLOOR_DBZ)
    assert (mosaic.mosaic_codes([scan(floor), scan(180, height=2000)], 10, x, y,
                              filtered=[True, False]) == floor).all()
    assert (mosaic.mosaic_codes([scan(0, gates=1), scan(180, height=2000)], 10, x, y,
                              filtered=[True, False]) == 180).any()


def test_unfiltered_drawn_pixels_keep_caption_metadata(make_emitter, hybrid, multisite, classified):
    classified.missing.update(multisite.scans)
    emitter = make_emitter(); emitter._do_radar()
    frame = emitter._radar_result.frames[-1]
    assert frame['unfilteredSites'] == [p['id'] for p in frame['siteScans']]
    assert any(v[2]['weatherPixels'] for k, v in emitter._radar_disk_inventory.records.items()
               if k[1] == frame['mosaicKey'])


def test_prefetch_pending_classification_continues_region_targets(make_emitter, hybrid, multisite, classified, monkeypatch):
    emitter = make_emitter(); emitter._do_radar()
    source, ctx = emitter._radar_idle_context
    ctx = dict(ctx, zoom=8, camera_zoom=8, viewed=True, source_pref='site', deadline=100, refresh={'state': 'idle'})
    monkeypatch.setattr(emitter, '_radar_is_viewed', lambda: True)
    events = []
    emitter._radar_prefetched.clear()
    monkeypatch.setattr(emitter, '_radar_headroom_delay', lambda *args: 0)
    monkeypatch.setattr(emitter, '_radar_iem_scan', lambda ctx: (hybrid.latest, {}, 100))
    def unfinished(*args):
        events.append('pending')
        raise ae._RadarClassificationPending('still arriving')
    monkeypatch.setattr(emitter, '_radar_mosaic_cached', lambda *args: None)
    monkeypatch.setattr(emitter, '_radar_mosaic_inputs', unfinished)
    batch = emitter._radar_tile_batch
    def record(source, *args):
        events.append(source)
        yield from batch(source, *args)
    monkeypatch.setattr(emitter, '_radar_tile_batch', record)
    emitter._radar_prefetch(source, ctx)
    assert 'pending' in events
    assert 'iem-mrms-lcref' in events[events.index('pending')+1:]


def test_stalled_hca_does_not_occupy_next_frame_workers(make_emitter, hybrid, monkeypatch):
    emitter = make_emitter(); hybrid.now = hybrid.latest+60
    release, started = threading.Event(), threading.Barrier(4)
    # Stalled work is held by events, never by the clock; the budget only has to
    # outlast thread scheduling of FRESH work on a loaded CI runner (a 30 ms
    # budget failed there once, 2026-09-25).
    monkeypatch.setattr(ae, 'RADAR_N0H_FRAME_BUDGET_SEC', 1.0)
    def acquire(site, stamp, ctx, deadline, product='N0B', volume_ts=None):
        if product == 'N0B':
            return scan(ts=stamp+24)
        if stamp == hybrid.latest:
            started.wait(timeout=2)
            assert release.wait(3)
        return classes(60, ts=volume_ts)
    monkeypatch.setattr(emitter, '_radar_level3_scan', acquire)
    try:
        first, _ = emitter._radar_mosaic_inputs([(s, hybrid.latest) for s in 'ABCD'], hybrid.latest, {}, 100)
        assert len(first['unfilteredSites']) == 4
        assert all((s, hybrid.latest+24, 'N0H') in emitter._radar_level3_failed for s in 'ABCD')
        second, _ = emitter._radar_mosaic_inputs([(s, hybrid.latest+60) for s in 'ABCD'], hybrid.latest+60, {}, 100)
        assert len(second['siteScans']) == 4 and not second['unfilteredSites']
        hybrid.mono += 200
        assert not emitter._radar_hca_due(first)
        for stamp in range(500):
            emitter._radar_remember_level3_failure(('A', stamp, 'N0H'), 10, 'cancelled', TimeoutError)
        assert len(emitter._radar_level3_failed) <= 2*ae.RADAR_LEVEL3_SCAN_CACHE
    finally:
        release.set()
        emitter._radar_hca_pool.shutdown(wait=True)


def test_cancelled_hca_owner_records_volume_failure(make_emitter, hybrid, multisite, classified, monkeypatch):
    emitter = make_emitter()
    def cancel(*args, **kwargs):
        raise ae._RadarSuperseded('new intent')
    monkeypatch.setattr(emitter, '_radar_request', cancel)
    key = ('KNEA', hybrid.latest+24, 'N0H')
    with pytest.raises(ae._RadarSuperseded):
        emitter._radar_level3_scan('KNEA', hybrid.latest, {}, 100, 'N0H', key[1])
    assert key in emitter._radar_level3_failed and key not in emitter._radar_level3_flights


def test_sidecar_index_restart_no_globs_unchanged_no_fsync(make_emitter, hybrid, multisite, classified, monkeypatch):
    emitter = make_emitter(); emitter._do_radar()
    restart = make_emitter(); restart._do_radar()
    frame = restart._radar_result.frames[-1]
    assert emitter._radar_native_budget.flush()
    assert restart._radar_native_budget.flush()
    def forbidden(*args, **kwargs):
        raise AssertionError('unchanged read/write must not glob or fsync')
    monkeypatch.setattr(Path, 'glob', forbidden)
    monkeypatch.setattr(os, 'fsync', forbidden)
    ctx = frame_context(restart)
    for _ in range(8):
        metadata = restart._radar_mosaic_cached(frame['requestedPairs'], frame['ts'], ctx)
        assert metadata['mosaicKey'] == frame['mosaicKey']
    index = restart._radar_disk_inventory.frame_metadata
    path = next(p for p in index.paths(Path(ae.RADAR_DIR)/'t'/ae._radar_render_revision('native')/SOURCE,
                 ae._radar_stamp_text(frame['ts'])) if p.parent.parent.name == frame['mosaicKey'])
    value = json.loads(path.read_text())
    metadata = {k: v for k, v in value.items() if k not in ('stamp', 'revision')}
    mosaic.write_frame_metadata(path, value['stamp'], value['requestedPairs'], metadata, value['revision'], index)
    assert path in index.paths(path.parents[2], path.parent.name)
    for key in list(restart._radar_disk_inventory.records):
        if key[1] == frame['mosaicKey']:
            restart._radar_invalidate_tile(key)
    assert path not in index.paths(path.parents[2], path.parent.name)


def test_render_deadline_and_cpu_memory_slots(monkeypatch):
    assert mosaic.RENDER_SLOT_COUNT == max(1, min(os.cpu_count() or 1, 80_000_000//mosaic.RENDER_PEAK_BYTES))
    semaphore = threading.BoundedSemaphore(1)
    semaphore.acquire()
    monkeypatch.setattr(mosaic, '_RENDER_SLOTS', semaphore)
    start = time.monotonic()
    with pytest.raises(TimeoutError, match='slot deadline'):
        mosaic.render_mosaic([scan()], 10, 0, 0, [], deadline=start+.03)
    assert time.monotonic()-start < .2
    semaphore.release()
    with pytest.raises(ValueError, match='scan inputs'):
        mosaic.render_mosaic([], 10, 0, 0, [])


@pytest.mark.parametrize('levels,mib', [((8, 9, 10), 24), ((7, 8, 9), 48)])
def test_geometry_eight_frame_loop_and_byte_eviction(monkeypatch, levels, mib):
    mosaic.clear_geometry_cache()
    candidates = [scan(height=i*200) for i in range(4)]
    for _ in range(8):
        for z in levels:
            x, y = map(int, tile_of(47.61, -122.33, z))
            for dx, dy in ((0, 0), (0, 1), (1, 0), (1, 1)):
                mosaic.mosaic_codes(candidates, z, x+dx, y+dy, size=512 if z < 8 else 256)
    info = mosaic.geometry_cache_info()
    assert 0 < info.pop('bytes') <= mib*1024**2  # covered projections can be smaller
    assert info == dict(hits=336, misses=48, entries=48)
    monkeypatch.setattr(mosaic, 'GEOMETRY_MAX_BYTES', 512*1024)
    mosaic.clear_geometry_cache()
    x, y = map(int, tile_of(47.61, -122.33, 10))
    for dx in range(10):
        mosaic.mosaic_codes([candidates[0]], 10, x+dx, y)
        assert mosaic.geometry_cache_info()['bytes'] <= 512*1024
    assert mosaic.geometry_cache_info()['entries'] == 1
    mosaic.clear_geometry_cache()


def test_geometry_reuses_bins_but_not_volume_radial_rows():
    mosaic.clear_geometry_cache()
    source = scan(160)
    x, y = map(int, tile_of(source.lat, source.lon, 10))
    assert mosaic.mosaic_codes([source], 10, x, y).any()
    source.bearing_index[:] = -1
    assert not mosaic.mosaic_codes([source], 10, x, y).any()
    assert mosaic.geometry_cache_info()['hits'] == 1
    for attribute in ('height_m', 'elevation_deg', 'lat', 'lon'):
        setattr(source, attribute, getattr(source, attribute)+.01)
        mosaic.mosaic_codes([source], 10, x, y)
    assert mosaic.geometry_cache_info()['misses'] == 5


def test_ledger_writer_never_blocks_counter_and_drains_trailing_burst(tmp_path, monkeypatch):
    ledger = budget.NativeBudget(tmp_path/'bytes.json')
    entered, release = threading.Event(), threading.Event()
    writer_threads = set()
    replace = os.replace
    def slow_replace(*args):
        writer_threads.add(threading.current_thread().name)
        entered.set()
        assert release.wait(3)
        return replace(*args)
    monkeypatch.setattr(os, 'replace', slow_replace)
    try:
        start = time.perf_counter(); ledger.add(10)
        assert time.perf_counter()-start < .1 and entered.wait(1)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(ledger.add, [1]*100))
        assert ledger.bytes == 110
    finally:
        release.set()
    ledger.persist()
    end = time.perf_counter()+3
    while time.perf_counter() < end:
        if json.loads(ledger.path.read_text())['bytes'] == 110:
            break
        time.sleep(.02)
    assert json.loads(ledger.path.read_text())['bytes'] == 110
    assert writer_threads == {'radar-ledger'}


def test_ledger_hard_kill_loses_at_most_two_seconds(tmp_path):
    # The child emits timestamped bursts. SIGKILL runs no cleanup/atexit code.
    path = tmp_path/'ledger.json'
    program = '''
import sys,time
from lib.radar_native_budget import NativeBudget
ledger=NativeBudget(sys.argv[1])
for i in range(100):
    ledger.add(100)
    print(i+1, time.monotonic(), flush=True)
    time.sleep(.1)
'''
    process = subprocess.Popen([sys.executable, '-u', '-c', program, str(path)],
                               stdout=subprocess.PIPE, text=True)
    events = []
    try:
        for _ in range(34):
            count, stamp = process.stdout.readline().split()
            events.append((int(count), float(stamp)))
        process.kill(); process.wait(timeout=3)
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=3)
        process.stdout.close()
    saved = json.loads(path.read_text())['bytes']//100
    assert saved > 1, 'trailing writes must run independently of watcher/exit'
    assert events[-1][1]-events[saved-1][1] <= 2
    assert (events[-1][0]-saved)*100 <= 2000


def test_ledger_threshold_and_day_change_bypass_two_second_debounce(tmp_path):
    wall, mono = [1_800_000_000.], [0.]
    ledger = budget.NativeBudget(tmp_path/'ledger.json', lambda: wall[0], lambda: mono[0])
    ledger.add(1); ledger.persist()
    ledger.add(2); ledger.persist()
    assert json.loads(ledger.path.read_text())['bytes'] == 1
    ledger.add(budget.NATIVE_NEWEST_ONLY_BYTES); ledger.persist()
    assert json.loads(ledger.path.read_text())['bytes'] == budget.NATIVE_NEWEST_ONLY_BYTES+3
    ledger.add(budget.NATIVE_PAUSE_BYTES); ledger.persist()
    assert json.loads(ledger.path.read_text())['bytes'] == ledger.bytes
    wall[0] += 86400
    ledger.snapshot(); ledger.persist()
    assert json.loads(ledger.path.read_text())['bytes'] == 0


def test_prefetch_cached_eviction_does_not_record_completion(make_emitter, hybrid, multisite, classified, monkeypatch):
    emitter = make_emitter(); emitter._do_radar()
    source, ctx = emitter._radar_idle_context
    ctx = dict(ctx, viewed=True, deadline=100, refresh={'state': 'idle'})
    monkeypatch.setattr(emitter, '_radar_is_viewed', lambda: True)
    monkeypatch.setattr(emitter, '_radar_headroom_delay', lambda *args: 0)
    emitter._radar_prefetch(source, ctx)
    cached = emitter._radar_mosaic_cached
    evicted = []
    def evict(pairs, stamp, warm):
        metadata = cached(pairs, stamp, warm)
        if metadata is not None:
            victim = next(k for k in emitter._radar_disk_inventory.records
                          if k[1] == metadata['mosaicKey'] and k[3] == warm['zoom'])
            emitter._radar_invalidate_tile(victim)
            evicted.append((victim, (SOURCE, warm['zoom'], ctx['center']['lat'], ctx['center']['lon'])))
        return metadata
    monkeypatch.setattr(emitter, '_radar_mosaic_cached', evict)
    emitter._radar_prefetch(source, ctx)
    assert evicted
    for victim, round_key in evicted:
        assert victim not in emitter._radar_disk_inventory
        assert round_key not in emitter._radar_prefetched


def test_prefetch_stalled_hca_is_target_pending_not_budget(make_emitter, hybrid, monkeypatch):
    emitter = make_emitter(); release = threading.Event()
    monkeypatch.setattr(ae, 'RADAR_N0H_FRAME_BUDGET_SEC', .02)
    def acquire(site, stamp, ctx, deadline, product='N0B', volume_ts=None):
        if product == 'N0B':
            return scan(ts=stamp+24)
        assert release.wait(2)
        return classes(60, ts=volume_ts)
    monkeypatch.setattr(emitter, '_radar_level3_scan', acquire)
    try:
        with pytest.raises(ae._RadarClassificationPending):
            emitter._radar_mosaic_inputs([('A', hybrid.latest)], hybrid.latest, {'prefetch': True}, 100)
        assert ('A', hybrid.latest+24, 'N0H') in emitter._radar_level3_failed
    finally:
        release.set()
        emitter._radar_hca_pool.shutdown(wait=True)
