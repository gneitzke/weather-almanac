"""Independent v2-only review regressions; all acquisitions use local fixtures."""
import json
import urllib.error
from pathlib import Path

import pytest

from lib import almanac_emit as ae
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401
from tests.test_radar_level3 import native  # noqa: F401
from tests.test_radar_auto import intent
from tests.test_radar_auto_page import controls
from tests.test_radar_buffer_page import run_page

SITE = 'iem-nexrad-n0b'


def tier(emitter, tmp_path, name):
    for marker in ('radar_viewed', 'radar_viewing'):
        (tmp_path / marker).unlink(missing_ok=True)
    emitter._radar_attention.forced = emitter._radar_attention.tier = name


def full_timeline(multisite, latest):
    for site in ('KNEA', 'KMID'):
        multisite.scans[site] = list(range(latest-7*300, latest+1, 300))


@pytest.mark.parametrize('cold', [False, True])
def test_watch_publication_lag_waits_for_negative_expiry_without_failure(
        make_emitter, hybrid, multisite, native, tmp_path, monkeypatch, cold):
    emitter = make_emitter()
    tier(emitter, tmp_path, 'watch')
    if not cold:
        emitter._do_radar()
    retained = emitter._radar_result
    latest = hybrid.latest + 300
    multisite.scans['KNEA'].append(latest)
    native.missing.add(latest)
    hybrid.now = latest + 90
    warnings, retries = [], []
    monkeypatch.setattr(ae.Logger, 'warning', warnings.append)
    monkeypatch.setattr(emitter, '_schedule_retry', lambda key, callback, delay, **kw: retries.append(hybrid.mono+delay))
    native.calls.clear()
    for elapsed in (0, 2, 4, 59):
        hybrid.mono = elapsed
        emitter._do_radar(discovery=True, intent_triggered=False)
        assert emitter._radar_pass['outcome'] == 'unpublished'
        assert not emitter._radar_transport_failures
        assert emitter._radar_result.frames == retained.frames
        assert emitter._radar_result.source_id == SITE
        assert retries[-1] == 60
    assert warnings == []
    assert not [c for c in native.calls if c[0] == 'get']
    assert len([c for c in native.calls if c[0] == 'list']) == int(cold)
    native.missing.clear()
    hybrid.mono = 60
    emitter._do_radar(discovery=True, intent_triggered=False)
    assert emitter._radar_result.ts_frame == latest
    assert len(emitter._radar_result.frames) == 1
    assert emitter._radar_result.frames[0]['primaryOnly']


def test_watch_overdue_publication_reaches_warning_bound(
        make_emitter, hybrid, multisite, native, tmp_path, monkeypatch):
    emitter = make_emitter()
    tier(emitter, tmp_path, 'watch')
    native.missing.add(hybrid.latest)
    warnings = []
    monkeypatch.setattr(ae.Logger, 'warning', warnings.append)
    hybrid.now = hybrid.latest + 600
    emitter._do_radar()
    assert not warnings and not emitter._radar_transport_failures
    hybrid.mono = 60
    emitter._do_radar(discovery=True, intent_triggered=False)
    assert any('scan unavailable' in message for message in warnings)
    assert emitter._radar_transport_failures[SITE] == 1


@pytest.mark.parametrize('neighbours', [False, True])
@pytest.mark.parametrize('new_scan', [False, True])
def test_watch_frame_is_rebuilt_with_neighbours_on_attendance(
        make_emitter, hybrid, multisite, native, tmp_path, new_scan, neighbours):
    full_timeline(multisite, hybrid.latest)
    if not neighbours:
        multisite.scans['KMID'] = []
    emitter = make_emitter()
    tier(emitter, tmp_path, 'watch')
    emitter._do_radar()
    watch = emitter._radar_result.frames[-1]
    assert watch['primaryOnly'] and len(watch['requestedPairs']) == 1
    if new_scan:
        for site in (('KNEA', 'KMID') if neighbours else ('KNEA',)):
            multisite.scans[site].append(hybrid.latest+300)
        hybrid.mono += 300
    tier(emitter, tmp_path, 'live')
    emitter._do_radar(discovery=True, intent_triggered=True)
    emitter._do_radar(discovery=True, intent_triggered=False)
    frames = emitter._radar_result.frames
    assert len(frames) == 8 and all(f['complete'] for f in frames)
    assert all(not f.get('primaryOnly') for f in frames)
    expected = {'KNEA', 'KMID'} if neighbours else {'KNEA'}
    assert all({p['id'] for p in f['siteScans']} == expected for f in frames)
    rebuilt = next(f for f in frames if f['ts'] == watch['ts'])
    assert (rebuilt['mosaicKey'] != watch['mosaicKey']) is neighbours
    downloads = [key for kind, key in native.calls if kind == 'get']
    assert len(downloads) == len(set(downloads)), 'warmed products should be reused'


