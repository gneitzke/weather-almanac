"""Offline regression coverage for the September 25 mosaic review."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier, Event, Lock

import numpy as np
import pytest

from lib import almanac_emit as ae, radar_level3 as l3, radar_mosaic as mosaic
from lib.radar_palette import DISPLAY_FLOOR_DBZ
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401
from tests.test_radar_level3 import native  # noqa: F401
from tests.test_radar_mosaic import classified, scan, classes, pixels, SOURCE  # noqa: F401


def frame_context(emitter):
    result = emitter._radar_result
    grid = result.tiles['grid']
    return dict(native=True, attention='live', zoom=result.zoom, site_id=result.site_id,
        inventory=emitter._radar_disk_inventory,
        tiles=[(x, y, 0, 0) for y in range(grid['y0'], grid['y0']+grid['h'])
               for x in range(grid['x0'], grid['x0']+grid['w'])])


@pytest.mark.parametrize('low', [0, 1, l3.floor_code(DISPLAY_FLOOR_DBZ)-1])
def test_low_clear_or_missing_does_not_hide_higher_echo(low):
    assert (pixels([scan(160, height=2000), scan(low)]) == 160).all()
    assert (pixels([scan(low), scan(0, height=2000)]) == 0).all()


def test_lowest_echo_wins_at_exact_floor_not_maximum():
    floor = l3.floor_code(DISPLAY_FLOOR_DBZ)
    assert (pixels([scan(180, height=2000), scan(floor)]) == floor).all()


def test_four_stalled_classifications_share_one_frame_wait(make_emitter, hybrid, monkeypatch):
    emitter = make_emitter()
    started, release = Barrier(4), Event()
    reflectivity = Barrier(4)
    requests = []
    # One shared wait, not four serial ones: serial would take >= 4 x .5 s.
    monkeypatch.setattr(ae, 'RADAR_N0H_FRAME_BUDGET_SEC', .5)
    def acquire(site, stamp, ctx, deadline, product='N0B', volume_ts=None):
        requests.append((site, product, deadline))
        if product == 'N0B':
            reflectivity.wait(timeout=2)  # would fail with serial N0B
            return scan(ts=stamp+24)
        started.wait(timeout=2)  # would fail with serial N0H
        assert release.wait(2)
        return classes(60, ts=volume_ts)
    monkeypatch.setattr(emitter, '_radar_level3_scan', acquire)
    start = time.perf_counter()
    try:
        metadata, inputs = emitter._radar_mosaic_inputs([(s, hybrid.latest) for s in ('A','B','C','D')],
            hybrid.latest, {}, 100)
        elapsed = time.perf_counter()-start
        assert len(inputs) == 4 and metadata['unfilteredSites'] == ['A','B','C','D']
        assert len(requests) == 8 and .45 <= elapsed < 1.5
        assert {end for _, product, end in requests if product == 'N0H'} == {98}
        # Flight deadlines do not grow serially; the coordinator alone owns
        # the .5 s wait and may reuse their late successes on the next pass.
    finally:
        release.set()
        emitter._radar_hca_pool.shutdown(wait=True)


def test_hca_starts_while_other_reflectivity_is_pending(make_emitter, hybrid, monkeypatch):
    emitter = make_emitter(); hca_started = Event()
    def acquire(site, stamp, ctx, deadline, product='N0B', volume_ts=None):
        if product == 'N0H':
            hca_started.set()
            return classes(60, ts=volume_ts)
        if site == 'B':
            assert hca_started.wait(2)
        return scan(ts=stamp+24)
    monkeypatch.setattr(emitter, '_radar_level3_scan', acquire)
    metadata, inputs = emitter._radar_mosaic_inputs([('A',hybrid.latest),('B',hybrid.latest)],hybrid.latest,{},100)
    assert len(inputs) == 2 and not metadata['unfilteredSites']


def test_restart_reuses_entire_loop_without_level3_requests(make_emitter, hybrid, multisite, native, classified):
    stamps = [hybrid.latest-i*120 for i in range(8)]
    for site in multisite.scans:
        multisite.scans[site] = sorted(stamps)
    emitter = make_emitter(); emitter._do_radar()
    frames = emitter._radar_result.frames
    assert len(frames) == 8 and all(f['complete'] for f in frames)
    native.calls.clear(); classified.calls.clear()
    restart = make_emitter()
    before = restart._radar_native_budget.snapshot()['bytesToday']
    restart._do_radar()
    assert len(restart._radar_result.frames) == 8
    assert all(f['complete'] for f in restart._radar_result.frames)
    assert not native.calls and not classified.calls
    assert restart._radar_native_budget.snapshot()['bytesToday'] == before
    ctx = frame_context(restart)
    for f in frames:
        assert restart._radar_frame_request_cost(SOURCE, ctx, f['requestedPairs'], frame_ts=f['ts']) == 0


@pytest.mark.parametrize('damage', ['key', 'volume', 'filtered', 'pairs', 'oversize'])
def test_sidecar_is_validated_before_reuse(make_emitter, hybrid, multisite, classified, damage):
    emitter = make_emitter(); emitter._do_radar()
    frame = emitter._radar_result.frames[-1]
    sidecar = next(Path(ae.RADAR_DIR).glob('t/*/%s/%s/*/frame.json' % (SOURCE,frame['mosaicKey'])))
    value = json.loads(sidecar.read_text())
    if damage == 'key': value['mosaicKey'] = 'M'+'0'*24
    elif damage == 'volume': value['siteScans'][0]['volumeTs'] += 60
    elif damage == 'filtered': value['siteScans'][0]['filtered'] = 'yes'
    elif damage == 'pairs': value['requestedPairs'][0][1] -= 60
    sidecar.write_text('x'*8193 if damage == 'oversize' else json.dumps(value))
    assert emitter._radar_mosaic_cached(frame['requestedPairs'],frame['ts'],frame_context(emitter)) is None


def test_missing_tile_costs_inputs_and_sidecar_prunes_with_last_tile(make_emitter, hybrid, multisite, classified):
    emitter = make_emitter(); emitter._do_radar()
    frame = emitter._radar_result.frames[-1]; ctx = frame_context(emitter)
    key = next(k for k in emitter._radar_disk_inventory.records if k[1] == frame['mosaicKey'])
    emitter._radar_invalidate_tile(key)
    assert emitter._radar_mosaic_cached(frame['requestedPairs'],frame['ts'],ctx) is None
    emitter._radar_level3_scans.clear(); emitter._radar_level3_listings.clear()
    assert emitter._radar_frame_request_cost(SOURCE,ctx,frame['requestedPairs'],frame_ts=frame['ts']) == 4*len(frame['siteScans'])
    emitter._radar_disk_inventory.MAX_FILES = 0
    emitter._radar_disk_inventory.evict()
    assert not list(Path(ae.RADAR_DIR).glob('t/*/%s/M*/*/frame.json' % SOURCE))


@pytest.mark.parametrize('count,kept', [(22,False),(23,True),(44,True)])
def test_bi_strict_majority_and_class_30_included(count, kept):
    source = scan(140); hca = classes(0)
    # The fixture's N0B row 0 maps to HCA row 0, across north.
    rows = [358,359,0,1,2]; cells = [(r,g) for r in rows for g in range(46,55) if (r,g)!=(0,50)]
    for row,gate in cells[:count]: hca.codes[row,gate] = 30
    hca.codes[0,50] = 10
    assert mosaic.quality_control(source,hca).codes[0,50] == (140 if kept else 0)
    source.codes[0,50] = 1
    assert mosaic.quality_control(source,hca).codes[0,50] == 1


def test_bi_range_does_not_wrap_and_gc_rf_still_fall_through():
    source = scan(); hca = classes(60)
    hca.codes[:,0] = 10
    # At range zero only 20/45 neighbours are precipitation; distant gates
    # must not wrap around and create a false majority.
    assert mosaic.quality_control(source,hca).codes[0,0] == 0
    for code in (20,150):
        hca.codes[0,50] = code
        assert mosaic.quality_control(source,hca).codes[0,50] == 1


def test_real_fixture_strong_bi_gates_preserved(capsys):
    path = Path('/tmp/claude-almanac/mosaic/n0b.bin')
    if not path.exists():
        pytest.skip('review-only N0B fixture is not distributed')
    source = l3.decode(path.read_bytes(),speckle_dbz=DISPLAY_FLOOR_DBZ)
    hca = l3.decode_n0h((Path(__file__).parent/'fixtures/n0h.bin').read_bytes())
    # Independent centre lookup, and a literal neighbourhood reference.
    bearings = np.arange(3600)*np.pi/1800; valid = source.bearing_index >= 0
    index = source.bearing_index[valid]
    angles = np.angle(np.bincount(index,weights=np.cos(bearings[valid]),minlength=source.radials)
                    +1j*np.bincount(index,weights=np.sin(bearings[valid]),minlength=source.radials))
    rows = hca.bearing_index[np.rint(np.degrees(angles)%360*10).astype(int)%3600]
    strong = source.codes[:,:hca.gates] >= l3.floor_code(25)
    bi = strong & (hca.codes[rows] == 10)
    qc = mosaic.quality_control(source,hca)
    before = int(bi.sum()); after = int((bi & (qc.codes[:,:hca.gates] == 0)).sum())
    preserved = 0
    for row,gate in zip(*np.nonzero(bi)):
        hrow = rows[row]
        count = sum(30 <= hca.codes[r%hca.radials,g] <= 120
                    for r in range(hrow-2,hrow+3) for g in range(max(0,gate-4),min(hca.gates,gate+5)))
        preserved += count > 22
    assert after == before-preserved and 0 < after < before
    print('BI >=25 dBZ cleared before=%d after=%d preserved=%d' % (before,after,preserved))


def test_missing_hca_eventually_allows_unchanged_and_stops_listing(make_emitter, hybrid, multisite, classified, monkeypatch):
    classified.missing.add('KMID')
    emitter = make_emitter(); emitter._do_radar()
    requests = len(classified.calls); builds = []
    original = emitter._radar_fill_frame
    def fill(*args,**kwargs):
        builds.append(args[1]); return original(*args,**kwargs)
    monkeypatch.setattr(emitter,'_radar_fill_frame',fill)
    for _ in range(5):
        hybrid.mono += 61
        emitter._do_radar(discovery=True,intent_triggered=False)
    assert not builds and len(classified.calls) == requests


def test_negative_volume_stays_negative_after_window_but_new_volume_can_fetch(make_emitter, hybrid, multisite, classified):
    classified.missing.add('KNEA'); emitter = make_emitter(); emitter._do_radar()
    count = len(classified.calls); hybrid.mono += 61
    with pytest.raises(ValueError,match='not published'):
        emitter._radar_level3_scan('KNEA',hybrid.latest,{},100,'N0H',hybrid.latest+24)
    assert len(classified.calls) == count
    with pytest.raises(ValueError,match='not published'):
        emitter._radar_level3_scan('KNEA',hybrid.latest+60,{},100,'N0H',hybrid.latest+84)
    assert len(classified.calls) > count, 'negative knowledge belongs to one volume'
    assert len(emitter._radar_level3_failed) <= 2*ae.RADAR_LEVEL3_SCAN_CACHE


def test_mosaic_render_concurrency_is_bounded(monkeypatch):
    lock = Lock(); release = Event(); two = Event(); active = peak = 0
    def render(*args):
        nonlocal active,peak
        with lock:
            active += 1; peak = max(peak,active)
            if active == mosaic.RENDER_SLOT_COUNT: two.set()
        assert release.wait(2)
        with lock: active -= 1
    monkeypatch.setattr(mosaic,'_render_mosaic',render)
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = [pool.submit(mosaic.render_mosaic,[scan()],7,0,0,[]) for _ in range(6)]
        assert two.wait(2)
        assert peak == mosaic.RENDER_SLOT_COUNT
        release.set()
        for job in jobs: job.result()
    assert peak == mosaic.RENDER_SLOT_COUNT


def test_prefetch_classification_budget_refusal_is_control_flow(make_emitter, hybrid, monkeypatch):
    emitter = make_emitter()
    def acquire(site, stamp, ctx, deadline, product='N0B', volume_ts=None):
        if product == 'N0H': raise ae._RadarBudget('no classification headroom')
        return scan(ts=stamp+24)
    monkeypatch.setattr(emitter,'_radar_level3_scan',acquire)
    with pytest.raises(ae._RadarBudget,match='headroom'):
        emitter._radar_mosaic_inputs([('KNEA',hybrid.latest)],hybrid.latest,{'prefetch':True},100)
    assert not emitter._radar_prefetched


def test_late_hca_upgrades_all_eligible_retained_frames(make_emitter, hybrid, multisite, classified, monkeypatch):
    hybrid.now = hybrid.latest+60
    hybrid.view()  # Keep this multi-site/backfill scenario attended after moving the clock.
    for site in multisite.scans: multisite.scans[site] = [hybrid.latest-60,hybrid.latest]
    classified.missing.add('KNEA')
    emitter = make_emitter(); emitter._do_radar()
    old = {f['ts']:f['mosaicKey'] for f in emitter._radar_result.frames}
    assert len(old) == 2 and all(f['unfilteredSites'] for f in emitter._radar_result.frames)
    classified.missing.clear(); hybrid.mono += 21
    emitter._do_radar(discovery=True,intent_triggered=False)
    assert len(emitter._radar_result.frames) == 2
    for f in emitter._radar_result.frames:
        assert f['complete'] and not f['unfilteredSites'] and f['mosaicKey'] != old[f['ts']]


def test_listing_eviction_keeps_current_hour_even_if_inserted_first(make_emitter, hybrid, multisite, classified):
    emitter = make_emitter(); emitter._do_radar()
    cap = ae.RADAR_LEVEL3_LISTING_CACHE
    assert cap >= ae.RADAR_SITE_MAX_COUNT*len(ae.RADAR_LEVEL3_TRANSPORTS)*3
    emitter._radar_level3_listings.clear(); emitter._radar_level3_scans.clear()
    current = ('KNEW','NEW_N0B_2026_09_13_00')
    emitter._radar_level3_listings[current] = (0,())
    for i in range(cap-1):
        emitter._radar_level3_listings[('K%03d'%i,'%03d_N0B_2026_09_12_23'%i)] = (i+1,())
    emitter._radar_level3_scan('KNEA',hybrid.latest,{},100)
    assert len(emitter._radar_level3_listings) == cap and current in emitter._radar_level3_listings
    assert ('K000','000_N0B_2026_09_12_23') not in emitter._radar_level3_listings


def test_sidecar_atomic_replace_preserves_previous_identity(tmp_path, monkeypatch):
    import os
    pairs = [('KATX',100020)]
    metadata = dict(mosaicKey='M'+'a'*24,
                    siteScans=[dict(id='KATX',ts=100020,volumeTs=100024,filtered=True)])
    path = tmp_path/'frame.json'
    mosaic.write_frame_metadata(path,'197001020347',pairs,metadata,'revision')
    original = path.read_bytes()
    def fail(*args): raise OSError('simulated replace failure')
    monkeypatch.setattr(os,'replace',fail)
    with pytest.raises(OSError,match='replace failure'):
        mosaic.write_frame_metadata(path,'197001020347',pairs,dict(metadata,mosaicKey='M'+'b'*24),'revision')
    assert path.read_bytes() == original and list(tmp_path.iterdir()) == [path]


def test_server_does_not_publish_private_frame_sidecars(monkeypatch, tmp_path):
    from tests.test_freshness_health import _load_serve, _payload
    module = _load_serve(monkeypatch,tmp_path,_payload())
    revision = ae._radar_render_revision('native')
    path = tmp_path/'radar'/'t'/revision/SOURCE/('M'+'a'*24)/'202609251307'/'frame.json'
    path.parent.mkdir(parents=True); path.write_text('{}')
    (tmp_path/'radar'/'.native-revision').write_text(revision)
    handler = object.__new__(module.Handler)
    handler.path = '/'+str(path.relative_to(tmp_path)); handler.directory = str(tmp_path)
    errors = []; handler.send_error = lambda code,*args: errors.append(code)
    assert handler.send_head() is None and errors == [404]


def test_missing_hca_only_in_backfill_arms_retry_and_upgrades(make_emitter, hybrid, multisite, classified, monkeypatch):
    hybrid.now = hybrid.latest+60
    hybrid.view()  # Keep this multi-site/backfill scenario attended after moving the clock.
    for site in multisite.scans: multisite.scans[site] = [hybrid.latest-60,hybrid.latest]
    emitter = make_emitter(); retry = []; pending = [True]
    acquire = emitter._radar_level3_scan
    def delayed(site, stamp, ctx, deadline, product='N0B', volume_ts=None):
        if product == 'N0H' and volume_ts == hybrid.latest-36 and pending[0]:
            raise ValueError('older HCA not published yet')
        return acquire(site,stamp,ctx,deadline,product,volume_ts)
    monkeypatch.setattr(emitter,'_radar_level3_scan',delayed)
    monkeypatch.setattr(emitter,'_schedule_retry',lambda key,callback,timeout,**kw: retry.append((key,timeout)))
    emitter._do_radar()
    old, newest = emitter._radar_result.frames
    assert old['unfilteredSites'] and not newest['unfilteredSites']
    assert ('radar',20) in retry
    pending[0] = False; hybrid.mono += 21
    emitter._do_radar(discovery=True,intent_triggered=False)
    assert all(f['complete'] and not f['unfilteredSites'] for f in emitter._radar_result.frames)
    assert emitter._radar_result.frames[0]['mosaicKey'] != old['mosaicKey']
    assert emitter._radar_result.frames[-1]['mosaicKey'] == newest['mosaicKey']
