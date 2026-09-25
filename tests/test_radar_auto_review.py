"""Review regressions: production engine/server/page paths, simulated transport only."""
import io
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

from lib import almanac_emit as ae, radar_auto as auto, radar_native_budget as budget
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401
from tests.test_radar_level3 import native  # noqa: F401
from tests.test_radar_auto import intent
from tests.test_freshness_health import _load_serve
from tests.test_radar_buffer_page import run_page


@pytest.mark.parametrize('mode,zoom,coverage', [('mosaic', 9, 1.), ('auto', 6, 1.), ('auto', 9, .5)])
@pytest.mark.parametrize('tier', ['live', 'warm'])
def test_region_loop_ignores_native_soft_ceiling(make_emitter, hybrid, multisite, native, tmp_path, monkeypatch, mode, zoom, coverage, tier):
    intent(tmp_path, zoom, mode)
    monkeypatch.setattr(auto, 'coverage_fraction', lambda *args: coverage)
    counts = []
    for amount in (0, budget.NATIVE_NEWEST_ONLY_BYTES+1):
        emitter = make_emitter()
        emitter._radar_attention.forced = emitter._radar_attention.tier = tier
        emitter._radar_native_budget.add(amount)
        emitter._do_radar()
        snap = emitter._radar_result
        assert snap.source_mode == 'mosaic'
        counts.append(sum(f['complete'] for f in snap.frames))
    assert counts[0] > 1 and counts[0] == counts[1]


@pytest.mark.parametrize('failure', ['listing', 'nearest_listing', 'timeout', 'budget'])
def test_unknown_discovery_keeps_site_and_evidence(make_emitter, hybrid, multisite, tmp_path, monkeypatch, failure):
    intent(tmp_path, 9)
    emitter = make_emitter(); emitter._do_radar()
    assert emitter._radar_result.source_mode == 'site'
    status = dict(emitter._radar_site_status['KNEA'])
    evidence = dict(emitter._radar_auto_evidence)
    if failure in ('listing', 'nearest_listing'):
        opened = ae.RadarSession.open
        def fail(session, req, timeout):
            if 'operation=list' in req.full_url and (failure == 'listing' or 'radar=NEA' in req.full_url):
                raise OSError('transport unavailable')
            return opened(session, req, timeout)
        monkeypatch.setattr(ae.RadarSession, 'open', fail)
    else:
        def fail(ctx):
            raise TimeoutError('deadline') if failure == 'timeout' else ae._RadarBudget('budget')
        monkeypatch.setattr(emitter, '_radar_site_discover', fail)
    for elapsed in (2, 12):
        hybrid.mono = elapsed
        emitter._radar_forget('iem-nexrad-n0b')
        emitter._do_radar(discovery=True, intent_triggered=False)
        assert emitter._radar_result.source_mode == 'site'
        if failure in ('listing', 'nearest_listing'):
            assert emitter._radar_site_status['KNEA']['reporting'] is None
        else:
            assert emitter._radar_site_status['KNEA'] == status
        assert emitter._radar_auto_evidence == (evidence if elapsed < ae.RADAR_SITE_MAX_AGE_SEC else {})


@pytest.mark.parametrize('coverage,expected', [(.85, 'site'), (.70, 'site'), (.699, 'mosaic')])
def test_site_coverage_hysteresis_and_guard(coverage, expected):
    assert auto.choose(9, 'site', True, coverage, 11) == expected
    assert auto.choose(9, 'site', True, coverage, 2) == 'site'
    assert auto.choose(9, 'mosaic', True, coverage, 11) == ('site' if coverage >= .85 else 'mosaic')
    assert auto.choose(9, 'site', None, 0, 11) == 'site'
    assert auto.choose(9, 'site', False, 1, 2) == 'mosaic'


