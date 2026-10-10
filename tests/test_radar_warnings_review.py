""" Round-3 adversarial review of NWS warning polygons: engine fixes.

Each test reproduces a reviewed failure (it fails on the round-3 code) and
pins the root-cause fix:
  1 partial cancellation resolved per segment and by chronology
  2 coverage = everything the page camera can reach (zoom 4 floor, pan cap)
  3 coverage, station containment and the item cap from ORIGINAL geometry
  5 the earliest removal deadline published apart from the displayed end
  6 published freshness deadline (a failed refresh: refreshFailedAt, see test_radar_warnings_ux)
  7 site latency: persistent seen-scan history, one vote per listing, sample age
  + exact polygon reach (not bounds), malformed features isolated, one
    end-to-end fetch deadline; the loop caption's frame time is station-local.
Hermetic: fixtures and a local TLS origin; no network.
"""
import json
import math
import time
from datetime import datetime, timezone

import pytest

from lib import almanac_emit as ae
from lib import radar_engine
from lib import nws_warnings as nw
from tests.fixtures import nws_warnings as fx
from tests.fixtures import obs_scenarios as scn
from tests.fixtures.config import make_config
from tests.test_radar_keepalive import origin  # noqa: F401  (local TLS origin fixture)
from tests.test_radar_warnings import FakeResp, Net, emitter

STATION = (47.61, -122.33)                      # a generic Puget Sound point
REACH = nw.Reach(*STATION)
HOME = nw.Reach(*STATION, radar_engine._radar_zoom_for(STATION[0]))


def parse(features, now, reach=REACH, until=None, home=HOME):
    return nw.parse(features, now, reach, until, home)


def ids(items):
    return [i['id'] for i in items]


# ------------------------------------------------------- 1 partial cancellation
def test_partial_cancellation_keeps_the_continuing_segment():
    # One SVS, two segments, same VTEC event: the part of the warning near the
    # station is cancelled (CAN) while the part over it continues (CON).
    now = time.time()
    original = fx.feature(now, n=1, ring=fx.OVER_STATION, sent_ago=900)
    can = fx.feature(now, n=2, ring=fx.NEARBY, message='Update', vtec_action='CAN', sent_ago=30, references=(1,))
    con = fx.feature(now, n=3, ring=fx.OVER_STATION, message='Update', vtec_action='CON', sent_ago=30, references=(1,))
    items = parse([original, can, con], now)
    assert ids(items) == [con['properties']['id']] and items[0]['affectsStation'] is True


def test_a_cancel_never_vetoes_a_continuation_sent_after_it():
    now = time.time()
    original = fx.feature(now, n=1, ring=fx.OVER_STATION, sent_ago=900)
    can = fx.feature(now, n=2, ring=fx.NEARBY, message='Update', vtec_action='CAN', sent_ago=600, references=(1,))
    later = fx.feature(now, n=3, ring=fx.OVER_STATION, message='Update', vtec_action='CON', sent_ago=60, references=(2,))
    assert ids(parse([original, can, later], now)) == [later['properties']['id']]


def test_a_cancel_sent_after_the_continuation_ends_the_warning():
    now = time.time()
    con = fx.feature(now, n=1, ring=fx.OVER_STATION, message='Update', vtec_action='CON', sent_ago=600)
    can = fx.feature(now, n=2, ring=fx.OVER_STATION, message='Update', vtec_action='CAN', sent_ago=60)
    assert parse([con, can], now) == []


def test_a_cancel_only_touches_its_own_warning():
    now = time.time()
    keep = fx.feature(now, n=1, ring=fx.OVER_STATION, etn=51, sent_ago=900)
    other = fx.feature(now, n=2, ring=fx.NEARBY, etn=52, sent_ago=900)
    can_other = fx.feature(now, n=3, ring=fx.NEARBY, etn=52, message='Cancel', vtec_action='CAN',
                           sent_ago=30, references=(2,))
    assert ids(parse([keep, other, can_other], now)) == [keep['properties']['id']]