@pytest.mark.parametrize('attention,forced,count', [('active', 'warm', 4), ('active', 'live', 8), ('shadow', 'watch', 1)])
def test_unviewed_full_mosaic_policy(make_emitter, hybrid, multisite, native, tmp_path,
                                    monkeypatch, attention, forced, count):
    full_timeline(multisite, hybrid.latest)
    monkeypatch.setattr(ae, 'RADAR_ATTENTION_MODE', attention)
    emitter = make_emitter()
    tier(emitter, tmp_path, forced)
    emitter._do_radar()
    frames = emitter._radar_result.frames
    assert sum(f['complete'] for f in frames) >= count
    assert all({p['id'] for p in f['siteScans']} == {'KNEA', 'KMID'} for f in frames if f['complete'])
    assert not any(f.get('primaryOnly') for f in frames)
    assert len({k for kind, k in native.calls if kind == 'get'}) == 2*count


@pytest.mark.parametrize('retry_after', ['3600', 'Fri, 25 Sep 2099 00:00:00 GMT'])
def test_level3_429_caps_cooldown_and_falls_back_then_recovers(
        make_emitter, hybrid, multisite, native, monkeypatch, retry_after):
    emitter = make_emitter()
    emitter._do_radar()
    opened = ae.RadarSession.open
    def limited(session, request, timeout):
        if request.full_url.startswith(ae.RADAR_LEVEL3_BUCKET):
            raise urllib.error.HTTPError(request.full_url, 429, 'limited', {'Retry-After': retry_after}, None)
        return opened(session, request, timeout)
    monkeypatch.setattr(ae.RadarSession, 'open', limited)
    emitter._radar_begin_log_pass()
    with pytest.raises(ae._RadarBudget):
        emitter._radar_request(ae.RADAR_LEVEL3_TRANSPORT, ae.RADAR_LEVEL3_BUCKET+'test', 20)
    assert emitter._radar_cooldowns[ae.RADAR_LEVEL3_TRANSPORT] == 300
    native.calls.clear()
    for elapsed in (0, 30, 120, 299):
        hybrid.mono = elapsed
        emitter._do_radar(discovery=True, intent_triggered=False)
        radar = emitter._build_payload()['radar']
        assert radar['tiles']['variant'] is False
        assert radar['nativeFallback'] == dict(active=True, reason='level3-unreachable', recovering=False)
    assert not native.calls
    hybrid.mono = 300
    assert not emitter._radar_level3_down()
    assert not emitter._radar_native_fallback(emitter._radar_result)['recovering']
    monkeypatch.setattr(ae.RadarSession, 'open', opened)
    for site in ('KNEA', 'KMID'):
        multisite.scans[site].append(hybrid.latest+300)
    emitter._do_radar(discovery=True, intent_triggered=False)
    assert emitter._radar_result.tiles['variant'] == 'native'
    assert emitter._radar_level3_outage is None


def test_auto_watch_holds_attended_mode_without_neighbour_requests(
        make_emitter, hybrid, multisite, native, tmp_path, monkeypatch):
    monkeypatch.setattr(ae, '_NEXRAD_SITES', {
        'KNEA': (48.86, -122.33, 'north'), 'KMID': (46.26, -122.33, 'south'),
        'KFAR': (49.5, -122.33, 'far')})
    multisite.scans['KMID'] = list(multisite.scans['KNEA'])
    intent(tmp_path, 8, 'auto')
    emitter = make_emitter()
    emitter._do_radar()
    assert emitter._radar_result.source_mode == 'site'
    coverage = next(iter(emitter._radar_auto_evidence.values()))['coverage']
    assert coverage > .98
    tier(emitter, tmp_path, 'watch')
    multisite.calls.clear()
    emitter._do_radar(discovery=True, intent_triggered=False)
    assert emitter._radar_result.source_mode == 'site'
    assert multisite.calls == [('list', 'KNEA')]
    assert next(iter(emitter._radar_auto_evidence.values()))['coverage'] == coverage


