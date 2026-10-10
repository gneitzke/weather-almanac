"""Radar engine v3: spherical coverage, multi-site scans and intent generations."""
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image, ImageChops

from lib import almanac_emit as ae
from lib import radar_engine
from lib.radar_geometry import circle_intersects_bounds, distance_meters, EARTH_RADIUS_METERS
from tests.fixtures.config import make_config
from tests.test_radar_hybrid import hybrid, png  # noqa: F401
from tests.test_emitter_lifecycle import FakeClock, InlineThread


def test_circle_intersection_edges_corners_and_wrap():
    import math
    bounds = dict(w=-1, e=1, s=-1, n=1)
    # 231 km beyond the top edge is out; 229 km is in.
    deg = math.degrees(1000 / EARTH_RADIUS_METERS)
    assert not circle_intersects_bounds(1+231*deg, 0, 230000, bounds)
    assert circle_intersects_bounds(1+229*deg, 0, 230000, bounds)
    # Diagonally beyond both edges: include a circle grazing the corner, but
    # exclude one whose bounding box alone overlaps (not a circle intersection).
    corner_distance = distance_meters(2, 2, 1, 1)
    assert circle_intersects_bounds(2, 2, corner_distance+1, bounds)
    assert not circle_intersects_bounds(2, 2, corner_distance-1, bounds)
    wrapped = dict(w=179, e=-179, s=-1, n=1)
    assert circle_intersects_bounds(0, -179.5, 1, wrapped)
    assert not circle_intersects_bounds(0, 0, 230000, wrapped)
    assert circle_intersects_bounds(82, 10, 230000, dict(w=-2, e=2, s=80, n=84))


def test_cap_is_viewport_distance_not_station(monkeypatch):
    sites = {f'K{i:03}': (47, -123+i*.1, str(i)) for i in range(9)}
    monkeypatch.setattr(radar_engine, '_NEXRAD_SITES', sites)
    bounds = radar_engine._radar_viewport(47, -122, 4, 956, 490)[2]
    selected, considered = radar_engine._radar_sites((47, -123), bounds)
    assert considered == 9 and len(selected) == radar_engine.RADAR_SITE_MAX_COUNT == 4
    assert [s['id'] for s in selected] == [f'K{i:03}' for i in range(5,9)]
    assert [s['viewportDistanceMeters'] for s in selected] == sorted((s['viewportDistanceMeters'] for s in selected), reverse=True)


