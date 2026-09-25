"""V2-only policy and labelled, bounded fallback; all transport is simulated."""
import socket
from pathlib import Path

import pytest

from lib import almanac_emit as ae, radar_native_budget as budget
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401
from tests.test_radar_level3 import native  # noqa: F401
from tests.test_radar_auto_page import controls
from tests.test_radar_remote_page import run_remote
from tests.test_freshness_health import _load_serve


@pytest.mark.parametrize('legacy', [b'v1', b'v2', b'garbage', b'\xff', None])
def test_legacy_renderer_file_is_ignored_even_when_unreadable(
        make_emitter, hybrid, multisite, native, tmp_path, legacy):
    path = tmp_path/'radar_render'
    if legacy is None:
        path.mkdir()
    else:
        path.write_bytes(legacy)
    emitter = make_emitter()
    assert 'radar_render' not in emitter._radar_stamp_names()
    emitter._do_radar()
    radar = emitter._build_payload()['radar']
    assert radar['native'] and 'renderPref' not in radar
    assert radar['nativeFallback'] == dict(active=False, reason=None, recovering=False)
    assert not hasattr(budget, 'default_renderer') and not hasattr(budget, 'render_preference')


def test_server_ignores_old_query_and_sends_no_renderer_header(monkeypatch, tmp_path):
    server = _load_serve(monkeypatch, tmp_path, {})
    monkeypatch.setattr(server.http.server.SimpleHTTPRequestHandler, 'do_GET', lambda h: None)
    monkeypatch.setattr(server.http.server.SimpleHTTPRequestHandler, 'end_headers', lambda h: None)
    h = object.__new__(server.Handler)
    h.client_address = ('127.0.0.1', 1)
    h.path = '/wx.json?radarSession=old-render-session&radarRender=v1'
    headers = {}
    h.send_header = lambda k, v: headers.update({k: v})
    h.do_GET(); h.end_headers()
    assert not (tmp_path/'radar_render').exists()
    assert 'X-Radar-Render' not in headers
    assert 'radar_render' not in Path('design/almanac/kiosk/almanac-kiosk.sh').read_text()


@pytest.mark.parametrize('ceiling', [0, budget.NATIVE_NEWEST_ONLY_BYTES+1])
@pytest.mark.parametrize('viewed', [False, True])
def test_watch_fetches_only_primary_newest_and_counts_bytes(
        make_emitter, hybrid, multisite, native, monkeypatch, tmp_path, ceiling, viewed):
    monkeypatch.setattr(ae, 'RADAR_ATTENTION_MODE', 'active')
    if not viewed:
        (tmp_path/'radar_viewed').unlink()
        (tmp_path/'radar_viewing').unlink()
    emitter = make_emitter()
    emitter._radar_attention.forced = emitter._radar_attention.tier = 'watch'
    emitter._radar_native_budget.add(ceiling)
    emitter._do_radar()
    r = emitter._build_payload()['radar']
    assert r['native'] and r['frameCount'] == 1
    assert {p['id'] for p in emitter._radar_result.frames[0]['siteScans']} == {'KNEA'}
    assert multisite.calls == [('list', 'KNEA')]
    assert native.calls and all(key.startswith('NEA_') for _, key in native.calls)
    assert any('N0H' in key for _, key in native.calls)
    from lib.radar_level3 import s3_key_time
    assert all(s3_key_time(key) == hybrid.latest+24 for kind, key in native.calls if kind == 'get')
    assert r['nativeBudget']['bytesToday'] > ceiling
    native.calls.clear()
    emitter._radar_attention.forced = emitter._radar_attention.tier = 'watch'
    emitter._do_radar(discovery=True, intent_triggered=False)
    assert not [c for c in native.calls if c[0] == 'get']


@pytest.mark.parametrize('tier', ['live', 'warm', 'watch'])
def test_unviewed_builds_only_primary_newest(
        make_emitter, hybrid, multisite, native, monkeypatch, tmp_path, tier):
    monkeypatch.setattr(ae, 'RADAR_ATTENTION_MODE', 'active')
    (tmp_path/'radar_viewed').unlink(); (tmp_path/'radar_viewing').unlink()
    emitter = make_emitter()
    emitter._radar_attention.forced = emitter._radar_attention.tier = tier
    emitter._do_radar()
    assert emitter._radar_result.tiles['variant'] == 'native'
    assert len(emitter._radar_result.frames) == 1
    assert all(key.startswith('NEA_') for _, key in native.calls)


