"""Second review: real engine, page and server paths; all transport simulated."""
import json
import os
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from lib import almanac_emit as ae, radar_auto as auto, radar_native_budget as budget
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401
from tests.test_radar_level3 import native  # noqa: F401
from tests.test_radar_auto import intent
from tests.test_radar_buffer_page import run_page
from tests.test_radar_auto_page import controls
from tests.test_freshness_health import _load_serve


def fail_listings(emitter, monkeypatch, site=None):
    request = emitter._radar_request
    def unavailable(source, url, *args, **kwargs):
        if 'operation=list' in url and (site is None or 'radar='+site[1:] in url):
            raise TimeoutError('listing endpoint unavailable')
        return request(source, url, *args, **kwargs)
    monkeypatch.setattr(emitter, '_radar_request', unavailable)
    emitter._radar_forget('iem-nexrad-n0b', site)


def test_dead_listing_falls_back_and_region_keeps_advancing(make_emitter, hybrid, multisite, tmp_path, monkeypatch):
    intent(tmp_path, 9)
    emitter = make_emitter(); emitter._do_radar()
    assert emitter._radar_result.source_mode == 'site'
    fail_listings(emitter, monkeypatch)
    published = []
    publish = emitter._radar_publish_refresh
    def observe(ctx, *args, **kwargs):
        if ctx.get('staging_source'):
            published.append(ctx['staging_source'])
        return publish(ctx, *args, **kwargs)
    monkeypatch.setattr(emitter, '_radar_publish_refresh', observe)
    for elapsed in (600, 1200, 1800, 3600):
        hybrid.latest += elapsed-hybrid.mono
        hybrid.mono = elapsed
        emitter._do_radar(discovery=True, intent_triggered=False)
        assert emitter._radar_result.source_mode == 'mosaic'
        assert emitter._radar_result.ts_frame == hybrid.latest
        assert emitter._radar_site_status['KNEA']['reporting'] is not True
    assert 'iem-mrms-lcref' in published