@pytest.fixture
def multisite(hybrid, monkeypatch, tmp_path):
    # IEM layer/topology tests use this fallback; S3 tests opt into native.
    monkeypatch.setattr(radar_engine.RadarEngine, '_level3_down', lambda self: True)
    monkeypatch.setattr(radar_engine, '_NEXRAD_SITES', {
        'KNEA': (47.61, -122.33, 'nearest'),
        'KMID': (47.8, -122.33, 'middle'),
        'KFAR': (48, -122.33, 'far')})
    state = SimpleNamespace(scans={
        'KNEA': [hybrid.latest-600, hybrid.latest],
        'KMID': [hybrid.latest-720, hybrid.latest-60, hybrid.latest+60],
        'KFAR': []}, calls=[], colors={'KNEA': (82, 214, 162, 128), 'KMID': (12, 145, 16, 255)}, failure=None)
    original = radar_engine.RadarSession.open
    def fetch(self, req, timeout):
        url = req.full_url
        if 'operation=list' in url:
            site = 'K'+parse_qs(urlsplit(url).query)['radar'][0]
            state.calls.append(('list', site))
            return io.BytesIO(json.dumps(dict(scans=[dict(ts=datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%dT%H:%MZ'))
                for t in state.scans[site]])).encode())
        if 'ridge::' in url:
            site = 'K'+url.split('ridge::')[1][:3]
            state.calls.append(('tile', site, url))
            if state.failure:
                state.failure(site, url)
            return io.BytesIO(png(state.colors[site]))
        return original(self, req, timeout)
    monkeypatch.setattr(radar_engine.RadarSession, 'open', fetch)
    hybrid.pin('site')
    return state


def test_multisite_alignment_stacking_dark_and_identity(make_emitter, hybrid, multisite):
    hybrid.view()
    emitter = make_emitter(); emitter.radar._acquire()
    r = emitter._build_payload()['radar']
    assert r['available'] and r['siteId'] == 'KNEA'
    assert r['sitesConsidered'] == r['sitesDrawn'] == 3
    assert [s['id'] for s in r['sites']] == ['KFAR', 'KMID', 'KNEA']
    assert not r['sites'][0]['contributing'] and r['sites'][0]['reason']=='not reporting'
    assert r['sites'][-1]['primary']
    assert sorted(c for c in multisite.calls if c[0]=='list') == sorted([('list', 'KNEA'), ('list', 'KMID'), ('list', 'KFAR')])
    assert [f['ts'] for f in r['tiles']['frames']] == multisite.scans['KNEA']
    assert [f['siteScans'] for f in r['tiles']['frames']] == [
        [dict(id='KMID', ts=hybrid.latest-720), dict(id='KNEA', ts=hybrid.latest-600)],
        [dict(id='KMID', ts=hybrid.latest-60), dict(id='KNEA', ts=hybrid.latest)]]
    for site,color in multisite.colors.items():
        paths=list(Path(radar_engine.RADAR_DIR).glob('t/*/iem-nexrad-n0b/'+site+'/*/8/*/*.png'));assert paths
        expected=radar_engine.remap(Image.new('RGBA',(256,256),color),'iem-nexrad-n0b',radar_engine.source_palette('iem-nexrad-n0b')).tobytes()
        for path in paths:
            with Image.open(path) as image:assert image.convert('RGBA').tobytes()==expected
    scans=r['tiles']['frames'][-1]['siteScans'];multisite.calls.clear();emitter.radar._acquire()
    assert all(c[0]=='list' for c in multisite.calls)
    multisite.scans['KMID'].insert(-1,hybrid.latest);emitter.radar._acquire()
    assert emitter._build_payload()['radar']['tiles']['frames'][-1]['siteScans']!=scans
    assert emitter.radar._ts_frame==r['observedTs']


def test_dark_nearest_promotes_reporting_primary(make_emitter, hybrid, multisite):
    multisite.scans['KNEA'] = [hybrid.now-900]
    emitter = make_emitter(); emitter.radar._acquire()
    r = emitter._build_payload()['radar']
    assert r['available'] and r['sourceMode']=='site' and r['siteId']=='KMID'
    assert r['sources'][1]['siteId']=='KMID' and r['sources'][1]['available']
    assert r['sites'][-1]['reporting'] is False and r['sites'][-1]['ageSec']==900
    assert not any(c[:2]==('tile', 'KNEA') for c in multisite.calls)


def test_slot_omits_stale_and_future_and_dark_scans():
    ctx = dict(sites=[dict(id='old', reporting=True), dict(id='future', reporting=True),
                     dict(id='good', reporting=True), dict(id='dark', reporting=False)],
               site_scans={'old': (0,), 'future': (1001,), 'good': (101, 998, 1001), 'dark': (999,)})
    assert radar_engine._radar_site_pairs(ctx, 1000, now=1000) == (('good', 998),)  # relative rules, seen at the anchor


@pytest.mark.parametrize('zoom', [7, 8])
def test_cap_limits_scan_requests_in_real_adapter(make_emitter, hybrid, multisite, monkeypatch, tmp_path, zoom):
    sites = {f'K{i:03}': (47.61+i*.01, -122.33, str(i)) for i in range(9)}
    monkeypatch.setattr(radar_engine, '_NEXRAD_SITES', sites)
    multisite.scans = {site: [hybrid.latest] for site in sites}
    multisite.colors = {site: (20, 80, 120, 100) for site in sites}
    (tmp_path/'radar_zoom').write_text(str(zoom))
    emitter = make_emitter(); emitter.radar._acquire()
    r = emitter._build_payload()['radar']
    assert r['sourceMode'] == 'site' and r['sitesConsidered'] == 9 and r['sitesDrawn'] == 4
    assert sorted(c[1] for c in multisite.calls if c[0] == 'list') == [f'K{i:03}' for i in range(4)]
    assert len(emitter.radar._request_times) <= radar_engine.RADAR_REQUESTS_PER_MIN
    latest = r['tiles']['frames'][-1]
    assert [s['id'] for s in latest['siteScans']] == [f'K{i:03}' for i in reversed(range(4))]


def test_neighbor_listing_error_does_not_fail_reporting_primary(make_emitter, hybrid, multisite, monkeypatch):
    emitter = make_emitter(); original = emitter.radar._request
    def fetch(source, url, *args, **kwargs):
        if 'radar=MID' in url:
            raise OSError('neighbor listing down')
        return original(source, url, *args, **kwargs)
    monkeypatch.setattr(emitter.radar, '_request', fetch)
    emitter.radar._acquire()
    assert emitter.radar._available and emitter.radar._result.site_id == 'KNEA'
    assert emitter.radar._result.sites[1]['reporting'] is None


def test_site_budget_aborts_without_negative_cache(make_emitter, hybrid, multisite, monkeypatch):
    monkeypatch.setattr(radar_engine, 'RADAR_REQUESTS_PER_MIN', 5)
    emitter = make_emitter(); emitter.radar._acquire()
    assert not emitter.radar._available
    assert len(emitter.radar._request_times) == 5
    assert not emitter.radar._negative
    assert emitter.radar._refresh['state'] == 'failed'
    # Successful in-flight tiles survive a budget yield in the immutable cache.
    assert all(p.read_bytes().startswith(b'\x89PNG') for p in Path(radar_engine.RADAR_DIR).rglob('*.png'))






@pytest.mark.parametrize('preference,value', [('radar_zoom','9'), ('radar_center','47.8,-122.3'), ('radar_intent','41')])
def test_supersede_tile_boundary_immediate_new_pass(make_emitter, hybrid, monkeypatch, tmp_path, preference, value):
    emitter = make_emitter(); emitter.radar._acquire(); old = emitter.radar._result
    hybrid.latest += 120; hybrid.now += 120
    clock = FakeClock()
    monkeypatch.setattr(ae, 'Clock', clock)
    monkeypatch.setattr(ae, 'threading', SimpleNamespace(Thread=InlineThread))
    emitter._runtime.running = True
    emitter.radar._schedule_retry('radar', emitter.radar._check, 120)
    # Tile requests run concurrently.  Do not let the result depend on which
    # worker gets scheduled first: hold every new-frame response until the
    # intent marker is durable, so the next tile-boundary checkpoint must
    # supersede this pass before it can publish a partial newest frame.
    import threading
    entered, release = threading.Event(), threading.Event()
    seen = []
    def slow(req, timeout):
        if 'mrms::' in req.full_url:
            if not seen:
                seen.append(emitter.radar._refresh)
                hybrid.mono += .25
                entered.set()
            assert release.wait(10)
    hybrid.failure = slow
    def supersede():
        assert entered.wait(10)
        (tmp_path/preference).write_text(value)
        release.set()
    writer = threading.Thread(target=supersede)
    writer.start()
    closed = []
    monkeypatch.setattr(radar_engine.RadarSession, 'close', lambda self: closed.append(self))
    try:
        emitter.radar._check()
    finally:
        release.set(); writer.join(10)
    assert not writer.is_alive()
    # Discovery already slid the old geometry before the superseding intent
    # arrived at a tile boundary. Keep that published hour until the next pass.
    pending = emitter.radar._result
    assert pending.frames[:-1] == old.frames[1:]
    assert not pending.frames[-1]['complete'] and pending.ts_frame == old.ts_frame
    assert pending.zoom == old.zoom and pending.bounds == old.bounds
    assert emitter.radar._restart
    assert emitter.radar._restart and not emitter.radar._negative
    assert not list(Path(radar_engine.RADAR_DIR).rglob('*.tmp.*'))
    assert not closed and not emitter._runtime.inflight
    assert 'radar' not in emitter._runtime.retries
    assert any(e.timeout==0 for e in clock.events)
    intents = []
    original = emitter.radar._publish_refresh
    def record(ctx, **kw):
        original(ctx, **kw)
        intents.append(ctx['intent'])
    monkeypatch.setattr(emitter.radar, '_publish_refresh', record)
    clock.advance(ae.EMIT_INTERVAL)
    assert not emitter.radar._restart and intents
    # Provider fallback may drop IEM, but normal same-provider passes keep it.
    assert not closed
    expect = {'radar_zoom': ('zoom',9),
              'radar_center': ('center',dict(lat=47.8, lon=-122.3)), 'radar_intent': ('seq',41)}[preference]
    assert all(intent[expect[0]]==expect[1] for intent in intents)
    assert emitter.radar._refresh['state']=='idle'
    emitter.stop()




def test_supersede_between_history_frames_keeps_published_newest(make_emitter, hybrid, monkeypatch, tmp_path):
    hybrid.view(); emitter=make_emitter()
    original=radar_engine.RadarEngine.__setattr__
    published=[]
    def record(self, key, value):
        original(self, key, value)
        if key=='_result' and value.available and any(f['complete'] for f in value.frames):
            published.append(value)
            (tmp_path/'radar_center').write_text('47.8,-122.3')
    monkeypatch.setattr(radar_engine.RadarEngine, '__setattr__', record)
    emitter.radar._acquire()
    assert len(published)==1 and emitter.radar._result is published[0]
    assert sum(f['complete'] for f in emitter.radar._frames)==1
    assert not emitter.radar._negative and emitter.radar._restart
    assert emitter.radar._restart








@pytest.mark.parametrize('count,cap',[(1,31),(2,8)])
def test_site_history_cap(make_emitter,hybrid,multisite,monkeypatch,count,cap):
    monkeypatch.setattr(radar_engine,'RADAR_REQUESTS_PER_MIN',10000)
    monkeypatch.setattr(radar_engine,'RADAR_MAX_FRAME_BUILDS_PER_PASS',100)
    stamps=list(range(hybrid.latest-3600,hybrid.latest+1,120))
    multisite.scans={'KNEA':stamps,'KMID':stamps if count==2 else [],'KFAR':[]}
    hybrid.view(); emitter=make_emitter();emitter.radar._acquire()
    assert len(emitter.radar._frames)==cap


def test_site_tile_clipping_uses_circle_not_whole_viewport(monkeypatch):
    monkeypatch.setattr(radar_engine,'_NEXRAD_SITES',{'KONE':(47,-124,'test')})
    tiles=radar_engine._radar_viewport(47,-122,7,956,490)[0]
    selected=radar_engine._radar_site_tiles(dict(zoom=7,tiles=tiles),'KONE')
    assert 0<len(selected)<len(tiles)
    for tile in tiles:
        x,y=tile[:2]; n,w=radar_engine.world_inverse(x*256,y*256,7);south,e=radar_engine.world_inverse((x+1)*256,(y+1)*256,7)
        assert (tile in selected)==circle_intersects_bounds(47,-124,230000,dict(n=n,s=south,w=w,e=e))


def test_runtime_sequence_validation(monkeypatch,tmp_path):
    from tests.test_freshness_health import _load_serve
    serve_at=_load_serve(monkeypatch,tmp_path,{})
    marker=tmp_path/'radar_intent'
    def write_seq(value):
        serve_at._write_radar_intent(dict(radarSeq=[value],radarZoom=['7'],radarCenter=['station']))
    for value in ('1','41','999999999999'):
        write_seq(value)
        assert json.loads(marker.read_text())['seq']==int(value)
    before=marker.read_bytes()
    for value in ('-1','1.0','1e2','1234567890123','١',''):
        write_seq(value)
        assert marker.read_bytes()==before
    durable=tmp_path/'durable-seq';durable.write_text('8')
    marker.unlink();marker.symlink_to(durable)
    write_seq('42')
    assert not marker.is_symlink() and durable.read_text()=='8'






@pytest.mark.parametrize('address',['127.0.0.1','::1','::ffff:127.0.0.1','198.51.100.2'])
def test_loopback_sequence_intent_and_duplicate(monkeypatch,tmp_path,address):
    from tests.test_freshness_health import _load_serve
    module=_load_serve(monkeypatch,tmp_path,{})
    monkeypatch.setattr(module.http.server.SimpleHTTPRequestHandler,'do_GET',lambda h:None)
    handler=object.__new__(module.Handler);handler.client_address=(address,1)
    handler.path='/wx.json?radarZoom=7&radarSource=site&radarCenter=station&radarSeq=41'
    handler.do_GET();module._flush_preferences();marker=tmp_path/'radar_intent'  # preference writer thread: wait for the durable write
    assert marker.exists()==(address in module.LOOPBACK)
    if marker.exists():
        # An old page's radarSource rides along and is ignored.
        assert json.loads(marker.read_text())==dict(seq=41,zoom=7,center='station')
        assert (tmp_path/'radar_zoom').read_text().strip()=='7'
        assert not (tmp_path/'radar_source').exists()
        handler.path='/wx.json?radarSeq=42&radarSeq=43';handler.do_GET()
        assert json.loads(marker.read_text())==dict(seq=41,zoom=7,center='station')


def test_all_dark_sites_keep_contract_on_mosaic(make_emitter,hybrid,multisite):
    multisite.scans={s:[] for s in multisite.scans}
    emitter=make_emitter();emitter.radar._acquire();r=emitter._build_payload()['radar']
    assert r['sourceMode']=='mosaic'
    assert all(not s['contributing'] and not s['primary'] and s['reason']=='not reporting' for s in r['sites'])


def test_repeated_layer_transport_failure_opens_shared_host(make_emitter,hybrid,multisite,monkeypatch):
    monkeypatch.setattr(radar_engine,'RADAR_REQUESTS_PER_MIN',10000)
    stamps=list(range(hybrid.latest-3600,hybrid.latest+1,120))
    multisite.scans.update(KNEA=stamps,KMID=stamps)
    def fail(site,url):
        if site=='KMID':raise OSError('layer down')
    multisite.failure=fail;hybrid.view();emitter=make_emitter()
    for _ in range(5): emitter.radar._acquire(intent_triggered=False)
    assert emitter.radar._result.source_id == 'rainviewer'
    assert emitter.radar._health.snapshot()['breaker'] == 'open'
    assert any(k[1] == 'KNEA' for k in emitter.radar._tiles)  # paid-for good site tiles survive






def test_history_reserves_next_zoom_newest(make_emitter,hybrid,tmp_path):
    hybrid.view();emitter=make_emitter();emitter.radar._acquire()
    count=len(emitter.radar._request_times)
    assert count<=radar_engine.RADAR_REQUESTS_PER_MIN-radar_engine.RADAR_HISTORY_RESERVE
    before=emitter.radar._result
    (tmp_path/'radar_zoom').write_text('9');emitter.radar._acquire()
    assert emitter.radar._result.zoom==9 and emitter.radar._result is not before
    assert emitter.radar._refresh['state']=='idle'
    assert count<len(emitter.radar._request_times)<=radar_engine.RADAR_REQUESTS_PER_MIN


@pytest.mark.parametrize('bad',['size','json','counts','truncated','revision','pixels'])
def test_corrupt_warm_cache_rebuilt(make_emitter,hybrid,bad):
    from PIL.PngImagePlugin import PngInfo
    emitter=make_emitter();emitter.radar._acquire()
    path=next(Path(radar_engine.RADAR_DIR).glob('t/*/*/*/*/*/*/*.png'))
    with Image.open(path) as im:meta=json.loads(im.info['radarRemap']);image=im.copy()
    if bad=='size':image=Image.new('RGBA',(1,1))
    if bad=='pixels':image.putpixel((0,0),(19,37,53,255))
    if bad=='counts':meta['unmatchedPixels']=meta['opaquePixels']+1
    if bad=='revision':meta['revision']='old'
    info=PngInfo();info.add_text('radarRemap','broken' if bad=='json' else json.dumps(meta));image.save(path,pnginfo=info)
    if bad=='truncated':path.write_bytes(path.read_bytes()[:80])
    Path(emitter.output_path).with_name('radar_bad_tiles').write_text(json.dumps([path.relative_to(Path(emitter.output_path).parent).as_posix()]))
    hybrid.calls.clear();emitter.radar._acquire()
    radar_engine._radar_tile_metadata(path, 'iem-mrms-lcref')
    assert not any('mrms::' in c[2] for c in hybrid.calls)  # rebuild from native LRU
    with Image.open(path) as im:im.load();assert im.size==(256,256)
    assert emitter.radar._refresh['state']=='idle'


def test_primary_recovery_can_move_timestamp_back(make_emitter,hybrid,multisite):
    multisite.scans['KNEA']=[];emitter=make_emitter();emitter.radar._acquire()
    assert emitter.radar._result.site_id=='KMID'
    previous=emitter.radar._result
    multisite.scans['KNEA']=[previous.ts_frame-60]
    emitter.radar._acquire()
    assert emitter.radar._result.site_id=='KNEA' and emitter.radar._result.ts_frame==previous.ts_frame-60


def test_atomic_intent_worker_while_durable_writer_paused(make_emitter,hybrid,tmp_path,monkeypatch):
    import threading
    from tests.test_freshness_health import _load_serve
    module=_load_serve(monkeypatch,tmp_path,{})
    entered,release=threading.Event(),threading.Event()
    original=module._write_radar_zoom
    def pause(values):
        entered.set();assert release.wait(10);original(values)
    monkeypatch.setattr(module,'_write_radar_zoom',pause)
    params=dict(radarSeq=['42'],radarZoom=['9'],radarCenter=['47.8,-122.3'])
    worker=threading.Thread(target=module._write_radar_intent,args=(params,));worker.start()
    try:
        assert entered.wait(10)
        emitter=make_emitter();emitter.radar._acquire()
        assert emitter.radar._read_intent()==dict(seq=42,zoom=9,center=dict(lat=47.8,lon=-122.3))
        assert emitter.radar._result.zoom==9 and emitter.radar._result.center==dict(lat=47.61,lon=-122.33)
    finally:release.set();worker.join(10)
    assert not worker.is_alive()
    before=(tmp_path/'radar_intent').read_bytes()
    for seq in ('41','42'):
        module._write_radar_intent(dict(params,radarSeq=[seq],radarZoom=['5']))
        assert (tmp_path/'radar_intent').read_bytes()==before


def test_real_worker_supersede_at_network_barrier(make_emitter,hybrid,tmp_path):
    import threading
    emitter=make_emitter();emitter.radar._acquire();old=emitter.radar._result
    hybrid.latest+=120;hybrid.now+=120
    entered,release=threading.Event(),threading.Event()
    def fetch(req,timeout):
        if 'mrms::' in req.full_url:entered.set();assert release.wait(10)
    hybrid.failure=fetch
    worker=threading.Thread(target=emitter.radar._acquire);worker.start()
    try:
        assert entered.wait(10)
        (tmp_path/'radar_intent').write_text(json.dumps(dict(seq=42,zoom=9,center='station')))
        assert 'geometryOnly' not in emitter._build_payload()['radar']
        pending = emitter.radar._result
        assert pending.frames[:-1] == old.frames[1:]
        assert not pending.frames[-1]['complete'] and pending.ts_frame == old.ts_frame
        assert pending.zoom == old.zoom and pending.bounds == old.bounds
    finally:release.set();worker.join(10)
    assert not worker.is_alive() and emitter.radar._restart and emitter.radar._result is pending
    assert not emitter.radar._negative and not list(Path(radar_engine.RADAR_DIR).rglob('*.tmp.*'))




def test_budget_retry_exact_subsecond_headroom(make_emitter,hybrid,monkeypatch):
    emitter=make_emitter();emitter.radar._acquire()
    needed=len(radar_engine._radar_viewport(47.61,-122.33,8,956,490)[0])+2
    emitter.radar._request_times=[-59.5]*needed+[0]*(radar_engine.RADAR_REQUESTS_PER_MIN-needed)
    delays=[];monkeypatch.setattr(emitter.radar,'_schedule_retry',lambda key,cb,delay,**kw:delays.append(delay))
    emitter.radar._acquire();assert delays==[.5] and emitter.radar._refresh['state']=='idle'


def test_pan_cap_cannot_replace_station_timeline(make_emitter,hybrid,multisite,monkeypatch,tmp_path):
    sites={f'K{i:03}':(47.61+i*.01,-122.33,str(i)) for i in range(9)}
    monkeypatch.setattr(radar_engine,'_NEXRAD_SITES',sites)
    multisite.scans={site:[hybrid.latest] for site in sites}
    multisite.colors={site:(12,145,16,255) for site in sites}
    emitter=make_emitter();emitter.radar._acquire();assert emitter.radar._result.site_id=='K000'
    hybrid.mono+=60;multisite.calls.clear();(tmp_path/'radar_center').write_text('48,-122.33')
    emitter.radar._acquire();r=emitter._build_payload()['radar']
    assert r['sourceMode']=='site' and r['siteId']=='K000'
    assert 'K000' not in {s['id'] for s in r['sites']}
    assert {c[1] for c in multisite.calls if c[0]=='list'}=={'K005','K006','K007','K008'}
    # The four station-timeline listings remain valid across the pan.
    assert not any(c[:2]==('tile','K000') for c in multisite.calls)




def test_partial_site_keeps_independently_valid_tiles(make_emitter,hybrid,multisite):
    import urllib.error
    failed=[]
    def fail_one(site,url):
        if site=='KNEA' and not failed:
            failed.append(url);raise urllib.error.HTTPError(url,404,'one missing tile',{},None)
    multisite.failure=fail_one
    emitter=make_emitter();emitter.radar._acquire()
    assert failed and emitter.radar._result.source_id=='iem-nexrad-n0b'
    pairs=emitter.radar._result.tiles['frames'][-1]['siteScans']
    assert {p['id'] for p in pairs}=={'KNEA','KMID'}
    paths=list(Path(radar_engine.RADAR_DIR).glob('t/*/iem-nexrad-n0b/KNEA/*/8/*/*.png'))
    assert paths and all(p.read_bytes().startswith(b'\x89PNG') for p in paths)