def test_zoom_seven_uses_zoom_eight_footprint(make_emitter, hybrid, multisite, tmp_path, monkeypatch):
    multisite.scans['KMID'] = []  # genuine single-site Seattle footprint
    seen = []
    original = auto.coverage_fraction
    def measure(bounds, sites, radius):
        seen.append((dict(bounds), original(bounds, sites, radius)))
        return seen[-1][1]
    monkeypatch.setattr(auto, 'coverage_fraction', measure)
    intent(tmp_path, 8)
    emitter = make_emitter(); emitter._do_radar()
    assert emitter._radar_result.source_mode == 'site'
    intent(tmp_path, 7, seq=2); hybrid.mono += 11
    emitter._do_radar()
    assert emitter._radar_result.source_mode == 'site'
    assert seen[0] == seen[-1] and seen[-1][1] >= .85


def test_ledger_durable_forward_days_and_write_rate(tmp_path, monkeypatch):
    clock, mono = [1_800_000_000.], [0.]
    target = tmp_path/'durable'; target.write_text('{}')
    path = tmp_path/'radar_native_bytes.json'; path.symlink_to(target)
    writes = []
    replace = budget.os.replace
    monkeypatch.setattr(budget.os, 'replace', lambda *args: (writes.append(args), replace(*args))[-1])
    ledger = budget.NativeBudget(path, lambda: clock[0], lambda: mono[0])
    ledger.add(10)
    ledger.persist()
    saved = ledger.snapshot()
    clock[0] -= 86400  # boot before NTP
    assert budget.NativeBudget(path, lambda: clock[0]).snapshot() == saved
    for _ in range(100): ledger.add(1)
    assert len(writes) == 1 and ledger.snapshot()['bytesToday'] == 110
    mono[0] = 60; ledger.persist()
    assert len(writes) == 2
    ledger.add(budget.NATIVE_NEWEST_ONLY_BYTES)
    ledger.persist()
    ledger.add(budget.NATIVE_PAUSE_BYTES)
    ledger.persist()
    assert len(writes) == 4  # each crossing persists immediately
    clock[0] += 2*86400; ledger.persist()
    assert len(writes) == 5 and ledger.snapshot()['bytesToday'] == 0
    assert path.is_symlink() and not list(tmp_path.glob('*.tmp'))
    script = Path('design/almanac/kiosk/almanac-kiosk.sh').read_text()
    assert 'ln -sfn "$RADAR_STATE/radar_native_bytes.json" "$DATA_DIR/radar_native_bytes.json"' in script


@pytest.mark.parametrize('invalid', [False, True])
def test_accounting_failure_preserves_transport_result_and_metrics(make_emitter, monkeypatch, caplog, invalid):
    emitter = make_emitter(); emitter._radar_begin_log_pass(); emitter._radar_session = ae.RadarSession()
    monkeypatch.setattr(emitter._radar_session, 'open', lambda *args, **kwargs: io.BytesIO(b'body'))
    def fail(*args): raise OSError('SD read only')
    monkeypatch.setattr(budget.os, 'replace', fail)
    def validate(raw):
        if invalid: raise ValueError('actual validation error')
    def request():
        return emitter._radar_request(ae.RADAR_LEVEL3_TRANSPORT, ae.RADAR_LEVEL3_BUCKET+'object', ae.time.monotonic()+10, validate=validate)
    if invalid:
        with pytest.raises(ValueError, match='actual validation error'): request()
    else:
        assert request() == b'body'
    assert emitter._radar_request_metrics[-1]['bytes'] == 4
    emitter._radar_native_budget.persist()
    assert emitter._radar_native_budget.snapshot()['ledgerState'] == 'retrying'
    emitter._radar_native_budget.add(3)
    assert emitter._radar_native_budget.snapshot()['bytesToday'] == 7
    assert len([r for r in caplog.records if 'ledger unavailable' in r.message]) == 1


def test_sd_write_holds_neither_renderer_nor_accounting_lock(make_emitter, monkeypatch):
    emitter = make_emitter(); emitter._radar_begin_log_pass(); emitter._radar_session = ae.RadarSession()
    monkeypatch.setattr(emitter._radar_session, 'open', lambda *args, **kwargs: io.BytesIO(b'body'))
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    def fsync(fd):
        entered.set(); assert release.wait(5)
    monkeypatch.setattr(budget.os, 'fsync', fsync)
    def request():
        emitter._radar_request(ae.RADAR_LEVEL3_TRANSPORT, ae.RADAR_LEVEL3_BUCKET+'object', ae.time.monotonic()+10)
    worker = threading.Thread(target=request); worker.start()
    assert entered.wait(5)
    def checkpoint():
        with emitter._radar_lock:
            assert emitter._radar_native_budget.snapshot()['bytesToday'] == 4
        completed.set()
    reader = threading.Thread(target=checkpoint); reader.start()
    try: assert completed.wait(2)
    finally:
        release.set(); worker.join(5); reader.join(5)