def test_cancel_message_without_vtec_removes_only_what_it_references():
    now = time.time()
    a = fx.feature(now, n=1, ring=fx.OVER_STATION, etn=51)
    b = fx.feature(now, n=2, ring=fx.NEARBY, etn=52)
    cancel = fx.feature(now, n=3, ring=fx.NEARBY, message='Cancel', references=(2,))
    cancel['properties']['parameters'].pop('VTEC')
    assert ids(parse([a, b, cancel], now)) == [a['properties']['id']]


def test_an_upgrade_in_one_segment_cancels_the_old_event_and_draws_the_new():
    # SVR upgraded to TOR: the TOR segment carries CAN for the SVR and NEW for the TOR.
    now = time.time()
    svr = fx.feature(now, n=1, ring=fx.OVER_STATION, event='Severe Thunderstorm Warning', phen='SV', etn=40, sent_ago=900)
    tor = fx.feature(now, n=2, ring=fx.OVER_STATION, etn=12, sent_ago=60)
    tor['properties']['parameters']['VTEC'] = ['/O.CAN.KSEW.SV.W.0040.000000T0000Z-261009T2045Z/',
                                               '/O.NEW.KSEW.TO.W.0012.261009T2018Z-261009T2045Z/']
    items = parse([svr, tor], now)
    assert [i['kind'] for i in items] == ['tornado']


# ------------------------------------------------------------------ 2 reach
def test_reach_is_the_camera_floor_and_its_pan_limit():
    # The reviewer's case: Denver is 932 camera px from Seattle at zoom 6,
    # inside the 1,611 px pan allowance. The old zoom-8 disc missed it.
    denver = [[-105.05, 39.70], [-104.90, 39.70], [-104.90, 39.80], [-105.05, 39.80]]
    assert nw.Reach(*STATION, 6).reaches(denver)
    assert not nw.Reach(*STATION, 8).reaches(denver)
    assert REACH.reaches(denver) and 'CO' in nw.area_codes(REACH)
    now = time.time()
    [item] = parse([fx.feature(now, ring=denver, office='KBOU')], now)
    assert item['affectsStation'] is False


def test_reach_edges_match_the_page_clamp():
    # Straight east of the station the camera can put a point on screen up to
    # pan + half the viewport width away (zoom 6, so no wrap); just beyond, not.
    r = nw.Reach(*STATION, 6)
    world = 256 * 2 ** 6
    def east(px):
        dlon = px / world * 360
        return [[STATION[1] + dlon, STATION[0] - .01], [STATION[1] + dlon + .001, STATION[0] - .01],
                [STATION[1] + dlon + .001, STATION[0] + .01], [STATION[1] + dlon, STATION[0] + .01]]
    edge = 1.5 * math.hypot(956, 490) + 478
    assert r.reaches(east(edge - 2)) and not r.reaches(east(edge + 2))
    # Diagonally the region is rounded: the viewport corner plus the pan radius.
    x, y = nw._mercator(*STATION)
    d = (1.5 * math.hypot(956, 490)) / world / math.sqrt(2)
    lat_of = lambda my: math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * my))))
    def corner(scale):
        mx, my = x + 478 / world + d * scale, y - 245 / world - d * scale
        lon, lat = mx * 360 - 180, lat_of(my)
        return [[lon, lat], [lon + 1e-4, lat], [lon + 1e-4, lat + 1e-4], [lon, lat + 1e-4]]
    assert r.reaches(corner(.99)) and not r.reaches(corner(1.01))


def test_the_emitter_queries_what_the_camera_can_reach(make_emitter, monkeypatch):
    Net(monkeypatch)
    e = emitter(make_emitter)
    reach, home, codes, url = e.radar._warnings_query()
    assert reach.zoom == radar_engine.RADAR_MIN_ZOOM and home.zoom == radar_engine._radar_zoom_for(STATION[0])
    assert set(codes) == set(nw.AREAS) and 'area=' in url


@pytest.mark.parametrize('station', [(47.61, -122.33), (35.47, -97.52)])   # Seattle, Oklahoma City
def test_request_size_seattle_and_oklahoma_city(station):
    # Both cameras reach every NWS area at zoom 4: one national, event-filtered
    # query (490 characters), measured at 6 warnings / 43 KB on 2026-10-09.
    codes = nw.area_codes(nw.Reach(*station))
    assert len(codes) == len(nw.AREAS) == 74
    assert len(nw.query_url(codes)) < 600