@pytest.mark.parametrize('failure', ['budget', 'timeout'])
def test_unknown_site_hold_ends_at_display_freshness_boundary(make_emitter, hybrid, multisite, tmp_path, monkeypatch, failure):
    intent(tmp_path, 9)
    emitter = make_emitter(); emitter._do_radar()
    stamp = emitter._radar_result.ts_frame
    def unavailable(ctx):
        raise ae._RadarBudget('busy') if failure == 'budget' else TimeoutError('busy')
    monkeypatch.setattr(emitter, '_radar_site_discover', unavailable)
    for age, expected in [(ae.RADAR_SITE_MAX_AGE_SEC-1, 'site'), (ae.RADAR_SITE_MAX_AGE_SEC, 'mosaic')]:
        hybrid.mono = stamp+age-hybrid.now
        hybrid.latest = int((ae.time.time()-60)//60)*60
        emitter._do_radar(discovery=True, intent_triggered=False)
        assert emitter._radar_result.source_mode == expected


def test_site_adapter_failure_limit_has_region_adapter(make_emitter, hybrid, multisite, tmp_path, monkeypatch):
    intent(tmp_path, 9)
    emitter = make_emitter(); emitter._do_radar()
    def fail(ctx): raise ValueError('site tile adapter failed')
    monkeypatch.setattr(emitter, '_radar_site_frames', fail)
    for number in range(1, 4):
        hybrid.mono += 12
        emitter._do_radar(discovery=True, intent_triggered=False)
        assert emitter._radar_result.source_mode == ('site' if number < 3 else 'mosaic')


def test_redundant_failed_listing_cannot_veto_site_entry(make_emitter, hybrid, multisite, tmp_path, monkeypatch):
    intent(tmp_path, 9)
    emitter = make_emitter()
    fail_listings(emitter, monkeypatch, 'KFAR')
    emitter._do_radar()
    assert emitter._radar_result.source_mode == 'site'
    assert emitter._radar_site_status['KFAR']['reporting'] is None


def test_coverage_relevant_unknown_expires_after_one_cadence(make_emitter, hybrid, multisite, tmp_path, monkeypatch):
    intent(tmp_path, 9)
    emitter = make_emitter()
    # Deterministic coverage boundary: the missing neighbour matters initially.
    monkeypatch.setattr(auto, 'coverage_fraction', lambda bounds, sites, radius: .9 if any(s['id']=='KMID' for s in sites) else .6)
    fail_listings(emitter, monkeypatch, 'KMID')
    emitter._do_radar()
    assert emitter._radar_result.source_mode == 'mosaic'
    first = emitter._radar_site_status['KMID']['failedSince']
    hybrid.mono += ae._RADAR_SOURCES['iem-nexrad-n0b']['cadence']
    emitter._do_radar(discovery=True, intent_triggered=False)
    assert emitter._radar_site_status['KMID']['reporting'] is False
    assert emitter._radar_site_status['KMID']['failedSince'] == first
    assert next(iter(emitter._radar_auto_evidence.values()))['coverage'] == .6


@pytest.mark.parametrize('mode,zoom', [('mosaic', 9), ('auto', 5), ('auto', 7)])
def test_region_listing_and_warming_do_not_add_unused_breaker_wakes(make_emitter, hybrid, multisite, native, tmp_path, monkeypatch, mode, zoom):
    intent(tmp_path, zoom, mode)
    hybrid.view()
    emitter = make_emitter(); emitter._do_radar()
    assert emitter._radar_result.source_mode == 'mosaic'
    def forbidden(*args): raise AssertionError('Region ran an unused Auto coverage decision')
    monkeypatch.setattr(auto, 'coverage_fraction', forbidden)
    for _ in range(8):
        emitter._radar_health.record(ae.RADAR_LEVEL3_TRANSPORT, ae.RADAR_LEVEL3_BUCKET+'test', False, TimeoutError('S3 unavailable'))
    hybrid.mono += 10000
    # Keep a manual lease alive and Region fresh after the artificial cooldown.
    if mode == 'mosaic':
        (tmp_path/'presence').write_text(str(ae.time.time()))
    hybrid.latest += 9960
    hybrid.view()
    assert emitter._radar_probe_delay() is None
    emitter._do_radar(discovery=True, intent_triggered=False)
    source, ctx = emitter._radar_idle_context
    monkeypatch.setattr(emitter, '_radar_headroom_delay', lambda *args: 0)
    emitter._radar_prefetch(source, dict(ctx, viewed=True, refresh=dict(state='idle')))
    assert ('list', 'KNEA') in multisite.calls
    assert emitter._radar_probe_delay() is None
    assert emitter._radar_pass['outcome'] != 'superseded'
    emitter._running = True
    scheduled = []
    monkeypatch.setattr(emitter, '_schedule', lambda callback, delay: scheduled.append(delay))
    emitter._radar_arm_discovery()
    assert scheduled[-1] > 1


def test_eligible_auto_site_dependency_still_probes(make_emitter, monkeypatch):
    emitter = make_emitter()
    emitter._radar_result = emitter._radar_result._replace(source_pref='auto', source_mode='mosaic', source_id='iem-mrms-lcref', zoom_desired=8)
    seen = []
    monkeypatch.setattr(emitter._radar_health, 'probe_delay', lambda sources: seen.append(sources) or 12)
    assert emitter._radar_probe_delay() == 12
    assert 'iem-nexrad-n0b' in seen[0]


def test_coverage_geometry_is_reused_until_reporting_set_changes(make_emitter, hybrid, multisite, tmp_path, monkeypatch):
    intent(tmp_path, 9)
    measurements = []
    original = auto.coverage_fraction
    def measure(*args):
        measurements.append(args)
        return original(*args)
    monkeypatch.setattr(auto, 'coverage_fraction', measure)
    emitter = make_emitter(); emitter._do_radar()
    for _ in range(3): emitter._do_radar(discovery=True, intent_triggered=False)
    assert len(measurements) == 1
    multisite.scans['KMID'] = []
    emitter._radar_forget('iem-nexrad-n0b', 'KMID')
    emitter._do_radar(discovery=True, intent_triggered=False)
    assert len(measurements) == 2


@pytest.mark.parametrize('count', [1, 2, 3])
def test_pending_short_publication_keeps_tile_queue_running(count):
    run_page(r'''
const r=manifest('b');r.tiles.frames=r.tiles.frames.slice(-COUNT);r.refresh={state:'history',frameTotal:8};
renderRadar({radar:r,ts:100900});
radarView.pendingSource.frames.forEach(decode);
let pumps=0;radarPumpTiles=()=>pumps++;
realQueueTiles();
assert.ok(radarView.pendingSource);assert.equal(pumps,1,'premature accept stopped tile scheduling');
const next=manifest('b');renderRadar({radar:next,ts:100901});
radarView.pendingSource.frames.slice(-4).forEach(f=>{if(!f.bitmap)decode(f)});
realQueueTiles();assert.equal(radarView.pendingSource,null);
'''.replace('COUNT', str(count)))


def test_variant_adopts_newest_without_waiting_for_backfill():
    run_page(r'''
const old=radarView.loaded.slice(),r=manifest();
r.tiles.variant='native';r.tiles.revision='abcdef123456';r.native=true;
r.refresh={state:'history',frameTotal:8};r.tiles.frames=r.tiles.frames.slice(-2);
renderRadar({radar:r,ts:100900});
assert.ok(radarView.pendingSource.variantOnly);
decode(radarView.pendingSource.frames[0]);radarAcceptSource();
assert.ok(radarView.pendingSource,'older decoded plate cannot satisfy renderer handoff');
decode(radarView.pendingSource.frame);radarAcceptSource();
assert.equal(radarView.pendingSource,null);assert.equal(radarView.data.native,true);
assert.ok(old.every(f=>f.bitmap.closes===1));
''')


def test_ledger_retry_backoff_recovers_and_enforces_memory_limits(tmp_path, monkeypatch):
    clock, mono = [1_800_000_000.], [0.]
    ledger = budget.NativeBudget(tmp_path/'bytes.json', lambda: clock[0], lambda: mono[0])
    replace = budget.os.replace
    attempts = []
    def fail(*args):
        attempts.append(mono[0]);raise OSError('temporary read-only filesystem')
    monkeypatch.setattr(budget.os, 'replace', fail)
    ledger.add(10)
    ledger.persist()
    for tick in range(5):
        mono[0] = tick;ledger.add(1);ledger.persist()
    assert attempts == [0]
    assert ledger.snapshot()['ceilingState'] == 'normal'
    assert ledger.snapshot()['ledgerState'] == 'retrying'
    assert budget.native_allowed(True, 'live', ledger.snapshot()['ceilingState'])
    mono[0] = 5;ledger.persist()
    assert attempts == [0, 5]
    ledger.add(budget.NATIVE_PAUSE_BYTES)
    assert ledger.snapshot()['ceilingState'] == 'paused'
    mono[0] = 14;ledger.persist();assert attempts == [0, 5]
    monkeypatch.setattr(budget.os, 'replace', replace)
    mono[0] = 15;ledger.persist()
    assert ledger.snapshot()['ledgerState'] == 'ok'
    assert json.loads(ledger.path.read_text())['bytes'] == budget.NATIVE_PAUSE_BYTES+15
    assert budget.NativeBudget(ledger.path, lambda: clock[0]).snapshot() == ledger.snapshot()


@pytest.mark.parametrize('days,expected', [(1, 999), (2, 0), (10, 0)])
def test_future_ledger_day_has_one_day_tolerance(tmp_path, days, expected):
    now = 1_800_000_000
    day = (datetime.fromtimestamp(now, timezone.utc)+timedelta(days=days)).strftime('%Y-%m-%d')
    path = tmp_path/'bytes.json';path.write_text(json.dumps(dict(day=day, bytes=999)))
    ledger = budget.NativeBudget(path, lambda: now)
    assert ledger.snapshot()['bytesToday'] == expected
    ledger.persist()
    assert budget.NativeBudget(path, lambda: now).snapshot() == ledger.snapshot()


@pytest.mark.parametrize('marker', ['source', 'presence'])
def test_future_lease_anchor_survives_process_restart_and_server_expiry(tmp_path, monkeypatch, marker):
    now = 1_800_000_000
    source = tmp_path/'radar_source';source.write_text('site')
    os.utime(source, (now-3000, now-3000))
    if marker == 'source': os.utime(source, (now+10*86400, now+10*86400))
    else: (tmp_path/'presence').write_text(str(now+10*86400))
    script = 'from lib.radar_auto import source_preference; import sys; print(source_preference(sys.argv[1], now=float(sys.argv[2])))'
    for elapsed, expected in [(0, 'site'), (2600, 'site'), (2700, 'auto'), (10*86400+1, 'auto')]:
        result = subprocess.run(['python', '-c', script, str(tmp_path), str(now+elapsed)], capture_output=True, text=True, check=True)
        assert result.stdout.strip() == expected
    server = _load_serve(monkeypatch, tmp_path, {})
    monkeypatch.setattr(server.time, 'time', lambda: now+2700)
    server._expire_radar_source()
    assert source.read_text().strip() == 'auto'


def test_old_poll_ack_cannot_consume_a_new_source_tap():
    html = Path('design/almanac/console_live.html').read_text()
    poll = html[html.index('  function poll(viewStart)'):html.index('  /* Paint the no-data')]
    script = r'''
const assert=require('node:assert/strict');
let presenceDirty=false,pollTimer=null,pollController=null,polling=false,pollStart=0,FETCH_MS=4000,failCount=0,pollGen=0,reportRender=false;
const schedulePoll=()=>{},updateFreshness=()=>{},$=()=>({classList:{contains:()=>true}}),document={hidden:false},clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
const radarIntent={generation:1,ready:false,owned:true,owner:null,session:'review2-session',heartbeat:0,preferredMode:'mosaic'},radarGesture={state:'idle'},radarZoom={auto:false},radarSmooth={pending:null},radarBaseStyle={theme:'paper'},radarSource={desired:null};
const validPayload=()=>true,radarTrace=()=>{},isNum=v=>typeof v==='number'&&Number.isFinite(v);
let radarCamera={lat:47,lon:-122,zoom:9},requests=[];
const fetch=url=>{const handlers=[],chain={then(fn){handlers.push(fn);return chain},catch(){return chain}};requests.push({url,handlers});return chain};
POLL
function ack(index,generation){try{requests[index].handlers[1]({d:{},ack:{session:radarIntent.session,generation,intent:{source:'mosaic'}},render:'v1',smooth:'off'})}catch(e){if(e.name!=='ReferenceError')throw e}}
poll();
// Source tap occurs before its deferred poll has run.
radarIntent.preferredMode='site';radarIntent.sourceDirty=true;radarIntent.ready=true;radarIntent.generation=2;radarSource.desired='site';
ack(0,1);assert.equal(radarIntent.sourceDirty,true);assert.equal(radarIntent.ready,true);
polling=false;poll();assert.ok(requests[1].url.includes('radarSource=site'));
ack(1,2);assert.equal(radarIntent.sourceDirty,false);assert.equal(radarIntent.ready,false);
// Even a same-generation heartbeat did not send the source change.
polling=false;poll();radarIntent.sourceDirty=true;radarIntent.ready=true;
ack(2,2);assert.equal(radarIntent.sourceDirty,true);assert.equal(radarIntent.ready,true);
process.exit(0);
'''.replace('POLL', poll)
    result = subprocess.run(['node'], input=script, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_variant_caption_names_renderers_and_ledger_failure_names_accounting():
    controls(r'''
Object.assign(radarView.data,{sourceId:'iem-nexrad-n0b',sourceMode:'site',siteId:'KATX',native:false});
radarView.pendingSource={variantOnly:true,data:{native:true,sourceMode:'site'}};
radarSourceRender();assert.match(caption(),/^Restoring NOAA Level III · showing IEM tiles/);assert.ok(!caption().includes('Switching'));
radarView.pendingSource=null;radarView.data.native=true;
radarView.data.nativeBudget={ceilingState:'normal',ledgerState:'retrying'};
radarSourceRender();assert.match(caption(),/accounting retrying/);assert.ok(!caption().includes('daily data limit'));
''')


def test_region_fallback_checkpoint_ignores_displayed_site_policy(make_emitter):
    emitter = make_emitter()
    emitter._radar_result = emitter._radar_result._replace(source_mode='site')
    emitter._radar_native_budget.add(budget.NATIVE_PAUSE_BYTES+1)
    emitter._radar_checkpoint(dict(target_source='iem-mrms-lcref', native_ceiling='normal'))


def test_auto_decision_reuses_listings_within_scan_cadence(make_emitter, hybrid, multisite, tmp_path):
    intent(tmp_path, 9)
    emitter = make_emitter(); emitter._do_radar()
    multisite.calls.clear()
    hybrid.mono += 120
    emitter._do_radar(discovery=True, intent_triggered=False)
    assert not [c for c in multisite.calls if c[0]=='list']
    hybrid.mono += 180
    emitter._do_radar(discovery=True, intent_triggered=False)
    assert len([c for c in multisite.calls if c[0]=='list']) == 3


def test_failed_listing_ages_without_another_transport_attempt():
    evidence = dict(reporting=None, reason='scan unavailable', checkedTs=1200, failedSince=1000)
    assert auto.listing_availability(evidence, 1299, 300, 900) is None
    assert auto.listing_availability(evidence, 1300, 300, 900) is False


@pytest.mark.parametrize('mode', ['mosaic', 'auto'])
def test_quiet_region_refreshes_closest_listing(make_emitter, hybrid, multisite, tmp_path, monkeypatch, mode):
    intent(tmp_path, 6, mode)
    emitter = make_emitter();emitter._do_radar()
    monkeypatch.setattr(ae, 'RADAR_ATTENTION_MODE', 'active')
    emitter._radar_attention.forced = emitter._radar_attention.tier = 'rest'
    multisite.calls.clear()
    emitter._do_radar(discovery=True, intent_triggered=False)
    assert multisite.calls == [('list', 'KNEA')]


def test_region_admission_and_watcher_do_not_read_native_policy(make_emitter, monkeypatch):
    emitter = make_emitter()
    emitter._radar_result = emitter._radar_result._replace(source_mode='mosaic', source_id='iem-mrms-lcref')
    emitter._radar_target_source = 'iem-mrms-lcref'
    emitter._radar_policy_ceiling = 'normal'
    def unexpected(): raise AssertionError('Region evaluated the native ledger')
    monkeypatch.setattr(emitter._radar_native_budget, 'snapshot', unexpected)
    assert emitter._radar_headroom_delay('iem-mrms-lcref', 1) == 0
    assert emitter._radar_transport_sources('iem-mrms-lcref') == ('iem-mrms-lcref',)
    emitter._check_radar_zoom()


def test_auto_region_at_zoom_nine_ignores_unused_level3_breaker(make_emitter, hybrid, multisite, tmp_path, monkeypatch):
    """Opus final review: Auto on Region at zoom >= 8 (coverage too low) with an
    opened Level III breaker kept the probe delay at 0 -> discovery every second.
    Region never contacts Level III, so that breaker cannot clear from there."""
    monkeypatch.setattr(auto, 'coverage_fraction', lambda *a, **k: .5)
    (tmp_path/'radar_source').unlink(missing_ok=True)
    (tmp_path/'radar_intent').write_text(json.dumps(dict(seq=1, zoom=9, source='auto', center='station')))
    e = make_emitter(); e._do_radar()
    assert e._radar_result.source_mode == 'mosaic'
    url = ae.RADAR_LEVEL3_BUCKET + 'x'
    for _ in range(8):
        e._radar_health.record(ae.RADAR_LEVEL3_TRANSPORT, url, False, TimeoutError('t'))
    hybrid.mono += 10000
    for _ in range(3):
        e._do_radar(discovery=True, intent_triggered=False)
    assert e._radar_result.source_mode == 'mosaic'
    delay = e._radar_probe_delay()
    assert delay is None or delay > 1