@pytest.mark.parametrize('missing_count', [1, 2, 3])
def test_newest_only_tries_two_fallbacks_builds_one_frame(make_emitter, hybrid, multisite, native, missing_count):
    scans = [hybrid.latest-i*120 for i in range(4)]
    multisite.scans['KNEA'] = sorted(scans)
    multisite.scans['KMID'] = []
    native.missing.update(scans[:missing_count])
    emitter = make_emitter(); emitter._radar_native_budget.add(budget.NATIVE_NEWEST_ONLY_BYTES+1)
    emitter._do_radar()
    if missing_count < 3:
        assert emitter._radar_result.source_mode == 'site'
        assert len(emitter._radar_result.frames) == 1
        assert emitter._radar_result.ts_frame == scans[missing_count]
        assert emitter._radar_refresh['frameTotal'] == 1
    else:
        assert not emitter._radar_result.frames
    assert len([c for c in native.calls if c[0] == 'get']) == (1 if missing_count < 3 else 0)


def test_region_checkpoint_and_wake_ignore_site_only_policy(make_emitter, monkeypatch):
    monkeypatch.setattr(ae, 'RADAR_ATTENTION_MODE', 'active')
    emitter = make_emitter(); emitter._radar_native_requested = True
    emitter._radar_result = emitter._radar_result._replace(source_mode='mosaic', source_id='iem-mrms-lcref')
    emitter._radar_target_source = 'iem-mrms-lcref'
    before = dict(tier='watch', frames=4, tiles=True, prefetch=False, listing=120)
    after = dict(before, tier='warm')
    monkeypatch.setattr(emitter, '_radar_attention_knobs', lambda: after)
    wakes = []
    monkeypatch.setattr(emitter, '_schedule', lambda *args: wakes.append(args))
    monkeypatch.setattr(emitter, '_radar_arm_discovery', lambda **kwargs: None)
    emitter._radar_attention_changed(before, ae.time.time())
    assert not wakes
    emitter._radar_native_budget.add(budget.NATIVE_NEWEST_ONLY_BYTES+1)
    ctx = dict(native=True, native_ceiling='normal', target_source='iem-mrms-lcref', attention_knobs=before)
    emitter._radar_checkpoint(ctx)
    ctx['target_source'] = 'iem-nexrad-n0b'
    with pytest.raises(ae._RadarSuperseded): emitter._radar_checkpoint(ctx)


def test_camera_only_commit_preserves_expired_source_and_lease(tmp_path, monkeypatch):
    server = _load_serve(monkeypatch, tmp_path, {})
    now = 1_800_000_000
    monkeypatch.setattr(server.time, 'time', lambda: now)
    (tmp_path/'radar_source').write_text('site')
    os.utime(tmp_path/'radar_source', (now-3000, now-3000))
    record = intent(tmp_path, 9, 'site'); record['acceptedAt'] = now-3000
    (tmp_path/'radar_intent').write_text(json.dumps(record))
    server._expire_radar_source()
    stamp = (tmp_path/'radar_source').stat().st_mtime_ns
    params = dict(radarSession=['review-session-12345'], radarGeneration=['1'], radarHeartbeat=['1'], radarClaim=[''], radarClaimEpoch=['0'], radarCommit=['1'], radarPolicy=['manual'])
    activity = dict(moving=False, zoom=9, center=dict(lat=47, lon=-122))
    assert server._camera_transaction(activity, params)
    params.update(radarGeneration=['2'], radarHeartbeat=['2'], radarCommit=['1'], radarPolicy=['manual'])
    assert server._camera_transaction(activity, params)
    server._camera_persist_timer.cancel(); server._camera_persist_timer.function()
    accepted = server._read_radar_intent()
    assert accepted['source'] == 'auto' and accepted['sourceAcceptedAt'] == now-3000
    assert (tmp_path/'radar_source').stat().st_mtime_ns == stamp


