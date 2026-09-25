"""Second v2-only review regressions. Passes run through the real scheduler
(FakeClock timers, inline worker threads) with local fixtures only."""
import json
import urllib.error
from types import SimpleNamespace

import pytest

from lib import almanac_emit as ae
from tests.test_emitter_lifecycle import FakeClock, InlineThread
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401
from tests.test_radar_level3 import native  # noqa: F401
from tests.test_radar_v2_only_review import tier, production_function
from tests.test_radar_auto import intent
from tests.test_radar_auto_page import controls

SITE = 'iem-nexrad-n0b'


class SyncedClock(FakeClock):
    """Kivy's clock and the emitter's time.monotonic/time.time share one
    timeline: every timer fires with the fixture clock at its due time."""

    def __init__(self, state):
        super().__init__()
        self.state = state
        self.fired = []

    def advance(self, seconds):
        target = self.now + seconds
        while True:
            due = [event for event in self.events if event.due <= target]
            if not due:
                break
            event = min(due, key=lambda e: e.due)
            self.now = self.state.mono = event.due
            if event.interval:
                event.due = self.now + event.timeout
            else:
                event.cancel()
            self.fired.append((self.now, getattr(event.callback, '__qualname__', '')))
            if event.callback(0) is False:
                event.cancel()
        self.now = self.state.mono = target


@pytest.fixture
def scheduled(hybrid, monkeypatch):
    clock = SyncedClock(hybrid)
    monkeypatch.setattr(ae, 'Clock', clock)
    monkeypatch.setattr(ae, 'threading', SimpleNamespace(Thread=InlineThread))
    return clock


def run(emitter, clock):
    """Start the emitter's radar lane the way start() does: the 100 ms intent
    watcher plus the first pass, then leave every later pass to the timers."""
    emitter._running = True
    emitter._radar_start_inventory()
    assert emitter._radar_cache_ready.wait(30)  # production scans 60 s before the first pass
    emitter._radar_zoom_stamp = emitter._radar_preference_stamp()
    emitter._schedule(emitter._check_radar_zoom, ae.RADAR_INTENT_CHECK_SEC, interval=True)
    emitter._check_radar()
    assert emitter._radar_result.frames, 'the first scheduled pass drew'
    return emitter


def cooling(emitter):
    return emitter._radar_cooldowns.get(ae.RADAR_LEVEL3_TRANSPORT, 0) > ae.time.monotonic()


def rate_limit_level3(monkeypatch, retry_after='3600'):
    opened = ae.RadarSession.open
    def limited(session, request, timeout):
        if request.full_url.startswith(ae.RADAR_LEVEL3_BUCKET):
            raise urllib.error.HTTPError(request.full_url, 429, 'limited', {'Retry-After': retry_after}, None)
        return opened(session, request, timeout)
    monkeypatch.setattr(ae.RadarSession, 'open', limited)
    return opened


# 1. A Level III 429 inside a real pass: IEM draws the newest scan within seconds.

def test_level3_429_in_a_scheduled_pass_draws_iem_promptly(
        make_emitter, hybrid, multisite, native, monkeypatch, scheduled):
    emitter = run(make_emitter(), scheduled)
    assert emitter._radar_result.tiles['variant'] == 'native'
    opened = rate_limit_level3(monkeypatch)
    newest = hybrid.latest + 300
    for site in ('KNEA', 'KMID'):
        multisite.scans[site].append(newest)
    # Discovery finds the new scan on its own schedule; Level III answers 429.
    scheduled.advance(emitter._radar_discovery.due - ae.time.time() + .5)
    assert cooling(emitter), 'the 429 happened inside a scheduled pass'
    assert emitter._radar_pass['outcome'] == 'failed', 'handled as a Level III outage, not a local yield'
    assert emitter._radar_retry_reason == 'provider'
    # No schedule waits on the Level III cooldown while IEM can draw.
    assert emitter._radar_next_retry - ae.time.time() <= 2
    assert emitter._radar_discovery.due - ae.time.time() < ae.RADAR_LEVEL3_COOLDOWN_MAX_SEC
    assert emitter._radar_probe_delay() is None
    scheduled.advance(2)  # the v1 retry
    wire = emitter._build_payload()['radar']
    assert emitter._radar_result.ts_frame == newest
    assert wire['tiles']['variant'] is False
    assert wire['nativeFallback'] == dict(active=True, reason='level3-unreachable', recovering=False)
    assert [c for c in multisite.calls if c[0] == 'tile'], 'v1 fetched IEM ridge tiles'
    assert cooling(emitter)
    # Recovery: the cooldown ends, the outage wake restarts v2.
    monkeypatch.setattr(ae.RadarSession, 'open', opened)
    scheduled.advance(ae.RADAR_LEVEL3_COOLDOWN_MAX_SEC)
    assert emitter._build_payload()['radar']['tiles']['variant'] == 'native'
    emitter.stop()


