"""Ordered intent, mandatory acquisition and bounded resource regressions (offline)."""
import errno
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import pytest

from lib import almanac_emit as ae, radar_http as http
from lib.radar_fetch import HostHealth
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_freshness_health import serve_at, _get  # noqa: F401


def transaction(module, session, gen, source='site', zoom=8, claim=None, commit=True, policy='manual'):
    params = dict(radarSession=[session], radarGeneration=[str(gen)], radarSource=[source],
                  radarHeartbeat=[str(gen)], radarPolicy=[policy], radarCommit=['1'] if commit else ['0'])
    if claim is not None:
        params['radarClaim'] = [claim]
        params['radarClaimEpoch'] = [str(module._camera_owner(module._read_radar_intent())['epoch'])]
    activity = dict(at=time.time(), theme='paper', moving=False, zoom=zoom, center=dict(lat=47.61, lon=-122.33))
    with module._count_lock:
        return module._camera_transaction(activity, params)


def test_ordered_requests_user_claim_and_auto(serve_at, tmp_path):
    module, _ = serve_at({})
    a, b = 'session-a-12345678', 'session-b-12345678'
    assert transaction(module,a,1,claim='')
    assert transaction(module,a,2,source='site',zoom=8,policy='auto')
    before = module._read_radar_intent()
    assert not transaction(module,a,1,source='mosaic',zoom=7)
    assert not transaction(module,a,1,commit=False)
    assert module._read_radar_intent() == before
    assert not transaction(module,b,0,claim='')
    assert not transaction(module,b,0,claim=a)
    assert module._read_radar_intent() == before  # reload reconciliation is read-only
    assert transaction(module,b,1,source='site',zoom=8,policy='auto',claim=a)
    assert not transaction(module,a,3)
    module._camera_persist_timer.join(2)
    assert (tmp_path/'radar_zoom').read_text().strip() == 'auto'


def test_motion_heartbeat_cannot_reorder_same_generation_activity(serve_at, tmp_path):
    module, url = serve_at({})
    session='heartbeat-owner-12345'
    assert transaction(module,session,1,claim='')
    base=url+'/wx.json?view=radar&radarSession='+session+'&viewSession='+session+'&radarGeneration=1&radarTheme=paper&radarGeoZoom=8&radarGeoCenter=47.61,-122.33'
    _get(base+'&viewSeq=3&radarHeartbeat=3&radarMoving=0')
    before=(tmp_path/'radar_activity').read_text()
    _get(base+'&viewSeq=2&radarHeartbeat=2&radarMoving=1')
    assert (tmp_path/'radar_activity').read_text()==before
    _get(base+'&viewSeq=4&radarHeartbeat=4&radarMoving=1')
    assert json.loads((tmp_path/'radar_activity').read_text())['moving']
    assert module._read_radar_intent()['generation']==1


@pytest.mark.parametrize('error,kind', [
    (socket.gaierror(socket.EAI_AGAIN,'DNS unavailable'),'local'),
    (OSError(errno.ENETUNREACH,'route'),'local'),
    (OSError(errno.EMFILE,'descriptors'),'local'),
    (http.AmbiguousTransportError('silent reused socket'),'ambiguous'),
    (TimeoutError('fresh HTTP response timeout'),'host'),
    (ValueError('bad service payload'),'host')])
def test_health_failure_classes(error, kind):
    health=HostHealth()
    for _ in range(6): health.record('iem','https://provider.invalid/tile',False,error)
    snap=health.snapshot()
    assert http.failure_class(error)==kind
    assert snap['breaker']==('open' if kind=='host' else 'closed')
    assert snap['localFailures']==(6 if kind=='local' else 0)
    assert snap['ambiguousFailures']==(6 if kind=='ambiguous' else 0)


def test_resolver_ownership_survives_pool_replacement(monkeypatch):
    entered, release=threading.Event(),threading.Event();calls=[]
    def lookup(*args):
        calls.append(args);entered.set();release.wait(5)
        return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]
    monkeypatch.setattr(http.socket,'getaddrinfo',lookup)
    sessions=[]
    try:
        for _ in range(12):
            session=http.RadarSession();sessions.append(session)
            with pytest.raises(http.LocalTransportError):session._addresses_for(('stalled.invalid',443),time.monotonic()+.01)
            session.close()
        assert entered.is_set() and len(calls)==1
        assert len(http._dns_jobs)<=4
    finally:
        release.set()
        for session in sessions:session.close()


def test_unchanged_discovery_repairs_incomplete_loop(make_emitter, hybrid, tmp_path):
    hybrid.view();e=make_emitter();e._do_radar()
    previous=e._radar_result
    e._radar_result=previous._replace(frames=tuple(dict(f,complete=i==len(previous.frames)-1) for i,f in enumerate(previous.frames)))
    ctx=dict(discovery=True,viewed=True,preference_stamp=e._radar_result_stamp)
    e._radar_pending=dict(four=True,eight=True)
    e._radar_discovery_unchanged(previous.source_id,previous.ts_frame,ctx)
    # The real discovery pass rebuilds flags from disk and retains all history.
    e._do_radar(discovery=True,intent_triggered=False)
    assert sum(f['complete'] for f in e._radar_result.frames)>=8