def test_without_a_viewer_only_the_home_view_speeds_polling(make_emitter, monkeypatch):
    Net(monkeypatch)
    now = time.time()
    london = emitter(make_emitter, Station={'Latitude': '51.5', 'Longitude': '-0.12', 'Timezone': 'Europe/London'})
    london.radar._attention.tier = 'rest'
    london.radar._attention.wet_until = now + 600             # rain at London: no NWS concern
    assert london.radar._warnings_fast(now) is False
    london.radar._attention.tier = 'live'                     # a viewer can pan to the US
    assert london.radar._warnings_fast(now) is True
    seattle = emitter(make_emitter)
    seattle.radar._attention.tier = 'rest'
    far = [[-97.6, 35.4], [-97.4, 35.4], [-97.4, 35.6], [-97.6, 35.6]]
    seattle.radar._warnings.succeeded(now, parse([fx.feature(now, ring=far, office='KOUN')], now), 'u')
    assert seattle.radar._warnings_fast(now) is False                # a far warning: no hurry
    seattle.radar._warnings.succeeded(now, parse([fx.feature(now, ring=fx.NEARBY)], now), 'u')
    assert seattle.radar._warnings_fast(now) is True


# ------------------------------------------- 3 original geometry, cap, wrap
def seven_components(now, **kw):
    big = [fx.big_ring(-122.0 + .25 * k, 47.9, vertices=60, radius=.08) for k in range(6)]
    geometry = {'type': 'MultiPolygon',
                'coordinates': [[r + [r[0]]] for r in big] + [[fx.OVER_STATION + [fx.OVER_STATION[0]]]]}
    return fx.feature(now, geometry=geometry, event='Severe Thunderstorm Warning', phen='SV', **kw)


def test_station_coverage_uses_original_geometry_and_the_covering_part_leads():
    now = time.time()
    [item] = parse([seven_components(now)], now)
    assert item['affectsStation'] is True
    assert nw.point_in_ring(STATION[1], STATION[0], item['polygon'][0])
    assert len(item['polygon']) == 7
    assert sum(len(r) for r in item['polygon']) <= nw.MAX_ITEM_VERTICES