# 2. A NOAA S3 stall during watch: lag runs from the first unpublished scan.

def test_watch_level3_stall_warns_and_draws_labelled_iem_until_publication_resumes(
        make_emitter, hybrid, multisite, native, tmp_path, monkeypatch, scheduled):
    warnings = []
    monkeypatch.setattr(ae.Logger, 'warning', warnings.append)
    emitter = make_emitter()
    tier(emitter, tmp_path, 'watch')
    run(emitter, scheduled)
    assert emitter._radar_result.tiles['variant'] == 'native'
    base = hybrid.latest
    ages = []
    for minute in range(1, 41):
        if minute % 5 == 0:  # IEM keeps advertising scans that S3 never publishes
            for site in ('KNEA', 'KMID'):
                multisite.scans[site].append(base + minute*60)
            native.missing.add(base + minute*60)
        scheduled.advance(60)
        ages.append(ae.time.time() - emitter._radar_result.ts_frame)
    stalled = [w for w in warnings if 'Level III stalled' in w]
    assert stalled, 'a stalled feed is logged'
    assert len(stalled) <= 40*60 // ae.RADAR_FAILURE_LOG_SEC + 1, 'rate limited'
    wire = emitter._build_payload()['radar']
    assert wire['sourceMode'] == 'site' and wire['tiles']['variant'] is False
    assert wire['nativeFallback'] == dict(active=True, reason='level3-stalled', recovering=False)
    assert 'not published' in wire['health']['nativeFallback']['reason']
    assert max(ages[15:]) < ae.RADAR_LEVEL3_UNPUBLISHED_LOG_SEC + 120, 'IEM keeps the frame current'
    assert not emitter._radar_transport_failures, 'a Level III stall never strikes IEM'
    # Publication resumes: the next Level III check draws v2 and ends the stall.
    native.missing.clear()
    for site in ('KNEA', 'KMID'):
        multisite.scans[site].append(base + 45*60)
    scheduled.advance(5*60)
    wire = emitter._build_payload()['radar']
    assert wire['tiles']['variant'] == 'native' and emitter._radar_result.ts_frame == base + 45*60
    assert wire['nativeFallback'] == dict(active=False, reason=None, recovering=False)
    assert emitter._radar_level3_stall is None
    emitter.stop()


# 3. Watch's primary is the first REPORTING in-view radar, as in live.

def test_watch_skips_a_dark_nearest_radar_like_live(
        make_emitter, hybrid, multisite, native, tmp_path, monkeypatch, scheduled):
    warnings = []
    monkeypatch.setattr(ae.Logger, 'warning', warnings.append)
    multisite.scans['KNEA'] = [hybrid.now - 1200]  # the nearest radar went dark
    emitter = make_emitter()
    tier(emitter, tmp_path, 'watch')
    run(emitter, scheduled)
    multisite.calls.clear()
    for minute in range(1, 21):
        if minute % 5 == 0:
            multisite.scans['KMID'].append(hybrid.latest + 60 + minute*60)
        scheduled.advance(60)
    wire = emitter._build_payload()['radar']
    assert wire['sourceMode'] == 'site' and wire['siteId'] == 'KMID'
    assert wire['tiles']['variant'] == 'native'
    assert emitter._radar_result.ts_frame == hybrid.latest + 60 + 20*60
    assert not warnings and not emitter._radar_transport_failures
    assert all(f.get('primaryOnly') for f in emitter._radar_result.frames[-1:])
    # Only the new primary's scans are downloaded; KFAR (farther) is never listed.
    assert all(key.startswith('MID_') for kind, key in native.calls if kind == 'get')
    assert ('list', 'KFAR') not in multisite.calls
    # Fresh "not reporting" evidence spares the dark site a listing per pass.
    listed = [c for c in multisite.calls if c == ('list', 'KNEA')]
    assert 0 < len(listed) <= 20*60 // 300 + 1
    emitter.stop()


# 4. Watch holds only a mode Auto chose, never a Region the chain forced.