def test_watch_paused_uses_labelled_iem_fallback(
        make_emitter, hybrid, multisite, native, monkeypatch):
    monkeypatch.setattr(ae, 'RADAR_ATTENTION_MODE', 'active')
    emitter = make_emitter()
    emitter._radar_attention.forced = emitter._radar_attention.tier = 'watch'
    emitter._radar_native_budget.add(budget.NATIVE_PAUSE_BYTES+1)
    emitter._do_radar()
    r = emitter._build_payload()['radar']
    assert not native.calls and not r['native'] and r['frameCount'] == 1
    assert r['nativeFallback'] == dict(active=True, reason='daily-limit', recovering=False)
    assert {c[1] for c in multisite.calls} == {'KNEA'}


@pytest.mark.parametrize('age,kind,logged', [
    (60, 'unpublished', False), (600, 'unpublished', False), (601, 'unpublished', True),
    (60, 'invalid', False), (60, 'dns', True), (60, 'transport', True),
    (60, 'ambiguous', True), (60, 'circuit', True),
])
def test_count_all_inputs_but_warn_only_outages_and_overdue_publication(
        make_emitter, hybrid, monkeypatch, age, kind, logged):
    from lib.radar_http import AmbiguousTransportError
    from lib.radar_fetch import CircuitOpen
    errors = dict(unpublished=ae._RadarScanUnpublished('not published'), invalid=ValueError('bad product'),
                  dns=socket.gaierror(-3, 'dns'), transport=ConnectionError('reset'),
                  ambiguous=AmbiguousTransportError('no first byte'), circuit=CircuitOpen('open'))
    messages = []
    monkeypatch.setattr(ae.Logger, 'warning', messages.append)
    e = make_emitter()
    failures = [('KNEA', errors[kind], hybrid.now-age)]
    e._radar_level3_site_failures(failures)
    e._radar_level3_site_failures(failures)
    assert e._radar_health_payload()['mosaic']['siteFailures']['KNEA']['count'] == 2
    assert len(messages) == int(logged)
    hybrid.mono += ae.RADAR_FAILURE_LOG_SEC
    e._radar_level3_site_failures(failures)
    if logged:
        assert len(messages) == 2 and '1 not logged' in messages[-1]


def test_unpublished_type_survives_negative_cache(make_emitter, hybrid, multisite, native):
    native.missing.add(hybrid.latest)
    e = make_emitter(); e._radar_begin_log_pass(); e._radar_session = ae.RadarSession()
    for _ in range(2):
        with pytest.raises(ae._RadarScanUnpublished):
            e._radar_level3_scan('KNEA', hybrid.latest, {}, hybrid.mono+20)


def test_whole_network_outage_resets_streak_once_per_level3_window(
        make_emitter, hybrid, multisite, native, monkeypatch):
    """Keep discovery available from cache; both providers' transports are down."""
    e = make_emitter()
    attempts, retries = [], []
    def fail_inputs(pairs, stamp, ctx, deadline):
        attempts.append(hybrid.mono)
        error = socket.gaierror(-3, 'whole network offline')
        e._radar_level3_fallback(error)
        ctx['level3_failed'] = True
        raise error
    monkeypatch.setattr(e, '_radar_mosaic_inputs', fail_inputs)
    def fail_tiles(site, url):
        raise socket.gaierror(-3, 'whole network offline')
    multisite.failure = fail_tiles
    monkeypatch.setattr(e, '_radar_budget_retry', lambda source, n, min_delay=0, reason=None:
                        retries.append((min_delay, reason)))
    e._radar_local_failure_streak = 5
    e._do_radar()
    assert attempts == [0] and e._radar_local_failure_streak == 0
    assert retries[-1] == (2, 'provider')
    for second in (2, 4, 8, 16, 32, 64, 119):
        hybrid.mono = second
        e._do_radar()
    assert attempts == [0]
    assert e._radar_local_failure_streak > 1 and e._radar_local_backoff() > 2
    hybrid.mono = 120
    e._do_radar()
    assert attempts == [0, 120] and e._radar_local_failure_streak == 0
    assert retries[-1] == (2, 'provider')