def test_visible_four_eight_margin_optional_deep_request_sequence(make_emitter, hybrid, monkeypatch):
    monkeypatch.setattr(ae,'_NEXRAD_SITES',{})
    hybrid.view();e=make_emitter();e._do_radar()
    calls=[url for _,method,url,*_ in hybrid.calls if method=='GET' and 'mrms::lcref-' in url]
    import re
    parsed=[re.search(r'lcref-(\d+)/(\d+)/(\d+)/(\d+)',url).groups() for url in calls]
    z=str(e._radar_result.zoom)
    visible=ae._radar_viewport(47.61,-122.33,int(z),956,490)[0]
    n=len(visible)
    stamps=[ae.datetime.fromtimestamp(hybrid.latest-120*i,ae.timezone.utc).strftime('%Y%m%d%H%M') for i in range(8)]
    assert [r[0] for r in parsed[:n*8]]==[stamp for stamp in stamps for _ in range(n)]
    assert all(r[1]==z for r in parsed[:n*8])
    assert parsed[n*8][0]==stamps[0]  # only now may newest margin start
    assert sum(f['complete'] for f in e._radar_result.frames)>=8


def test_missing_margin_never_gates_visible_loop(make_emitter, hybrid):
    hybrid.view();e=make_emitter()
    def fail(req,_):
        if '/mrms::' in req.full_url and req.full_url.endswith('/85.png'):
            raise TimeoutError('off-screen tile missing')
    hybrid.failure=fail;e._do_radar()
    assert sum(f['complete'] for f in e._radar_result.frames)>=8


def test_panned_timestamp_regression_preserves_manifest(make_emitter, hybrid, tmp_path):
    (tmp_path/'radar_center').write_text('47.8,-123.0');hybrid.view()
    e=make_emitter();e._do_radar();old=e._radar_result
    hybrid.latest-=120;e._do_radar(intent_triggered=False)
    assert e._radar_result.ts_frame==old.ts_frame
    assert {f['ts'] for f in old.frames}<={f['ts'] for f in e._radar_result.frames}


def test_capped_source_uses_camera_footprint(make_emitter, hybrid, tmp_path):
    (tmp_path/'radar_zoom').write_text('10')
    from tests.fixtures.config import make_config
    e=make_emitter(config=make_config(Station={'Latitude':'52.52','Longitude':'13.4'}));e._do_radar()
    r=e._build_payload()['radar']
    assert r['tiles']['z']==7 and r['camera']['zoom']==10
    grid=r['tiles']['grid'];assert grid['w']<=2 and grid['h']<=2
    assert r['tiles']['frames'][-1]['levels']['7']


def test_failed_fallback_escapes_dwell(make_emitter, hybrid):
    def fail_primary(req,_):
        if 'iastate.edu' in req.full_url:raise ValueError('primary service down')
    hybrid.failure=fail_primary;e=make_emitter()
    for _ in range(3):e._do_radar(intent_triggered=False)
    assert e._radar_result.source_id=='rainviewer'
    hybrid.mono+=31
    def fail_fallback(req,_):
        if 'iastate.edu' not in req.full_url:raise ValueError('fallback service down')
    hybrid.failure=fail_fallback
    for _ in range(5):e._do_radar(intent_triggered=False)
    assert hybrid.mono<300
    assert e._radar_result.source_id=='iem-mrms-lcref'


def test_busy_worker_keeps_retry_work(make_emitter):
    e=make_emitter();e._inflight.add('radar');e._check_radar()
    assert e._radar_acquisition_pending
    e._inflight.clear();calls=[];e._spawn=lambda name,fn:calls.append(name)
    e._check_radar();assert calls==['radar'] and not e._radar_acquisition_pending


def test_concurrent_worker_admission_keeps_one_flight(make_emitter):
    e=make_emitter(_running=True);entered=threading.Event();release=threading.Event();calls=[]
    def work():
        calls.append(1);entered.set();release.wait(5)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _:e._spawn('radar',work),range(32)))
        assert entered.wait(1) and calls==[1]
        assert 'radar' in e._inflight
        e._check_radar()
        assert e._radar_acquisition_pending
    finally:release.set()


