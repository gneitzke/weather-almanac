"""Per-pixel ownership, immutable wire identity and native engine integration."""
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import numpy as np
import pytest

from lib import almanac_emit as ae, radar_level3 as l3, radar_mosaic as mosaic
from lib.radar_palette import source_palette
from tests.test_radar_level3 import product, tile_of, gate_code, native  # noqa: F401
from tests.test_radar_n0h import n0h_product
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401
from tests.test_freshness_health import _load_serve, _payload

SOURCE = 'iem-nexrad-n0b'


def scan(code=140, height=0, lat=47.61, lon=-122.33, gates=1840, elevation=.5, ts=1789257600):
    return l3.Scan(lat, lon, height, elevation, 215, ts,
        np.full((720, gates), code, np.uint8), np.arange(3600, dtype=np.int16)//5)


def classes(code, ts=1789257600):
    return l3.decode_n0h(n0h_product(classes=np.full((360, 1200), code, np.uint8), volume_ts=ts))


def pixels(scans):
    x, y = map(int, tile_of(47.61, -122.33, 10))
    return mosaic.mosaic_codes(scans, 10, x, y)


@pytest.mark.parametrize('low,high,classification,expected', [
    (110, 160, None, 110), (0, 160, None, 160), (80, 160, None, 160),
    (1, 160, None, 160), (110, 160, 20, 160), (110, 160, 150, 160),
    (110, 160, 10, 160), (110, 160, 0, 110), (110, 160, 140, 110),
    (110, 160, 60, 110), (1, 160, 10, 160),
])
def test_selection_table(low, high, classification, expected):
    near, far = scan(low), scan(high, height=1500)
    near = mosaic.quality_control(near, None if classification is None else classes(classification))
    assert (pixels([far, near]) == expected).all()


def test_beam_order_changes_across_pixels_and_actual_elevation_matters():
    left, right = scan(110, lon=-123), scan(160, lon=-122)
    x, y = map(int, tile_of(47.6, -122.5, 8))
    result = mosaic.mosaic_codes([left, right], 8, x, y)
    assert {110, 160} <= set(np.unique(result))
    # Same location but higher antenna or elevation loses regardless of list order.
    assert (pixels([scan(160, elevation=2), scan(110, elevation=-.5)]) == 110).all()


def test_no_data_outside_gate_range_disc_or_bearing():
    assert (pixels([scan(110, gates=1), scan(160, height=1500)]) == 160).any()
    assert (pixels([scan(110, lat=40)]) == 0).all()
    missing = scan(); missing.bearing_index[:] = -1
    assert (pixels([missing, scan(160, height=1500)]) == 160).all()
    assert not pixels([]).any()


def test_hca_uses_real_bearings_and_stops_at_300km():
    near = scan(); hca = classes(60)
    hca.codes[0, :] = 20  # 359.7..0.6 degrees, not N0B row//2
    filtered = mosaic.quality_control(near, hca)
    assert filtered.codes[0, 50] == 1
    assert filtered.codes[1, 50] == 140
    assert filtered.codes[719, 50] == 1
    assert filtered.codes[0, 1200] == 140
    assert (near.codes == 140).all(), 'QC must not mutate a cached N0B scan'
    with pytest.raises(ValueError, match='volume'):
        mosaic.quality_control(near, classes(60, ts=near.volume_ts+1))


def test_identity_is_sorted_exact_and_qc_sensitive():
    pairs = [('KATX', 100024, False), ('KLGX', 99984, True)]
    key = mosaic.mosaic_key(pairs)
    assert key == mosaic.mosaic_key(pairs[::-1]) and len(key) == 25
    assert key != mosaic.mosaic_key(pairs[:1])
    assert key != mosaic.mosaic_key([('KATX', 100024, True), pairs[1]])
    assert key != mosaic.mosaic_key([('KATX', 100025, False), pairs[1]])
    assert key != mosaic.mosaic_key(pairs, revision='next')


def test_time_selection_drops_stale_missing_and_accepts_future_minute():
    ctx = dict(native=True, attention='live', sites=[dict(id=i, reporting=True) for i in 'ABCDE'],
               site_scans=dict(A=[500, 1000, 1060, 1061], B=[519], C=[], D=[520], E=[700, 900]))
    assert ae._radar_site_pairs(ctx, 1000) == (('A', 1060), ('D', 520), ('E', 900))
    ctx['native'] = False
    assert ae._radar_site_pairs(ctx, 1000) == (('A', 1000), ('B', 519), ('D', 520), ('E', 900))


@pytest.fixture
def classified(native, multisite, monkeypatch):
    """Extend the native fixture's mocked S3 routes with real-layout HCA."""
    state = type('HCA', (), {})()
    state.calls, state.missing, state.bad = [], set(), set()
    opened = ae.RadarSession.open
    def fetch(self, req, timeout):
        url = req.full_url
        if url.startswith(ae.RADAR_LEVEL3_BUCKET) and '_N0H_' in url:
            if '?' in url:
                prefix = parse_qs(urlsplit(url).query)['prefix'][0]
                site = 'K'+prefix[:3]
                state.calls.append(('list', prefix))
                keys = ''.join('<Key>%s_N0H_%s</Key>' % (site[1:], datetime.fromtimestamp(t+24, timezone.utc).strftime('%Y_%m_%d_%H_%M_%S'))
                               for t in multisite.scans[site] if site not in state.missing)
                return io.BytesIO(('<ListBucketResult>'+keys+'</ListBucketResult>').encode())
            key = url.rsplit('/', 1)[1]; state.calls.append(('get', key)); site = 'K'+key[:3]
            if site in state.bad:
                return io.BytesIO(b'invalid HCA'*30)
            a, b, _ = ae._NEXRAD_SITES[site]
            return io.BytesIO(n0h_product(lat=a, lon=b, volume_ts=l3.s3_key_time(key, 'N0H')))
        return opened(self, req, timeout)
    monkeypatch.setattr(ae.RadarSession, 'open', fetch)
    return state


def test_engine_one_mosaic_layer_metadata_ledger_and_restart(make_emitter, hybrid, multisite, classified):
    emitter = make_emitter(); emitter._do_radar()
    result = emitter._radar_result
    assert result.tiles['variant'] == 'native'
    newest = result.frames[-1]
    assert newest['complete'] and newest['mosaicKey'].startswith('M')
    assert newest['siteScans'] and not newest['unfilteredSites']
    assert all(p['filtered'] for p in newest['siteScans'])
    assert not [c for c in multisite.calls if c[0]=='tile']
    assert len([c for c in classified.calls if c[0]=='get']) == len(set(c[1] for c in classified.calls if c[0]=='get'))
    records = {k:v for k,v in emitter._radar_disk_inventory.records.items() if k[0]==SOURCE}
    assert records and all(k[1].startswith('M') for k in records)
    assert any(v[2]['weatherPixels'] for v in records.values())
    assert emitter._radar_native_budget.snapshot()['bytesToday'] > 0
    assert emitter._radar_inventory_valid(result)
    first = {k: v[0].read_bytes() for k,v in records.items()}
    restart = make_emitter(); restart._do_radar()
    assert set(records) <= set(restart._radar_disk_inventory.records)
    assert all(restart._radar_disk_inventory.records[k][0].read_bytes()==v for k,v in first.items())


@pytest.mark.parametrize('failure', ['missing', 'bad'])
def test_hca_failure_never_blocks_and_late_hca_has_new_identity(make_emitter, hybrid, multisite, classified, failure):
    hybrid.now = hybrid.latest + 60  # late classification is eligible for 180 s
    getattr(classified, failure).add('KNEA')
    emitter = make_emitter(); emitter._do_radar()
    frame = emitter._radar_result.frames[-1]
    assert frame['complete'] and 'KNEA' in frame['unfilteredSites']
    old = frame['mosaicKey']
    old_tiles = {k:v[0].read_bytes() for k,v in emitter._radar_disk_inventory.records.items() if k[1]==old}
    assert old_tiles
    getattr(classified, failure).clear(); hybrid.mono += 61
    emitter._do_radar(discovery=True, intent_triggered=False)
    latest = emitter._radar_result.frames[-1]
    assert latest['complete'] and not latest['unfilteredSites'] and latest['mosaicKey'] != old
    assert all(emitter._radar_disk_inventory.records[k][0].read_bytes()==v for k,v in old_tiles.items())


def test_native_admission_counts_two_products_and_listings(make_emitter, hybrid, multisite, classified):
    emitter = make_emitter()
    tiles, *_ = ae._radar_viewport(47.61, -122.33, 8, 956, 490)
    ctx = dict(native=True, attention='live', zoom=8, tiles=tiles, inventory=emitter._radar_disk_inventory)
    assert emitter._radar_frame_request_cost(SOURCE, ctx, [('KNEA', hybrid.latest)]) == 4


def test_server_accepts_only_native_mosaic_identity(monkeypatch, tmp_path):
    module = _load_serve(monkeypatch, tmp_path, _payload())
    monkeypatch.setattr(module.http.server.SimpleHTTPRequestHandler, 'do_GET', lambda h: None)
    revision = ae._radar_render_revision('native')
    root = tmp_path/'radar'; root.mkdir(exist_ok=True)
    (root/'.native-revision').write_text(revision)
    key = mosaic.mosaic_key([('KATX', 100000, True)])
    relative = f'radar/t/{revision}/{SOURCE}/{key}/202609251307/8/40/80.png'
    path=tmp_path/relative; path.parent.mkdir(parents=True); path.write_bytes(b'png')
    h = object.__new__(module.Handler); h.client_address=('127.0.0.1', 1); h.path='/'+relative
    h.directory=str(tmp_path)
    h.send_error=lambda code, *args: setattr(h, 'error', code)
    monkeypatch.setattr(module.http.server.SimpleHTTPRequestHandler, 'send_head', lambda h: None)
    h.send_head()
    assert h._immutable_radar
    h.path=h.path.replace(key, key+'f'); h.send_head(); assert not h._immutable_radar


def test_missing_or_stale_site_does_not_hold_other_inputs(make_emitter, hybrid, multisite, classified, monkeypatch):
    emitter = make_emitter()
    t = hybrid.latest
    values = {'KNEA': scan(ts=t+24), 'KMID': scan(160, ts=t-60+24)}
    requests = []
    def acquire(site, stamp, ctx, deadline, product='N0B', volume_ts=None):
        requests.append((site, product))
        if product=='N0H' or site=='KFAR':
            raise ValueError('absent')
        return values[site]
    monkeypatch.setattr(emitter, '_radar_level3_scan', acquire)
    metadata, inputs = emitter._radar_mosaic_inputs([('KNEA',t),('KMID',t-481),('KFAR',t)],t,{},100)
    assert [p['id'] for p in metadata['siteScans']] == ['KNEA']
    assert len(inputs)==1 and metadata['unfilteredSites']==['KNEA']
    assert not any(site=='KMID' for site,_ in requests)


def test_optional_hca_budget_or_deadline_retains_n0b(make_emitter, hybrid, multisite, classified, monkeypatch):
    emitter=make_emitter(); t=hybrid.latest
    def acquire(site, stamp, ctx, deadline, product='N0B', volume_ts=None):
        if product=='N0H': raise ae._RadarBudget('HCA deferred')
        return scan(ts=t+24)
    monkeypatch.setattr(emitter, '_radar_level3_scan', acquire)
    for deadline in (100, 1):
        metadata, inputs=emitter._radar_mosaic_inputs([('KNEA',t)],t,{},deadline)
        assert len(inputs)==1 and metadata['unfilteredSites']==['KNEA']


def test_hca_single_flight_exact_second_and_failed_volume_cache(make_emitter, hybrid, multisite, classified, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import time
    emitter=make_emitter(); t=hybrid.latest; started=Event(); release=Event(); requests=[]
    monkeypatch.setattr(emitter, '_radar_checkpoint', lambda ctx: None)
    prefix=datetime.fromtimestamp(t+24,timezone.utc).strftime('%Y_%m_%d_%H_%M_')
    keys=[f'NEA_N0H_{prefix}23', f'NEA_N0H_{prefix}24']
    def request(source,url,deadline,**kwargs):
        requests.append(url)
        if '?' in url:
            kwargs['validate'](('<ListBucketResult>'+''.join('<Key>'+k+'</Key>' for k in keys)+'</ListBucketResult>').encode())
        else:
            started.set(); assert release.wait(2)
            a,b,_=ae._NEXRAD_SITES['KNEA']
            kwargs['validate'](n0h_product(lat=a,lon=b,volume_ts=t+24))
    monkeypatch.setattr(emitter, '_radar_request', request)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures=[pool.submit(emitter._radar_level3_scan,'KNEA',t,{},100,'N0H',t+24) for _ in range(4)]
        assert started.wait(2); release.set()
        results=[f.result(3) for f in futures]
    assert all(s is results[0] for s in results)
    assert len(requests)==2 and requests[-1].endswith(keys[1])
    assert not emitter._radar_level3_flights
    before=len(requests)
    for _ in range(2):
        with pytest.raises(ValueError, match='not published'):
            emitter._radar_level3_scan('KNEA',t,{},100,'N0H',t+25)
    assert len(requests)==before


def test_bad_mosaic_tile_consumption_and_pinning(make_emitter, hybrid, multisite, classified, tmp_path):
    emitter=make_emitter(); emitter._do_radar()
    frame=emitter._radar_result.frames[-1]
    cache=emitter._radar_disk_inventory
    key=next(k for k in cache.records if k[1]==frame['mosaicKey'] and k[3]==emitter._radar_result.zoom)
    path=cache.records[key][0]
    pinned=[]
    original_evict=cache.evict
    cache.MAX_FILES=0
    cache.evict=lambda protected, *args: pinned.extend(protected)
    emitter._radar_prune()
    assert key in pinned
    cache.evict=original_evict; cache.MAX_FILES=8000
    path.write_bytes(b'broken PNG')
    (tmp_path/'radar_bad_tiles').write_text(json.dumps([str(path.relative_to(tmp_path))]))
    emitter._radar_consume_bad_tiles()
    assert key not in cache and not path.exists()
    emitter._do_radar()
    assert path.exists() and key in cache
    assert ae._radar_tile_metadata(path, SOURCE)['remapped']


def test_mosaic_echo_is_counted_once_and_matches_metadata(make_emitter, hybrid, multisite, classified):
    emitter=make_emitter(); emitter._do_radar(); result=emitter._radar_result
    frame=result.frames[-1]; g=result.tiles['grid']
    ctx=dict(native=True,attention='live',zoom=result.zoom,inventory=emitter._radar_disk_inventory,
             tiles=[(x,y,0,0) for y in range(g['y0'],g['y0']+g['h']) for x in range(g['x0'],g['x0']+g['w'])])
    assert emitter._radar_frame_echo(ctx,SOURCE,ae._radar_frame_pairs(frame),frame['ts']) is not None
    assert emitter._radar_echo_pixels['tiles']==g['w']*g['h']


def test_n0h_provider_failure_cannot_open_n0b_circuit(make_emitter, hybrid, multisite, classified):
    hybrid.now = hybrid.latest + 60
    hybrid.view()  # Keep this multi-site/backfill scenario attended after moving the clock.
    emitter=make_emitter(); emitter._do_radar()
    for key in list(emitter._radar_level3_scans):
        if len(key)==3: del emitter._radar_level3_scans[key]
    emitter._radar_n0h_health.hosts.clear()
    for _ in range(6):
        emitter._radar_n0h_health.record(ae.RADAR_N0H_TRANSPORT, ae.RADAR_LEVEL3_BUCKET, False, ValueError('bad HCA'))
    state=emitter._radar_n0h_health._host(ae.RADAR_N0H_TRANSPORT,ae.RADAR_LEVEL3_BUCKET)
    assert state['until'] > hybrid.mono
    # Complete filtered tiles now survive cache loss/circuit failure. Exercise
    # a genuinely new volume, which requires optional classification admission.
    multisite.scans['KNEA'].append(hybrid.latest+60)
    hybrid.now += 60
    emitter._do_radar()
    frame=emitter._radar_result.frames[-1]
    assert frame['complete'] and frame['unfilteredSites']
    assert emitter._radar_health._host(ae.RADAR_LEVEL3_TRANSPORT,ae.RADAR_LEVEL3_BUCKET)['until']==0
    hybrid.mono+=31
    emitter._do_radar(discovery=True,intent_triggered=False)
    assert not emitter._radar_result.frames[-1]['unfilteredSites']
    assert emitter._radar_n0h_health._host(ae.RADAR_N0H_TRANSPORT,ae.RADAR_LEVEL3_BUCKET)['until']==0


def test_supersampling_after_selection_and_original_palette():
    palette=source_palette(SOURCE)
    for z in (7, 8, 9, 10):
        x,y=map(int,tile_of(47.61,-122.33,z)); samples=512 if z==7 else 256
        candidates=[scan(1),scan(146,height=1500)]
        expected=mosaic.mosaic_codes(candidates,z,x,y,samples)
        if z==7: expected=expected.reshape(256,2,256,2).max(axis=(1,3))
        image,visible=mosaic.render_mosaic(candidates,z,x,y,palette)
        slots,colours=l3.colour_table(palette)
        assert np.array_equal(np.asarray(image),slots[expected])
        assert visible==np.count_nonzero(slots[expected])
        image.close()


def test_mosaic_identity_includes_full_render_revision(make_emitter, hybrid, multisite, classified, monkeypatch):
    emitter=make_emitter(); t=hybrid.latest
    def acquire(site, stamp, ctx, deadline, product='N0B', volume_ts=None):
        if product=='N0H': raise ValueError('absent')
        return scan(ts=t+24)
    monkeypatch.setattr(emitter,'_radar_level3_scan',acquire)
    first,_=emitter._radar_mosaic_inputs([('KNEA',t)],t,{},100)
    monkeypatch.setattr(ae,'_radar_render_revision',lambda variant: 'abcdef123456')
    second,_=emitter._radar_mosaic_inputs([('KNEA',t)],t,{},100)
    assert first['mosaicKey']!=second['mosaicKey']


def test_one_site_matches_existing_sampler_and_colour_exactly():
    rng=np.random.default_rng(27)
    source=scan()
    source.codes[:]=rng.integers(0,256,source.codes.shape,dtype=np.uint8)
    palette=source_palette(SOURCE)
    for z in (7,8,9,10):
        x,y=map(int,tile_of(47.61,-122.33,z))
        old,_=l3.render_tile(source,z,x,y,palette)
        new,_=mosaic.render_mosaic([source],z,x,y,palette)
        assert np.array_equal(np.asarray(old),np.asarray(new))
        old.close();new.close()


def test_late_hca_schedules_short_readiness_retry(make_emitter, hybrid, multisite, classified, monkeypatch):
    hybrid.now=hybrid.latest+60
    classified.missing.add('KNEA')
    emitter=make_emitter(); retries=[]
    monkeypatch.setattr(emitter,'_schedule_retry',lambda key, callback, timeout, **kw: retries.append((key,timeout)))
    emitter._do_radar()
    assert emitter._radar_result.frames[-1]['unfilteredSites']
    assert ('radar',20) in retries


def test_migration_removes_prior_native_revision_only(make_emitter, hybrid, multisite, classified):
    emitter=make_emitter(); root=Path(ae.RADAR_DIR)
    old=root/'t'/'123456789abc'/SOURCE/'KNEA'/'202609250000'/'8'/'40'
    old.mkdir(parents=True); (old/'80.png').write_bytes(b'old renderer')
    emitter._do_radar()
    assert not (root/'t'/'123456789abc').exists()
    assert (root/'.native-revision').read_text()==ae._radar_render_revision('native')
    assert any(k[1].startswith('M') for k in emitter._radar_disk_inventory.records if k[0]==SOURCE)


@pytest.mark.parametrize('invalid', [False, True])
def test_n0h_body_bytes_share_durable_ledger(make_emitter, monkeypatch, invalid):
    emitter=make_emitter(); emitter._radar_begin_log_pass(); emitter._radar_session=ae.RadarSession()
    raw=b'bad HCA'*100 if invalid else n0h_product()
    monkeypatch.setattr(emitter._radar_session,'open',lambda *args, **kw: io.BytesIO(raw))
    def acquire():
        return emitter._radar_request(ae.RADAR_N0H_TRANSPORT,ae.RADAR_LEVEL3_BUCKET+'NEA_N0H_object',
            ae.time.monotonic()+10,validate=l3.decode_n0h,health=emitter._radar_n0h_health)
    if invalid:
        with pytest.raises(ValueError): acquire()
    else: acquire()
    assert emitter._radar_native_budget.snapshot()['bytesToday']==len(raw)
    assert emitter._radar_native_budget.flush()
    assert make_emitter()._radar_native_budget.snapshot()['bytesToday']==len(raw)


@pytest.mark.parametrize('tier', ['rest','dormant'])
def test_n0h_inherits_unattended_tier_gating(make_emitter, hybrid, multisite, classified, native, tier):
    emitter=make_emitter(); emitter._radar_attention.forced=tier; emitter._radar_attention.tier=tier
    emitter._do_radar()
    assert not classified.calls and not native.calls


def test_n0h_inherits_newest_only_and_pause(make_emitter, hybrid, multisite, classified, native):
    from lib.radar_native_budget import NATIVE_NEWEST_ONLY_BYTES, NATIVE_PAUSE_BYTES
    emitter=make_emitter(); emitter._radar_native_budget.add(NATIVE_NEWEST_ONLY_BYTES+1)
    emitter._do_radar()
    frame=emitter._radar_result.frames[-1]
    assert len(emitter._radar_result.frames)==1 and frame['complete'] and not frame['unfilteredSites']
    assert classified.calls
    newest={p['volumeTs'] for p in frame['siteScans']}
    assert all(l3.s3_key_time(key,'N0H') in newest for kind,key in classified.calls if kind=='get')
    classified.calls.clear();native.calls.clear()
    emitter._radar_native_budget.add(NATIVE_PAUSE_BYTES)
    emitter._do_radar()
    assert not classified.calls and not native.calls and emitter._radar_result.tiles['variant'] is False


def test_late_neighbour_scan_changes_key_without_rewriting_old_tiles(make_emitter, hybrid, multisite, classified):
    multisite.scans['KMID']=[hybrid.latest-60]
    emitter=make_emitter();emitter._do_radar();frame=emitter._radar_result.frames[-1]
    old=frame['mosaicKey']
    records={k:v[0].read_bytes() for k,v in emitter._radar_disk_inventory.records.items() if k[1]==old}
    multisite.scans['KMID'].append(hybrid.latest)
    hybrid.mono+=61
    emitter._do_radar(discovery=True,intent_triggered=False)
    frame=emitter._radar_result.frames[-1]
    assert frame['mosaicKey']!=old and any(p['id']=='KMID' and p['ts']==hybrid.latest for p in frame['siteScans'])
    assert all(emitter._radar_disk_inventory.records[k][0].read_bytes()==raw for k,raw in records.items())