def test_the_cap_never_drops_a_station_covering_warning():
    now = time.time()
    covering = seven_components(now, n=1, etn=1)
    nearby = [fx.feature(now, n=k + 2, etn=k + 2, event='Severe Thunderstorm Warning', phen='SV',
                         ring=fx.big_ring(-122.4 + (k % 4) * .1, 47.66 + (k // 4) * .1, vertices=8, radius=.03))
              for k in range(12)]
    items = parse(nearby + [covering], now)
    assert covering['properties']['id'] in ids(items) and items[0]['affectsStation']
    assert len(items) == len(nearby) + 1


def test_the_cap_never_drops_a_tornado_or_an_emergency():
    now = time.time()
    tornadoes = [fx.feature(now, n=k + 1, etn=k + 1, ring=fx.big_ring(-100 + k * .5, 35, vertices=10, radius=.1),
                         office='KOUN') for k in range(15)]
    ffe = fx.feature(now, n=50, etn=50, event='Flash Flood Warning', phen='FF', office='KOUN',
                     ring=fx.big_ring(-95, 33, vertices=10, radius=.1), params={'flashFloodDamageThreat': ['CATASTROPHIC']})
    severe = [fx.feature(now, n=60 + k, etn=60 + k, event='Severe Thunderstorm Warning', phen='SV',
                         ring=fx.big_ring(-122.2, 47.7 + k * .05, vertices=8, radius=.02)) for k in range(5)]
    items = parse(tornadoes + severe + [ffe], now)
    kinds = [i['kind'] for i in items]
    assert kinds.count('tornado') == 15 and ffe['properties']['id'] in ids(items)
    assert kinds.count('severe') == len(severe)        # distant priority cannot remove local coverage
    assert items[0]['kind'] == 'severe'
    assert all(i['polygon'] for i in items)


def test_every_warning_shares_one_vertex_budget():
    now = time.time()
    storms = [fx.feature(now, n=k + 1, etn=k + 1, ring=fx.big_ring(-100 + (k % 10), 30 + k // 10, radius=.3),
                         office='KOUN') for k in range(40)]
    items = parse(storms, now)
    assert len(items) == 40
    assert sum(len(r) for i in items for r in i['polygon']) <= nw.TARGET_TOTAL_VERTICES
    assert all(len(i['polygon'][0]) >= 4 for i in items)


def test_simplification_never_erases_a_ring():
    ring = fx.big_ring(-122.3, 47.6, vertices=400)
    out = nw.simplify(ring, 4)
    assert 4 <= len(out) <= 4 and out[0] == out[-1]
    tiny = [[-122.3301, 47.6101], [-122.3297, 47.6101], [-122.3297, 47.6104]]   # under 0.01 degree
    assert len(nw.simplify(tiny)) == 4


def test_antimeridian_polygon_covers_a_station_on_either_side():
    now = time.time()
    ring = [[179.5, 51.5], [-179.5, 51.5], [-179.5, 52.3], [179.5, 52.3]]
    feature = fx.feature(now, ring=ring, event='Special Marine Warning', phen='MA', office='PAFC')
    for lon in (179.9, -179.9):
        reach = nw.Reach(51.9, lon)
        [item] = nw.parse([feature], now, reach)
        assert item['affectsStation'] is True, lon
        assert all(-180 <= x <= 180 for r in item['polygon'] for x, _ in r)
    assert {'AK', 'PK'} <= set(nw.area_codes(nw.Reach(51.9, 179.9, 8)))


# --------------------------------------------- 5 removal deadline vs event end
def test_the_earliest_deadline_removes_and_the_event_end_is_displayed():
    now = 1_791_576_900
    until = lambda ts: ae._clock(datetime.fromtimestamp(ts, ae.pytz.timezone('America/Los_Angeles')), '12 hr')
    [item] = parse([fx.feature(now, ring=fx.OVER_STATION, expires_in=60, ends_in=600)], now, until=until)
    assert item['expires'] == now + 60 and item['ends'] == now + 600
    assert item['until'] == until(now + 600)
    t = nw.Tracker()
    t.succeeded(now, [item], 'u')
    assert len(t.payload(now + 59)['items']) == 1 and t.payload(now + 61)['items'] == []


# ------------------------------------------------------- 6 freshness deadline
def test_payload_publishes_a_freshness_deadline():
    t = nw.Tracker()
    t.began(1000.0, fast=False)
    t.succeeded(1000.0, [], 'u')
    p = t.payload(1000.0)
    assert p['staleAt'] == 1000 + nw.SLOW_SEC + nw.STALE_GRACE_SEC and p['stale'] is False
    assert t.payload(p['staleAt'])['stale'] is True
    t.began(2000.0, fast=True)
    t.succeeded(2000.0, [], 'u')
    assert t.payload(2000.0, fast=True)['staleAt'] == 2000 + nw.FAST_SEC + nw.STALE_GRACE_SEC


def test_switching_to_fast_does_not_flash_stale():
    # A slow-scheduled success stays current until a fast refresh replaces it.
    t = nw.Tracker()
    t.began(1000.0, fast=False)
    t.succeeded(1000.0, [], 'u')
    assert t.payload(1000.0 + 600, fast=True)['stale'] is False
    assert t.due(1000.0 + 600, True, 'u')


def test_a_failed_refresh_keeps_last_good_and_its_deadline():
    # Changed in the UX pass: one failed refresh no longer blanks the map; the
    # page draws last good (dimmed) until the last success's deadline.
    now = time.time()
    t = nw.Tracker()
    t.succeeded(now, parse([fx.feature(now, ring=fx.NEARBY)], now), 'u')
    t.failed(now + 1, 'HTTP 503')
    p = t.payload(now + 1)
    assert p['stale'] is False and len(p['items']) == 1 and p['staleAt'] is not None
    assert p['refreshFailedAt'] == int(now + 1)


def test_the_emitter_publishes_the_deadline(make_emitter, monkeypatch):
    net = Net(monkeypatch)
    now = time.time()
    net.answers.append(FakeResp(json.dumps(fx.collection(fx.feature(now, ring=fx.OVER_STATION)))))
    e = emitter(make_emitter)
    e.radar._attention.tier = 'rest'
    e.radar._check_warnings()
    e._spawned[0][1]()
    w = e._build_payload()['radar']['warnings']
    assert w['stale'] is False
    assert w['staleAt'] - w['fetchedTs'] - (nw.SLOW_SEC + nw.STALE_GRACE_SEC) in (0, 1)   # whole seconds
    assert set(w['items'][0]) >= {'expires', 'ends', 'until'} and not any(k.startswith('_') for k in w['items'][0])


# ------------------------------------------------------- 7 latency estimator
T = 1_791_576_900


def healthy(e, scans=12, latency=300):
    """ Scan k at T + 300k is listed from T + 300k + latency; listings every 60 s. """
    listing, now = [T - 300], T
    for k in range(scans):
        ts = T + 300 * k
        while now < ts + latency:
            e.radar._note_latency('KATX', list(listing), now)
            now += 60
        listing.append(ts)
        e.radar._note_latency('KATX', list(listing), now)
        now += 60
    return listing, now


def test_a_regressed_listing_does_not_turn_old_scans_into_latency(make_emitter):
    e = make_emitter(scn.all_none())
    listing, now = healthy(e)
    before = e.radar._site_latency('KATX', now)
    assert 300 <= before < 360 and radar_engine._radar_site_stale_sec(300, before) <= 960
    e.radar._note_latency('KATX', [], now)                         # IEM answers empty
    e.radar._note_latency('KATX', listing[:3], now + 60)            # ... then partial
    e.radar._note_latency('KATX', listing, now + 120)               # ... then whole again
    assert e.radar._site_latency('KATX', now + 120) == before
    assert len(e.radar._latency['KATX']['samples']) == 12


def test_a_backlog_batch_is_one_vote(make_emitter):
    e = make_emitter(scn.all_none())
    listing, now = healthy(e, scans=3)
    count = len(e.radar._latency['KATX']['samples'])
    newest = listing[-1]
    while now < newest + 900 + 300:                                # listings keep coming, nothing new
        e.radar._note_latency('KATX', list(listing), now)
        now += 60
    batch = listing + [newest + 300, newest + 600, newest + 900]
    e.radar._note_latency('KATX', batch, newest + 900 + 300)       # then three new scans at once
    samples = e.radar._latency['KATX']['samples']
    assert len(samples) == count + 1 and samples[-1][1] == 300      # the newest arrival only


def test_a_backfilled_older_scan_is_not_a_sample(make_emitter):
    # A scan that fills an old gap does not advance the newest scan: it says
    # nothing about how quickly IEM publishes.
    e = make_emitter(scn.all_none())
    listing, now = healthy(e, scans=4)
    count = len(e.radar._latency['KATX']['samples'])
    e.radar._note_latency('KATX', sorted(listing + [listing[-1] - 150]), now)
    assert len(e.radar._latency['KATX']['samples']) == count


def test_samples_expire(make_emitter):
    e = make_emitter(scn.all_none())
    _, now = healthy(e)
    assert e.radar._site_latency('KATX', now) is not None
    assert e.radar._site_latency('KATX', now + radar_engine.RADAR_SITE_LATENCY_MAX_AGE_SEC + 3600) is None


# --------------------------------------------- should-fix: exact reach test
def test_polygon_reach_is_exact_not_by_bounds():
    # A triangle whose bounding box contains the station but whose hypotenuse
    # passes far beyond what the camera can show (zoom 8).
    r = nw.Reach(*STATION, 8)
    lat, lon = STATION
    triangle = [[lon + 30, lat], [lon + 30, lat + 30], [lon, lat + 30]]
    box = [[lon, lat], [lon + 30, lat], [lon + 30, lat + 30], [lon, lat + 30]]
    assert r.reaches(box) and not r.reaches(triangle)
    now = time.time()
    assert nw.parse([fx.feature(now, ring=triangle)], now, r) == []


# ---------------------------------------- should-fix: malformed is isolated
@pytest.mark.parametrize('damage', [
    lambda p: p.update(parameters=['not', 'a', 'dict']),
    lambda p: p.update(references='urn:oid:x'),
    lambda p: p.update(references=[None, 7, {'identifier': 5}]),
    lambda p: p.update(event=['Tornado Warning']),
    lambda p: p.update(sent=17, expires={'x': 1}),
    lambda p: p['parameters'].update(VTEC='/O.NEW.KSEW.TO.W.0051.x/'),
    lambda p: p['parameters'].update(maxHailSize=[None], tornadoDetection=[3]),
])
def test_one_malformed_feature_never_costs_the_rest(damage):
    now = time.time()
    bad = fx.feature(now, n=9, ring=fx.NEARBY, etn=77)
    damage(bad['properties'])
    weird_geometry = fx.feature(now, n=8, etn=78, geometry={'type': 'MultiPolygon', 'coordinates': [[[['x', 1]]], 5, None]})
    good = fx.feature(now, n=1, ring=fx.OVER_STATION)
    items = parse([bad, weird_geometry, good], now)
    assert good['properties']['id'] in ids(items)


# ------------------------------------------ should-fix: end-to-end deadline
def test_a_trickled_body_hits_one_fetch_deadline(make_emitter, origin, monkeypatch):  # noqa: F811
    monkeypatch.setattr(nw, 'FETCH_DEADLINE_SEC', 1.0)
    body = json.dumps(fx.collection()).encode() + b' ' * 4000
    origin.drip = (b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n' % len(body) + body, 0.02)
    e = make_emitter(scn.all_none(), config=make_config(Display={'TimeFormat': '12 hr'}))
    reach, home, codes, url = e.radar._warnings_query()
    started = time.monotonic()
    try:
        e.radar._do_warnings(origin.url + '/alerts/active?area=WA', reach, home)
    finally:
        if e.radar._warnings_session is not None:
            e.radar._warnings_session.close()
    elapsed = time.monotonic() - started
    # The whole body would take ~80 s to drip; only a deadline stops it.
    assert elapsed < 3.0, elapsed
    assert e.radar._warnings.failures == 1 and e.radar._warnings.stale(time.time()) is True
    # bytes keep arriving every 20 ms, so no per-read timeout can fire: the
    # remaining-time deadline did (expressed as a socket timeout on the read)
    assert 'deadline' in e.radar._warnings.error or 'timed out' in e.radar._warnings.error, e.radar._warnings.error
    assert origin.requests and origin.requests[0][2].startswith('/alerts/active')


def test_the_fetch_uses_one_deadline_bounded_transport(make_emitter, monkeypatch):
    calls = []
    class Session:
        def open(self, req, timeout):
            calls.append(timeout)
            raise TimeoutError('radar request exceeded deadline')
        def close(self):
            pass
    monkeypatch.setattr(radar_engine, 'RadarSession', Session)
    e = make_emitter(scn.all_none(), config=make_config())
    reach, home, codes, url = e.radar._warnings_query()
    e.radar._do_warnings(url, reach, home)
    e.radar._do_warnings(url, reach, home)
    assert calls == [nw.FETCH_DEADLINE_SEC] * 2 and isinstance(e.radar._warnings_session, Session)
    assert e.radar._warnings.failures == 2


# ------------------------------------------- the loop caption's frame time
def test_frame_labels_are_station_local_in_the_clock_style():
    # The UX fixture wrote its own UTC "20:52" into frames[].at; the engine
    # labels every frame in the station's zone and clock style.
    ts = 1_791_579_120                                    # 20:52 UTC = 1:52 PM PDT
    frame = dict(ts=ts, stamp='x', complete=True, levels={}, mosaicKey='M', siteScans=[], requestedPairs=[])
    snap = radar_engine._RADAR_NONE._replace(available=True, reason=None, frames=(frame,), ts_frame=ts,
                                   tiles=dict(frames=[dict(frame)]), legend={}, sources=(), sites=())
    tz = ae.pytz.timezone('America/Los_Angeles')
    r = radar_engine.RadarEngine._payload(snap, ts + 300, tz, style='12 hr')
    assert r['tiles']['frames'][0]['at'] == '1:52 PM'
    assert datetime.fromtimestamp(ts, timezone.utc).strftime('%H:%M') == '20:52'