@pytest.mark.parametrize('in_view', [False, True])
def test_watch_saved_camera_never_downloads_offscreen_primary(
        make_emitter, hybrid, multisite, native, tmp_path, monkeypatch, in_view):
    (tmp_path/'radar_center').write_text('47.61,-129.5')
    (tmp_path/'radar_zoom').write_text('8')
    if in_view:
        monkeypatch.setitem(ae._NEXRAD_SITES, 'KMID', (47.61, -129.5, 'in view'))
        multisite.scans['KMID'] = [hybrid.latest]
    emitter = make_emitter()
    tier(emitter, tmp_path, 'watch')
    emitter._do_radar()
    if in_view:
        assert emitter._radar_result.site_id == 'KMID'
        assert emitter._radar_result.frames[-1]['complete']
        assert native.calls and all(key.startswith('MID_') for _, key in native.calls)
    else:
        assert not native.calls and not multisite.calls
        assert not emitter._radar_result.frames
        assert 'no viewport coverage' in emitter._radar_pass['error']
        wire = emitter._build_payload()['radar']
        assert wire['reason'] == 'out of view'
        assert next(s for s in wire['sources'] if s['mode'] == 'site')['reason'] == 'out of view'


def production_function(name):
    html = Path('design/almanac/console_live.html').read_text()
    start = html.index('  function '+name+'(')
    end = html.index('\n  function ', start+1)
    return name+'=function'+html[start:end].strip().removeprefix('function '+name)+';\n'


@pytest.mark.parametrize('recovering', [False, True])
def test_corner_note_names_renderer_transition(recovering):
    run_page(production_function('radarNoteRender')+r'''
radarIntent.postedAt=Date.now()-60000;radarPendingRetry=()=>null;
radarView.refresh={state:'idle'};
Object.assign(radarView.data,{sourceMode:'site',native:!RECOVERY});
radarView.pendingSource={variantOnly:true,frames:[],data:{native:RECOVERY,nativeFallback:{active:true,reason:'level3-unreachable'}}};
radarNoteRender();
assert.equal($('rad-note').textContent,RECOVERY?'Restoring NOAA Level III · showing IEM tiles':'Level III unreachable · loading IEM tiles · showing NOAA Level III');
assert.doesNotMatch($('rad-note').textContent,/sharpening/i);
'''.replace('RECOVERY', json.dumps(recovering)))


def test_outage_expiry_and_half_open_do_not_claim_recovery(
        make_emitter, hybrid, multisite, native):
    emitter = make_emitter()
    emitter._radar_level3_fallback(ConnectionError('down'))
    emitter._do_radar()
    fallback = emitter._radar_result
    for elapsed in (119, 120, 121, 240):
        hybrid.mono = elapsed
        assert emitter._radar_native_fallback(fallback) == dict(active=True, reason='level3-unreachable', recovering=False)
    url = ae.RADAR_LEVEL3_BUCKET+'?list-type=2&prefix=NEA_N0B_2026_09_13_00'
    host = emitter._radar_health._host(ae.RADAR_LEVEL3_TRANSPORT, url)
    host['until'] = hybrid.mono
    assert emitter._radar_health.admit(ae.RADAR_LEVEL3_TRANSPORT, url, metadata=True)
    assert emitter._radar_health.state(host) == 'half'
    assert not emitter._radar_native_fallback(fallback)['recovering']
    emitter._radar_health.record(ae.RADAR_LEVEL3_TRANSPORT, url, True, probe=True)
    # A successful listing is not proof that an N0B product can be fetched.
    emitter._radar_request(ae.RADAR_LEVEL3_TRANSPORT,
        ae.RADAR_LEVEL3_BUCKET+'?list-type=2&prefix=NEA_N0B_2026_09_13_00', 260, metadata=True)
    assert not emitter._radar_native_fallback(fallback)['recovering']
    emitter._radar_level3_scan('KNEA', hybrid.latest, {}, 260)
    assert emitter._radar_native_fallback(fallback) == dict(active=True, reason=None, recovering=True)


@pytest.mark.parametrize('ledger,ceiling', [('retrying', 'normal'), ('ok', 'paused')])
def test_budget_notices_are_site_only_and_not_repeated_in_fallback(ledger, ceiling):
    controls(production_function('radarNoteRender')+production_function('radarZoomRender')+r'''
radarIntent.postedAt=Date.now()-60000;radarView.refresh={state:'idle'};
Object.assign(radarView.data,{zoomMax:7,zoomSource:'Region',nativeBudget:{ledgerState:LEDGER,ceilingState:CEILING}});
radarZoomRender();radarSourceRender();
assert.match($('rad-note').textContent,/scaled to this view/);
assert.doesNotMatch(caption(),/Level III|accounting|daily data|v2/);
Object.assign(radarView.data,{sourceMode:'site',sourceId:'iem-nexrad-n0b',native:true,zoomMax:10});
radarZoomRender();
assert.match($('rad-note').textContent,LEDGER==='retrying'?/Level III byte ledger retrying/:/Daily Level III limit reached/);
Object.assign(radarView.data,{native:false,nativeFallback:{active:true,reason:CEILING==='paused'?'daily-limit':'level3-unreachable'}});
radarZoomRender();radarSourceRender();
assert.equal((caption().match(/Daily Level III limit reached/g)||[]).length,CEILING==='paused'?1:0);
assert.doesNotMatch(caption(),/v2|byte ledger|accounting|daily data limit/);
'''.replace('LEDGER', json.dumps(ledger)).replace('CEILING', json.dumps(ceiling)))


