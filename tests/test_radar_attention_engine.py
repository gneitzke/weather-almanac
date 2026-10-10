"""The engine applies the attention tiers: what a pass fetches, how often
discovery wakes, what the payload and health publish."""
import json
import time
import pytest
from lib import almanac_emit as ae
from lib import radar_engine
from tests.test_radar_hybrid import hybrid  # noqa: F401


@pytest.fixture
def active(monkeypatch):
    monkeypatch.setattr(radar_engine, 'RADAR_ATTENTION_MODE', 'active')


def tier(e, name, hour=14.0):
    e.radar._attention.tier = name
    e.radar._attention.since = ae.time.time()
    e.radar._local_hour = hour
    # a fresh emitter's first pass counts as user intent (preference stamp
    # changed); sync it so the pass under test is an ordinary scheduled one
    e.radar._zoom_stamp = e.radar._preference_stamp()


def tile_requests(calls):
    return [c for c in calls if '/mrms::lcref-' in c[2] or '/ridge/' in c[2] or '/256/' in c[2]]


def listing_requests(calls):
    return [c for c in calls if 'operation=list' in c[2] or c[2] == radar_engine.RADAR_IEM_METADATA_URL]


def test_rest_pass_is_listings_only_and_discovery_waits_the_floor(make_emitter, hybrid, active):
    e = make_emitter(); e._runtime.running = True
    tier(e, 'rest')
    e.radar._sentinel = dict(at=ae.time.time(), echo=False, pixels=0, stamp=0)   # sentinel not due
    e.radar._acquire()
    assert e.radar._pass['outcome'] == 'quiet'
    assert not tile_requests(hybrid.calls), hybrid.calls
    assert listing_requests(hybrid.calls)
    assert e.radar._discovery_floor_until - ae.time.time() >= 900 - 1   # the wakeup, not the schedule's own due
    assert not e.radar._result.available            # nothing was acquired, honestly


def test_dormant_pass_waits_an_hour_and_never_runs_the_sentinel(make_emitter, hybrid, active):
    e = make_emitter(); e._runtime.running = True
    tier(e, 'dormant', hour=2.0)
    e.radar._acquire()
    assert e.radar._pass['outcome'] == 'quiet' and e.radar._sentinel is None
    assert e.radar._discovery_floor_until - ae.time.time() >= 3600 - 1


def test_rest_sentinel_fetches_four_zoom5_tiles_and_records_echo(make_emitter, hybrid, active):
    e = make_emitter(); e._runtime.running = True
    tier(e, 'rest')
    hybrid.metadata = None
    e.radar._sentinel = None
    e.radar._acquire()
    tiles = [c for c in hybrid.calls if '/mrms::lcref-' in c[2]]
    assert len(tiles) == 4 and all(f'/{radar_engine.RADAR_SENTINEL_ZOOM}/' in c[2] for c in tiles), tiles
    assert e.radar._sentinel and e.radar._sentinel['echo'] is True    # the fixture tile is solid green
    hybrid.calls.clear(); hybrid.mono += 600
    e.radar._acquire()                                                       # not due again for an hour
    assert not [c for c in hybrid.calls if '/mrms::lcref-' in c[2]]


def test_warm_keeps_four_frames_watch_eight_by_day_one_by_night(make_emitter, hybrid, active):
    seen = {}
    for name, hour, expect in (('warm', 14.0, 4), ('watch', 14.0, 8), ('watch', 2.0, 1), ('live', 2.0, 8)):
        e = make_emitter(); e._runtime.running = True
        tier(e, name, hour)
        captured = {}
        original = e.radar._history
        def spy(source, newest, advertised, ctx, *a, _c=captured, _o=original, **k):
            _c['target'] = ctx.get('frames_target'); return _o(source, newest, advertised, ctx, *a, **k)
        e.radar._history = spy
        e.radar._acquire()
        seen[(name, hour)] = captured.get('target')
    assert seen == {('warm', 14.0): 4, ('watch', 14.0): 8, ('watch', 2.0): 1, ('live', 2.0): 8}, seen