@pytest.mark.parametrize('count',[2,4])
def test_multisite_cold_switch_with_partly_used_budget(make_emitter, hybrid, monkeypatch, tmp_path, count):
    # This local transport models IEM ridge layers, not Level III products.
    monkeypatch.setattr(ae.AlmanacEmitter, '_radar_level3_down', lambda self: True)
    # Distinct sites at the same footprint make every required layer explicit.
    sites={f'KAA{i}':(47.61,-122.33,f'Site {i}') for i in range(count)}
    monkeypatch.setattr(ae,'_NEXRAD_SITES',sites)
    from tests.test_radar_hybrid import png
    import io
    original=ae.RadarSession.open
    def open_(session,req,timeout):
        if req.full_url.startswith(ae.RADAR_SITE_LIST_URL):
            raw=json.dumps(dict(scans=[dict(ts=ae.datetime.fromtimestamp(hybrid.latest-300*i,ae.timezone.utc).isoformat()) for i in range(8)])).encode()
        elif 'ridge::' in req.full_url:
            raw=png((12,145,16,255))
        else:return original(session,req,timeout)
        hybrid.calls.append(('iem',req.get_method(),req.full_url,hybrid.mono,timeout))
        response=io.BytesIO(raw);response.status=200;response.headers={};return response
    monkeypatch.setattr(ae.RadarSession,'open',open_)
    hybrid.view();(tmp_path/'radar_source').write_text('site')
    e=make_emitter();e._radar_request_times=[hybrid.mono]*30;e._do_radar()
    assert e._radar_result.source_mode=='site'
    assert sum(f['complete'] for f in e._radar_result.frames)>=4
    assert len(e._radar_request_times)<=240
    assert all(len(f['siteScans'])==count for f in e._radar_result.frames if f['complete'])


def test_deadline_failure_qualifies_fallback_but_local_capacity_does_not(make_emitter):
    e=make_emitter();e._radar_budget_retry=lambda *a,**kw:None;e._radar_retained_refresh=lambda *a:None
    # 70% request success cannot hide failure to deliver the acquisition objective.
    for _ in range(7):e._radar_health.record('iem','https://provider.invalid/a',True)
    for _ in range(3):e._radar_health.record('iem','https://provider.invalid/a',False,TimeoutError())
    assert e._radar_health.snapshot()['breaker']=='closed'
    assert [e._radar_failed_pass('iem',TimeoutError('objective stalled'),{}) for _ in range(3)]==[False,False,True]
    assert not e._radar_failed_pass('iem',http.LocalTransportError('local route'),{})
    assert 'iem' not in e._radar_transport_failures


def test_metadata_and_archive_caches_are_bounded(make_emitter, hybrid):
    e=make_emitter();e._radar_session=ae.RadarSession()
    from tests.test_radar_hybrid import png
    import io
    e._radar_session.open=lambda *args,**kwargs:io.BytesIO(png())
    # Gate is orthogonal; verify storage bounds over many window URL identities.
    e._radar_request_gate=lambda *args:None
    for i in range(260):
        e._radar_request('iem',f'https://provider.invalid/window/{i}',100,metadata=True)
        e._radar_archive_probe('iem',f'https://provider.invalid/archive/{i}',100)
    assert len(e._radar_metadata)<=128 and len(e._radar_metadata_at)<=128
    assert len(e._radar_archive_positive)<=128


def test_publication_inventory_avoids_repeated_filesystem_stats(make_emitter, hybrid, monkeypatch):
    hybrid.view();e=make_emitter();e._do_radar();source,ctx=e._radar_idle_context
    old=e._radar_result;expected=ae._radar_tile_manifest(source,old.frames,ctx)
    from pathlib import Path
    def forbidden(*_):raise AssertionError('publication restatted immutable inventory')
    monkeypatch.setattr(Path,'is_file',forbidden)
    actual=ae._radar_tile_manifest(source,old.frames,ctx)
    assert actual['frames']==expected['frames'] and actual['newest']==expected['newest']
    assert len(e._radar_disk_inventory)<=8000


def test_site_zoom_ten_optional_targets_respect_mrms_limit(make_emitter, hybrid, monkeypatch, tmp_path):
    hybrid.view();e=make_emitter();e._do_radar();source,ctx=e._radar_idle_context
    ctx=dict(ctx,zoom=10,camera_zoom=10,refresh=dict(state='idle'),sites=[],site_scans={})
    calls=[]
    e._radar_tile_batch=lambda source,stamp,ctx,*args:(calls.append((source,ctx['zoom'])) or iter(()))
    e._radar_prefetch('iem-nexrad-n0b',ctx)
    assert all(source!='iem-mrms-lcref' or zoom<=9 for source,zoom in calls)


def test_sustained_camera_scan_activity_has_bounded_caches(make_emitter, hybrid, monkeypatch, tmp_path):
    monkeypatch.setattr(ae,'_NEXRAD_SITES',{})
    e=make_emitter();hybrid.view()
    for i in range(12):
        hybrid.latest+=120;hybrid.mono+=120;hybrid.view()
        (tmp_path/'radar_zoom').write_text(str(7+i%2))
        (tmp_path/'radar_center').write_text(f'{47.61+i%3*.02},-122.33')
        e._do_radar()
        assert sum(f['complete'] for f in e._radar_result.frames)>=8
        assert len(e._radar_tiles)<=400 and len(e._radar_disk_inventory)<=8000
        assert e._radar_disk_bytes<=64_000_000 and len(e._radar_metadata)<=128
        assert len(e._radar_negative)<=512