def test_captions_label_fallback_and_recovery_without_sharpening():
    controls(r'''
Object.assign(radarView.data,{sourceId:'iem-nexrad-n0b',sourceMode:'site',siteId:'KATX',native:true});
radarView.pendingSource={variantOnly:true,data:{native:false,nativeFallback:{active:true,reason:'level3-unreachable'}}};
radarSourceRender();assert.match(caption(),/^Level III unreachable · loading IEM tiles · showing NOAA Level III/);
radarView.data.native=false;radarView.data.nativeFallback=radarView.pendingSource.data.nativeFallback;radarView.pendingSource=null;
radarSourceRender();assert.match(caption(),/^Level III unreachable · showing IEM tiles/);
radarView.data.nativeFallback.reason='daily-limit';radarSourceRender();assert.match(caption(),/^Daily Level III limit reached · showing IEM tiles/);
radarView.pendingSource={variantOnly:true,data:{native:true}};radarSourceRender();assert.match(caption(),/^Restoring NOAA Level III · showing IEM tiles/);
radarView.pendingSource=null;radarView.data.nativeFallback={active:true,reason:null,recovering:true};
radarSourceRender();assert.match(caption(),/^Restoring NOAA Level III · showing IEM tiles/);
radarView.data.native=true;radarView.data.nativeFallback={active:false,reason:null,recovering:false};
radarSourceRender();assert.match(caption(),/NOAA Level III/);assert.doesNotMatch(caption(),/IEM tiles|Sharpening/);
''')


def test_smooth_still_works_on_region_and_fallback_but_not_native():
    html = Path('design/almanac/console_live.html').read_text()
    assert all(text not in html for text in ('rad-render', 'radarRender', 'X-Radar-Render', 'rad-v1', 'rad-v2'))
    run_remote(r'''
const a=page();await a.poll();
a.run("radarView.data={...manifest(),native:true,sourceMode:'site'};radarZoomRender();$('rad-smooth').listeners.click();assert.equal(radarSmooth.pending,null)");
a.run("radarView.data.native=false;$('rad-smooth').listeners.click()");await new Promise(setImmediate);assert.equal(server.smooth,'on');
a.run("radarView.data.sourceMode='mosaic';$('rad-smooth').listeners.click()");await new Promise(setImmediate);assert.equal(server.smooth,'off');
assert.ok(server.requests.every(q=>!q.has('radarRender')));
''')


def test_watch_acquires_matching_n0h_and_accounts_both_products(
        make_emitter, hybrid, multisite, native, monkeypatch):
    import io
    from datetime import datetime, timezone
    from tests.test_radar_n0h import n0h_product
    monkeypatch.setattr(ae, 'RADAR_ATTENTION_MODE', 'active')
    opened = ae.RadarSession.open
    bodies, hca = [], []
    stamp = hybrid.latest+24
    key = 'NEA_N0H_' + datetime.fromtimestamp(stamp, timezone.utc).strftime('%Y_%m_%d_%H_%M_%S')
    def fetch(session, req, timeout):
        if 'NEA_N0H' in req.full_url:
            hca.append(req.full_url)
            raw = (f'<ListBucketResult><Key>{key}</Key></ListBucketResult>'.encode()
                   if '?' in req.full_url else n0h_product(volume_ts=stamp))
            response = io.BytesIO(raw)
        else:
            response = opened(session, req, timeout)
        if req.full_url.startswith(ae.RADAR_LEVEL3_BUCKET):
            bodies.append(len(response.getvalue()))
        return response
    monkeypatch.setattr(ae.RadarSession, 'open', fetch)
    e = make_emitter(); e._radar_attention.forced = e._radar_attention.tier = 'watch'
    e._do_radar()
    assert len(hca) == 2  # one listing and one N0H product, no neighbours/history
    assert e._radar_result.frames[-1]['siteScans'] == [dict(id='KNEA', ts=hybrid.latest, volumeTs=stamp, filtered=True)]
    assert e._radar_native_budget.snapshot()['bytesToday'] == sum(bodies)


def test_unattended_unpublished_newest_never_downloads_older_scan(
        make_emitter, hybrid, multisite, native, monkeypatch):
    monkeypatch.setattr(ae, 'RADAR_ATTENTION_MODE', 'active')
    native.missing.add(hybrid.latest)
    e = make_emitter(); e._radar_attention.forced = e._radar_attention.tier = 'watch'
    e._do_radar()
    assert not [c for c in native.calls if c[0] == 'get']
    assert all(key.startswith('NEA_') for _, key in native.calls)
    assert not [c for c in multisite.calls if c[0] == 'tile']
    assert not e._radar_level3_down()