def test_prefetch_only_when_live(make_emitter, hybrid, active):
    e = make_emitter(); e._runtime.running = True
    tier(e, 'warm')
    ctx = dict(viewed=True, refresh=dict(state='idle'), zoom=8, sources=[{}, {'available': True}], station=(47.6, -122.3), center=dict(lat=47.6, lon=-122.3))
    hybrid.view()
    e.radar._prefetch('iem-mrms-lcref', ctx)                  # warm: returns before any work
    tier(e, 'live')
    e.radar._session = radar_engine.RadarSession(); e.radar._session.begin_pass(ae.time.monotonic() + 10)
    e.radar._result = radar_engine._RADAR_NONE._replace(ts_frame=None)
    e.radar._prefetch('iem-mrms-lcref', ctx)                  # live: proceeds (and finds nothing to do)


def test_payload_publishes_the_tier_and_a_rise_wakes_acquisition(make_emitter, hybrid, active, tmp_path):
    e = make_emitter(); e._runtime.running = True
    (tmp_path / 'radar_attention_force').write_text('rest')
    p = e._build_payload()
    a = p['radar']['attention']
    assert a['tier'] == 'rest' and a['tiles'] is False and a['mode'] == 'active' and a['waking'] is False
    (tmp_path / 'radar_attention_force').unlink()
    scheduled = []
    e._runtime.schedule = lambda cb, delay, interval=False: scheduled.append(delay)
    (tmp_path / 'radar_viewing').write_text(json.dumps(dict(since=ae.time.time(), last=ae.time.time())))
    p = e._build_payload()
    assert p['radar']['attention']['tier'] == 'live'
    assert p['radar']['attention']['waking'] is True           # no frame yet: the page says "waking"
    assert scheduled and min(scheduled) <= 0.1                 # acquisition kicked immediately
    assert e.radar._glances.total == 1                         # a view start was recorded


def test_force_marker_and_bytes_by_tier(make_emitter, hybrid, active, tmp_path):
    e = make_emitter(); e._runtime.running = True
    (tmp_path / 'radar_attention_force').write_text('dormant')
    p = e._build_payload()
    assert p['radar']['attention']['tier'] == 'dormant' and 'forced' in p['radar']['attention']['reason']
    tier(e, 'live')
    e.radar._attention.forced = None
    hybrid.view(); e.radar._acquire()
    health = e.radar._health_payload()['attention']
    assert health['bytesByTier'].get('live', 0) > 0
    assert set(health['knobs']) >= {'frames', 'tiles', 'listing', 'sentinel', 'prefetch'}


def test_frame_echo_comes_from_tile_metadata(make_emitter, hybrid, active):
    e = make_emitter(); e._runtime.running = True
    tier(e, 'live'); hybrid.view()
    e.radar._acquire()
    newest = e.radar._result.frames[-1]
    assert newest['complete'] and newest['echo'] is True       # solid green fixture tiles have opaque pixels


def test_shadow_mode_publishes_but_never_applies(make_emitter, hybrid, monkeypatch):
    monkeypatch.setattr(radar_engine, 'RADAR_ATTENTION_MODE', 'shadow')
    e = make_emitter(); e._runtime.running = True
    tier(e, 'dormant')
    e.radar._acquire()
    assert e.radar._result.available                           # tiles fetched as before
    assert e._build_payload()['radar']['attention']['mode'] == 'shadow'