@pytest.mark.parametrize('mode', ['mosaic', 'auto'])
def test_region_evidence_remains_independent_when_level3_cools_mid_pass(
        make_emitter, hybrid, multisite, native, tmp_path, monkeypatch, mode):
    intent(tmp_path, 6, mode)
    emitter = make_emitter()
    emitter._do_radar()
    reserve = emitter._radar_mandatory_reserve
    seen = []
    def cooldown_after_policy(source, ctx, stamps, *args, **kwargs):
        # A worker/429 can start a cooldown after native policy was snapshotted.
        assert ctx['native'] is True
        emitter._radar_cooldowns[ae.RADAR_LEVEL3_TRANSPORT] = hybrid.mono+300
        seen.append(True)
        return reserve(source, ctx, stamps, *args, **kwargs)
    monkeypatch.setattr(emitter, '_radar_mandatory_reserve', cooldown_after_policy)
    multisite.calls.clear()
    emitter._do_radar(discovery=True, intent_triggered=False)
    assert seen and multisite.calls == [('list', 'KNEA')]
    assert emitter._build_payload()['radar']['nexrad']['reporting'] is True


def test_watch_publication_retry_also_holds_discovery_wakeup(
        make_emitter, hybrid, multisite, native, tmp_path, monkeypatch):
    from tests.test_emitter_lifecycle import FakeClock
    clock = FakeClock()
    monkeypatch.setattr(ae, 'Clock', clock)
    emitter = make_emitter()
    tier(emitter, tmp_path, 'watch')
    emitter._running = True
    native.missing.add(hybrid.latest)
    hybrid.now = hybrid.latest+90
    emitter._do_radar()
    assert emitter._radar_next_retry == hybrid.now+60
    assert emitter._radar_discovery.due >= hybrid.now+60
    assert emitter._radar_discovery_event.due >= 60
    # An intent validation before expiry must replace, not inherit, a 2s retry.
    emitter._radar_clear_retry()
    emitter._schedule_retry('radar', emitter._check_radar, 2)
    emitter._do_radar(intent_triggered=True)
    assert emitter._radar_next_retry == hybrid.now+60
    assert not emitter._radar_transport_failures
    emitter.stop()


def test_cold_auto_watch_uses_cached_neighbour_coverage_without_fetching_it(
        make_emitter, hybrid, multisite, native, tmp_path, monkeypatch):
    monkeypatch.setattr(ae, '_NEXRAD_SITES', {
        'KNEA': (48.86, -122.33, 'north'), 'KMID': (46.26, -122.33, 'south'),
        'KFAR': (49.5, -122.33, 'far')})
    intent(tmp_path, 8, 'auto')
    emitter = make_emitter()
    tier(emitter, tmp_path, 'watch')
    emitter._radar_site_status['KMID'] = dict(reporting=True, newestTs=hybrid.latest,
        checkedTs=hybrid.now, reason=None)
    emitter._do_radar()
    assert emitter._radar_result.source_mode == 'site'
    assert next(iter(emitter._radar_auto_evidence.values()))['coverage'] > .98
    assert multisite.calls == [('list', 'KNEA')]
    assert all(key.startswith('NEA_') for _, key in native.calls)


def test_attendance_rebuilds_watch_even_while_the_next_scan_is_unpublished(
        make_emitter, hybrid, multisite, native, tmp_path):
    full_timeline(multisite, hybrid.latest)
    emitter = make_emitter()
    tier(emitter, tmp_path, 'watch')
    emitter._do_radar()
    original = emitter._radar_result
    for site in ('KNEA', 'KMID'):
        multisite.scans[site].append(hybrid.latest+300)
    native.missing.add(hybrid.latest+300)
    hybrid.mono = 60  # expire the hourly listing cache
    tier(emitter, tmp_path, 'live')
    emitter._do_radar(discovery=True, intent_triggered=False)
    current = emitter._radar_result
    assert current.ts_frame == original.ts_frame
    assert current.ts_fetch == original.ts_fetch, 'older scans must retain their measured freshness'
    assert len(current.frames) == 8 and all(f['complete'] for f in current.frames)
    assert all(not f.get('primaryOnly') for f in current.frames)
    assert all({p['id'] for p in f['siteScans']} == {'KNEA', 'KMID'} for f in current.frames)