def weather_goes_on(hybrid, multisite, clock, minutes):
    """Every provider publishes a new scan each five minutes."""
    for _ in range(minutes // 5):
        hybrid.latest += 300
        hybrid.rv += 300
        for site in ('KNEA', 'KMID'):
            multisite.scans[site].append(hybrid.latest)
        clock.advance(300)


def test_auto_watch_reevaluates_site_after_chain_forced_region(
        make_emitter, hybrid, multisite, native, tmp_path, monkeypatch, scheduled):
    intent(tmp_path, 8, 'auto')
    emitter = make_emitter()
    run(emitter, scheduled)
    assert emitter._radar_result.source_mode == 'site'
    tier(emitter, tmp_path, 'watch')
    opened = ae.RadarSession.open
    def listing_down(session, request, timeout):
        if 'operation=list' in request.full_url:
            raise urllib.error.HTTPError(request.full_url, 503, 'unavailable', {}, None)
        return opened(session, request, timeout)
    monkeypatch.setattr(ae.RadarSession, 'open', listing_down)
    weather_goes_on(hybrid, multisite, scheduled, 5)
    assert emitter._radar_result.source_mode == 'mosaic', 'Site strikes forced Region'
    # The site listing recovers while nobody is watching.
    monkeypatch.setattr(ae.RadarSession, 'open', opened)
    multisite.calls.clear()
    weather_goes_on(hybrid, multisite, scheduled, 15)
    wire = emitter._build_payload()['radar']
    assert ('list', 'KNEA') in multisite.calls
    assert wire['sourceMode'] == 'site' and wire['siteId'] == 'KNEA'
    assert emitter._radar_result.ts_frame == hybrid.latest
    emitter.stop()



# 5. The fallback reason shows once (caption); the note keeps loop state only.

@pytest.mark.parametrize('case', ['stalled', 'degrading', 'restoring', 'smooth'])
def test_fallback_reason_appears_once_and_renderer_changes_use_neutral_note(case):
    controls(production_function('radarNoteRender')+r'''
radarIntent.postedAt=Date.now()-60000;radarView.refresh={state:'idle'};
Object.assign(radarView.data,{sourceId:'iem-nexrad-n0b',sourceMode:'site',siteId:'KATX',native:false,
  nativeFallback:{active:true,reason:'level3-stalled',recovering:false},tiles:{smooth:false}});
const TABLE={
  stalled:[null,'NOAA Level III delayed · showing IEM tiles',''],
  degrading:[{variantOnly:true,frames:[],data:{native:false,nativeFallback:{active:true,reason:'level3-unreachable'}}},
    'Level III unreachable · loading IEM tiles · showing NOAA Level III','Playing previous view · updating newest frame'],
  restoring:[{variantOnly:true,frames:[],data:{native:true,nativeFallback:{active:false,reason:null}}},
    'Restoring NOAA Level III · showing IEM tiles','Playing previous view · updating newest frame'],
  smooth:[{variantOnly:true,frames:[],data:{native:false,tiles:{smooth:true}}},
    null,'Playing previous view · updating newest frame']};
const [pending,text,note]=TABLE[CASE];
if(CASE==='degrading')Object.assign(radarView.data,{native:true,nativeFallback:{active:false,reason:null}});
if(CASE==='smooth')Object.assign(radarView.data,{nativeFallback:{active:false,reason:null}});
radarView.pendingSource=pending;
radarSourceRender();radarNoteRender();
const shown=caption()+' | '+$('rad-note').textContent;
if(text){assert.ok(caption().startsWith(text+' · '),caption());assert.equal(shown.split(text).length-1,1,shown);}
assert.equal($('rad-note').textContent,note);
assert.doesNotMatch(shown,/sharpening/i);
assert.doesNotMatch($('rad-note').textContent,/Level III|IEM tiles|delayed|unreachable/);
'''.replace('CASE', json.dumps(case)))


def test_an_abandoned_stall_streak_cannot_fire_hours_later(make_emitter, hybrid, multisite, native, monkeypatch, tmp_path):
    """Opus review (probe_j): a routine delay starts a streak, the tier drops
    before the late scan is fetched, and hours later the next routine 90 s
    delay was measured from the abandoned streak: a false 'Level III delayed'
    fallback. A streak not observed for the bound starts over."""
    from tests.test_radar_v2_only_review import tier
    msgs = []
    monkeypatch.setattr(ae.Logger, 'warning', msgs.append)
    e = make_emitter(); tier(e, tmp_path, 'watch')
    e._do_radar()
    T = hybrid.latest + 300
    for s in ('KNEA', 'KMID'):
        multisite.scans[s].append(T)
    native.missing.add(T)
    hybrid.mono = T + 90 - (hybrid.latest + 360)
    e._do_radar(discovery=True, intent_triggered=False)
    assert e._radar_level3_stall is not None                 # a routine lag starts a streak
    native.missing.clear()
    tier(e, tmp_path, 'rest')
    hybrid.mono += 7200                                      # two hours away from watch
    X = T + 7200
    multisite.scans['KNEA'] = [X - 300, X]; multisite.scans['KMID'] = [X - 300, X]
    native.missing.add(X)
    hybrid.mono = X + 90 - (hybrid.latest + 360)
    tier(e, tmp_path, 'watch')
    hybrid.latest = X - 120
    import os
    os.utime(tmp_path / 'radar_source', (ae.time.time(), ae.time.time()))
    e._do_radar(discovery=True, intent_triggered=False)
    r = e._build_payload()['radar']
    assert e._radar_pass.get('outcome') == 'unpublished'     # an ordinary lag, not a stall
    assert not (r.get('nativeFallback') or {}).get('active')
    assert e._radar_level3_stall['since'] == X
    assert not any('stalled' in m for m in msgs)