def test_entering_rest_runs_a_prompt_quiet_pass_then_the_floor_and_a_rise_rearms_at_once(make_emitter, hybrid, active, tmp_path):
    e = make_emitter(); e._runtime.running = True
    (tmp_path / 'radar_attention_force').write_text('dormant')
    e._build_payload()                                            # watch -> dormant: first quiet check now, then the hour floor
    assert e.radar._attention.tier == 'dormant'
    assert e.radar._discovery_floor_until - ae.time.time() <= 5
    e.radar._acquire()                                                 # the quiet check runs and stamps the floor
    assert e.radar._pass['outcome'] == 'quiet'
    e.radar._arm_discovery()
    assert e.radar._discovery_floor_until - ae.time.time() >= 3600 - 10
    (tmp_path / 'radar_attention_force').write_text('live')
    e._build_payload()                                            # dormant -> live: the wakeup is no longer an hour away
    assert e.radar._attention.tier == 'live'
    assert e.radar._discovery_floor_until - ae.time.time() < 300


def test_frame_echo_needs_more_than_clutter(make_emitter, hybrid, active, monkeypatch):
    e = make_emitter(); e._runtime.running = True
    tier(e, 'live'); hybrid.view(); e.radar._acquire()
    assert e.radar._result.frames[-1]['echo'] is True             # solid fixture tiles: 65,536 opaque px each
    monkeypatch.setattr(radar_engine, 'RADAR_ECHO_MIN_SHARE', 10.0)
    ctx = dict(zoom=e.radar._result.zoom, smooth=False, bounds=e.radar._result.bounds, station=(47.6, -122.3),
               tiles=e.radar._result.tiles and [(t[0], t[1], 0, 0) for t in []] or None, sites=[], site_scans={})
    src = e.radar._result.source_id
    frame = e.radar._result.frames[-1]
    pairs = [(p['id'], p['ts']) for p in frame.get('siteScans') or []] or None
    assert e.radar._frame_echo(dict(ctx, tiles=radar_engine._radar_grid(dict(center=e.radar._result.center, zoom=ctx['zoom'], bounds=e.radar._result.bounds))), src, pairs, frame['ts']) in (False, None)


def test_a_forced_quiet_tier_ignores_the_viewed_marker(make_emitter, hybrid, active, tmp_path):
    # a fresh radar_viewed marker is real attention and promotes an ordinary rest to warm;
    # the force override is authoritative and keeps the pass quiet
    e = make_emitter(); e._runtime.running = True
    hybrid.view()
    (tmp_path / 'radar_attention_force').write_text('rest')
    e._build_payload()
    tier(e, 'rest'); e.radar._attention.forced = 'rest'
    e.radar._sentinel = dict(at=ae.time.time(), echo=False, pixels=0, stamp=ae.time.time())
    e.radar._acquire()
    assert e.radar._pass['outcome'] == 'quiet' and not tile_requests(hybrid.calls)


def test_a_fall_into_rest_checks_promptly_even_with_a_scan_schedule(make_emitter, hybrid, active, tmp_path):
    # with frames on hand DiscoverySchedule re-derives its due from the newest scan;
    # the first quiet check must still come at once, not at the next scan time
    e = make_emitter(); e._runtime.running = True
    tier(e, 'live'); hybrid.view(); e.radar._acquire()
    assert e.radar._result.frames
    e.radar._arm_discovery()
    assert e.radar._discovery_floor_until - ae.time.time() > 30       # the natural scan wait
    (tmp_path / 'radar_attention_force').write_text('rest')
    e._build_payload()
    assert e.radar._attention.tier == 'rest'
    assert e.radar._discovery_floor_until - ae.time.time() <= 5       # prompt first quiet check


def test_echo_floor_is_rain_not_insects():
    # the panel's measured dry-night KATX frame: 315k px at 10 dBZ, 6.9k at 20, 364 at 25, 0 at 30
    from lib import radar_palette as rp
    assert rp.WEATHER_FLOOR_DBZ == 25
    footprint = 15 * 65536
    assert 364 < radar_engine.RADAR_ECHO_MIN_SHARE * footprint            # the dry night stays quiet
    assert 19_475 >= radar_engine.RADAR_ECHO_MIN_SHARE * 35 * 65536       # the evening's real showers count