@pytest.mark.parametrize('future', ['presence', 'mtime'])
def test_future_lease_clock_is_clamped(tmp_path, monkeypatch, future):
    now = 1_800_000_000
    (tmp_path/'radar_source').write_text('site')
    os.utime(tmp_path/'radar_source', (now-3000, now-3000))
    if future == 'presence': (tmp_path/'presence').write_text(str(now+86400))
    else: os.utime(tmp_path/'radar_source', (now+86400, now+86400))
    assert auto.source_preference(tmp_path, now=now) == 'site'
    assert auto.source_preference(tmp_path, now=now+auto.MANUAL_HOLD_SEC-1) == 'site'
    assert auto.source_preference(tmp_path, now=now+auto.MANUAL_HOLD_SEC) == 'auto'


@pytest.mark.parametrize('tier', ['watch', 'rest', 'dormant'])
def test_shadow_tier_cannot_gate_auto_or_native(make_emitter, hybrid, multisite, native, tmp_path, monkeypatch, tier):
    monkeypatch.setattr(ae, 'RADAR_ATTENTION_MODE', 'shadow')
    intent(tmp_path, 9)
    emitter = make_emitter(); emitter._radar_attention.forced = emitter._radar_attention.tier = tier
    emitter._do_radar()
    assert emitter._radar_result.source_mode == 'site'
    assert emitter._radar_result.tiles['variant'] == 'native' and native.calls
    assert ae.RADAR_LEVEL3_TRANSPORT in emitter._radar_transport_sources('iem-nexrad-n0b')


def test_auto_schedules_site_breaker_recovery(make_emitter, monkeypatch):
    emitter = make_emitter()
    emitter._radar_result = emitter._radar_result._replace(source_pref='auto', source_id='iem-mrms-lcref', source_mode='mosaic', zoom_desired=8)
    seen = []
    monkeypatch.setattr(emitter._radar_health, 'probe_delay', lambda sources: seen.append(sources) or 12)
    assert emitter._radar_probe_delay() == 12
    assert 'iem-nexrad-n0b' in seen[0]


@pytest.mark.parametrize('count', [1, 3, 8])
def test_variant_change_adopts_decoded_newest(count):
    run_page(r'''
const old=radarView.loaded.slice(),r=manifest();
r.tiles.variant='native';r.tiles.revision='abcdef123456';r.native=true;
r.frameCount=COUNT;r.refresh={state:'history',frameTotal:COUNT};
r.tiles.frames=r.tiles.frames.slice(-1);
renderRadar({radar:r,ts:100900});
assert.ok(radarView.pendingSource);decode(radarView.pendingSource.frames[0]);radarAcceptSource();
assert.ok(old.every(f=>f.bitmap.closes===1));
assert.equal(radarView.pendingSource,null);assert.equal(radarView.data.tiles.variant,'native');
assert.equal(radarReady().length,1);
'''.replace('COUNT', str(count)))