@pytest.mark.parametrize('native_first', [True, False])
def test_fallback_and_recovery_keep_decoded_map_until_newest_is_ready(native_first):
    from tests.test_radar_buffer_page import run_page
    run_page(r'''
const first=manifest('b');first.native=NATIVE;first.tiles.variant=NATIVE?'native':false;
radarView.data=first;radarView.loaded=first.tiles.frames.map(f=>decode({...f,sourceId:'b',revision:first.tiles.revision}));
radarView.good=radarView.current=radarView.loaded.at(-1);radarView.cycle=radarView.loaded.slice();
radarView.readyFrames=radarView.loaded.slice();radarView.windowKey=radarWindowKey(first);
const held=radarView.current, next=structuredClone(first);next.native=!NATIVE;next.tiles.variant=!NATIVE?'native':false;next.tiles.revision='abcdef123456';
renderRadar({radar:next,ts:100900});
assert.ok(radarView.pendingSource.variantOnly);assert.equal(radarView.current,held);assert.equal(held.bitmap.closes,0);
radarAcceptSource();assert.ok(radarView.pendingSource);assert.equal(radarView.current,held);
decode(radarView.pendingSource.frames.at(-1));radarAcceptSource();
assert.equal(radarView.pendingSource,null);assert.equal(radarView.data.native,!NATIVE);assert.ok(radarView.current.bitmap.width);
'''.replace('NATIVE', 'true' if native_first else 'false'))


def test_region_soft_ceiling_warms_primary_newest_native(
        make_emitter, hybrid, multisite, native, monkeypatch, tmp_path):
    (tmp_path/'radar_source').write_text('mosaic')
    (tmp_path/'radar_viewed').unlink(); (tmp_path/'radar_viewing').unlink()
    e = make_emitter(); e._radar_native_budget.add(budget.NATIVE_NEWEST_ONLY_BYTES+1)
    e._radar_attention.forced = e._radar_attention.tier = 'live'
    e._do_radar()
    source, ctx = e._radar_idle_context
    hybrid.view()
    monkeypatch.setattr(e, '_radar_headroom_delay', lambda *args: 0)
    e._radar_prefetch(source, dict(ctx, viewed=True, refresh=dict(state='idle')))
    assert native.calls and all(key.startswith('NEA_') for _, key in native.calls)
    from lib.radar_level3 import s3_key_time
    assert all(s3_key_time(key) == hybrid.latest+24 for kind, key in native.calls if kind == 'get')
    assert not [c for c in multisite.calls if c[0] == 'tile']
    assert e._radar_result.source_mode == 'mosaic'


def test_live_expands_warmed_primary_without_redownloading_it(
        make_emitter, hybrid, multisite, native, monkeypatch):
    monkeypatch.setattr(ae, 'RADAR_ATTENTION_MODE', 'active')
    e = make_emitter(); e._radar_attention.forced = e._radar_attention.tier = 'watch'
    e._do_radar()
    first = [key for kind, key in native.calls if kind == 'get']
    assert len(first) == 1
    native.calls.clear()
    e._radar_attention.forced = e._radar_attention.tier = 'live'
    e._do_radar(discovery=True, intent_triggered=False)
    assert len(e._radar_result.frames) > 1
    assert len(e._radar_result.frames[-1]['siteScans']) > 1
    assert not [key for kind, key in native.calls if kind == 'get' and key in first]


def test_initial_native_loading_is_not_mislabelled_as_iem_fallback():
    controls(r'''
Object.assign(radarView.data,{sourceId:'iem-nexrad-n0b',sourceMode:'site',siteId:'KATX',native:false,
 nativeFallback:{active:false,reason:null,recovering:false},tiles:{frames:[]}});
radarSourceRender();assert.doesNotMatch(caption(),/unreachable|showing IEM tiles|Restoring/);
''')


@pytest.mark.parametrize('tier', ['rest', 'dormant'])
@pytest.mark.parametrize('hour', [2, 14])
def test_quiet_tiers_fetch_no_site_tiles_or_level3_products_with_sentinel_due(
        make_emitter, hybrid, multisite, native, monkeypatch, tier, hour):
    monkeypatch.setattr(ae, 'RADAR_ATTENTION_MODE', 'active')
    e = make_emitter(); e._radar_attention.forced = e._radar_attention.tier = tier
    e._radar_local_hour = hour
    for elapsed in (0, 3600, 7200):
        hybrid.mono = elapsed
        hybrid.latest = hybrid.now + elapsed - 360
        e._do_radar()
    assert not native.calls
    assert all(call[0] == 'list' for call in multisite.calls)
    assert all(call[2] == ae.RADAR_IEM_METADATA_URL or '/mrms::lcref-' in call[2]
               for call in hybrid.calls)
    sentinel_tiles = [call for call in hybrid.calls if '/mrms::lcref-' in call[2]]
    if tier == 'rest':
        assert len(sentinel_tiles) == (12 if hour == 14 else 8)
        assert e._radar_sentinel is not None
    else:
        assert not hybrid.calls and e._radar_sentinel is None