def test_camera_posts_source_only_for_explicit_change():
    html = Path('design/almanac/console_live.html').read_text()
    poll = html[html.index('  function poll(viewStart)'):html.index('  /* Paint the no-data')]
    script = r'''
const assert=require('node:assert/strict');
let presenceDirty=false,pollTimer=null,pollController=null,polling=false,pollStart=0,FETCH_MS=4000,failCount=0,pollGen=0,reportRender=false;
const schedulePoll=()=>{},updateFreshness=()=>{},$=()=>({classList:{contains:()=>true}}),document={hidden:false},clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
const radarIntent={generation:1,ready:true,owned:true,owner:null,session:'review-session-12345',heartbeat:0,preferredMode:'site'},radarGesture={state:'idle'},radarZoom={auto:false},radarSmooth={pending:null},radarBaseStyle={theme:'paper'};
let radarCamera={lat:47,lon:-122,zoom:9},urls=[];
const fetch=url=>{urls.push(url);const chain={then:()=>chain,catch:()=>chain};return chain};
POLL
poll();assert.ok(urls[0].includes('radarCommit=1'));assert.ok(!urls[0].includes('radarSource='));
polling=false;radarIntent.sourceDirty=true;poll();assert.ok(urls[1].includes('radarSource=site'));
process.exit(0);
'''.replace('POLL', poll)
    result = subprocess.run(['node'], input=script, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_confirmed_dark_site_can_leave_during_guard(make_emitter, hybrid, multisite, tmp_path):
    intent(tmp_path, 6)
    emitter = make_emitter(); emitter._do_radar()
    intent(tmp_path, 9, seq=2); emitter._do_radar()
    assert emitter._radar_result.source_mode == 'site'
    hybrid.mono += 2; multisite.scans['KNEA'] = []
    emitter._radar_forget('iem-nexrad-n0b')
    emitter._do_radar(discovery=True, intent_triggered=False)
    assert emitter._radar_result.source_mode == 'mosaic'


def test_coverage_exit_waits_for_guard_in_real_engine(make_emitter, hybrid, multisite, tmp_path, monkeypatch):
    intent(tmp_path, 6)
    coverage = [1.]
    monkeypatch.setattr(auto, 'coverage_fraction', lambda *args: coverage[0])
    emitter = make_emitter(); emitter._do_radar()
    intent(tmp_path, 9, seq=2); emitter._do_radar()
    assert emitter._radar_result.source_mode == 'site'
    coverage[0] = .69; hybrid.mono += 2
    emitter._radar_coverage_cache.clear()  # injected geometry changed
    emitter._do_radar(discovery=True, intent_triggered=False)
    assert emitter._radar_result.source_mode == 'site' and emitter._radar_auto_due == 10
    hybrid.mono += 10
    emitter._do_radar(discovery=True, intent_triggered=False)
    assert emitter._radar_result.source_mode == 'mosaic'


def test_page_source_click_marks_explicit_source_commit():
    run_page(r'''
radarView.data.sourcePref='site';radarIntent.preferredMode='site';
radarPostIntent=()=>{};radarChooseSource('auto');
assert.equal(radarIntent.sourceDirty,true);assert.equal(radarIntent.preferredMode,'auto');
''')


def test_launcher_migrates_ledger_and_preserves_it_on_restart(tmp_path):
    runtime = tmp_path/'runtime'; runtime.mkdir()
    (runtime/'radar_native_bytes.json').write_text('{"day":"2026-09-25","bytes":123}')
    script = Path('design/almanac/kiosk/almanac-kiosk.sh').read_text()
    setup = script[script.index('RADAR_STATE='):script.index('cp -f "$APP/design/almanac/console_live.html"')]
    env = dict(os.environ, XDG_STATE_HOME=str(tmp_path/'state'), DATA_DIR=str(runtime))
    for _ in range(2):
        subprocess.run(['bash', '-c', setup], env=env, check=True, capture_output=True, text=True)
        marker = runtime/'radar_native_bytes.json'
        assert marker.is_symlink() and marker.resolve() == tmp_path/'state/wfpiconsole/radar_native_bytes.json'
        assert json.loads(marker.read_text())['bytes'] == 123


def test_region_prefetch_uses_actual_site_target_for_ceiling(make_emitter, hybrid, multisite, native, tmp_path, monkeypatch):
    (tmp_path/'radar_source').write_text('mosaic')
    emitter = make_emitter(); emitter._do_radar()
    source, ctx = emitter._radar_idle_context
    assert source == 'iem-mrms-lcref'
    emitter._radar_native_budget.add(budget.NATIVE_NEWEST_ONLY_BYTES+1)
    monkeypatch.setattr(emitter, '_radar_headroom_delay', lambda *args: 0)
    # The Region pass is unaffected, but warming native Site must see its own
    # target when checking policy at the very first prefetch boundary.
    emitter._radar_checkpoint(dict(ctx, target_source=source))
    with pytest.raises(ae._RadarSuperseded, match='native daily budget'):
        emitter._radar_prefetch(source, dict(ctx, viewed=True, refresh=dict(state='idle')))
