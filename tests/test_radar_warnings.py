""" NWS storm-based warning polygons (lib/nws_warnings + the emitter's fetch).

Hermetic: urllib is replaced by fakes that record requests; features are
real-shaped GeoJSON from tests/fixtures/nws_warnings. No network.
"""
import io
import json
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

import pytest

from lib import almanac_emit as ae
from lib import radar_engine
from lib import nws_warnings as nw
from tests.fixtures import nws_warnings as fx
from tests.fixtures import obs_scenarios as scn
from tests.fixtures.config import make_config

STATION = (47.61, -122.33)
REACH = nw.Reach(*STATION)                                      # the camera floor: zoom 4
HOME = nw.Reach(*STATION, radar_engine._radar_zoom_for(STATION[0]))       # the station's automatic view


class FakeResp:
    def __init__(self, body, etag=None):
        self._body = body.encode() if isinstance(body, str) else body
        self.headers = {'ETag': etag} if etag else {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, limit=-1):
        return self._body if limit is None or limit < 0 else self._body[:limit]


class Net:
    """ Records requests; answers from a queue of bodies or exceptions. """
    def __init__(self, monkeypatch):
        self.requests, self.answers = [], []
        monkeypatch.setattr(urllib.request, 'urlopen', self.urlopen)            # the station strip
        monkeypatch.setattr(radar_engine.RadarEngine, '_warnings_open',              # the warnings transport
                            lambda emitter, req: self.urlopen(req, nw.FETCH_DEADLINE_SEC))

    def urlopen(self, req, timeout=None):
        self.requests.append((req, timeout))
        answer = self.answers.pop(0) if self.answers else FakeResp(json.dumps(fx.collection()))
        if isinstance(answer, BaseException):
            raise answer
        return answer


def parse(features, now=None, reach=REACH, until=None, home=HOME):
    return nw.parse(features, time.time() if now is None else now, reach, until, home)


# ------------------------------------------------------------------ parsing
def test_storm_warning_over_the_station_is_drawn_and_flagged():
    now = time.time()
    items = parse([fx.feature(now, ring=fx.OVER_STATION), fx.feature(now, n=2, ring=fx.NEARBY, etn=52)], now)
    assert [i['affectsStation'] for i in items] == [True, False]
    lead = items[0]
    assert lead['event'] == 'Tornado Warning' and lead['color'] == '#FF0000' and lead['kind'] == 'tornado'
    assert lead['level'] == 'warning' and lead['label'] == 'Tornado Warning'
    assert lead['polygon'][0][0] == lead['polygon'][0][-1]                 # closed ring of [lon, lat]
    assert lead['polygon'][0][0] == [-122.4, 47.55]
    assert lead['expires'] == int(now + 1500) and isinstance(lead['onset'], int)
    assert lead['headline'].startswith('Tornado Warning issued')
    assert lead['detail'] == 'Radar indicated'


def test_multipolygon_keeps_every_outer_ring():
    now = time.time()
    geometry = fx.multipolygon(fx.NEARBY, fx.OVER_STATION)
    [item] = parse([fx.feature(now, event='Flash Flood Warning', phen='FF', geometry=geometry)], now)
    assert len(item['polygon']) == 2 and item['affectsStation'] is True
    assert item['color'] == '#8B0000' and item['kind'] == 'flashflood'


def test_zone_based_and_non_storm_alerts_are_not_drawn():
    now = time.time()
    tropical = fx.feature(now, n=3, event='Tropical Storm Warning', ring=fx.OVER_STATION, phen='TR')
    assert parse([fx.zone_based(now), tropical], now) == []
    # a storm-based event without geometry has nothing to draw
    assert parse([fx.feature(now, ring=None)], now) == []


def test_only_actual_alerts_count():
    now = time.time()
    assert parse([fx.feature(now, ring=fx.OVER_STATION, status='Test'),
                  fx.feature(now, n=2, ring=fx.OVER_STATION, status='Exercise', etn=52)], now) == []


def test_moving_update_preserves_uncovered_referenced_warning():
    now = time.time()
    old = fx.feature(now, n=1, ring=fx.NEARBY, sent_ago=900)
    new = fx.feature(now, n=2, ring=fx.OVER_STATION, message='Update', references=(1,), vtec_action='CON', sent_ago=60)
    items = parse([old, new], now)
    assert [i['id'] for i in items] == [new['properties']['id'], old['properties']['id']]
    assert items[0]['affectsStation']


def test_same_vtec_event_with_disjoint_polygons_keeps_both_segments_without_references():
    # An event identity and county alone do not establish segment replacement.
    now = time.time()
    a = fx.feature(now, n=1, ring=fx.NEARBY, sent_ago=900)
    b = fx.feature(now, n=2, ring=fx.OVER_STATION, message='Update', vtec_action='CON', sent_ago=60)
    assert {i['id'] for i in parse([a, b], now)} == {a['properties']['id'], b['properties']['id']}


def test_cancel_and_vtec_can_remove_the_warning():
    now = time.time()
    warning = fx.feature(now, n=1, ring=fx.OVER_STATION)
    cancel = fx.feature(now, n=2, ring=fx.OVER_STATION, message='Cancel', references=(1,), vtec_action='CAN')
    assert parse([warning, cancel], now) == []
    # A CAN update with no Cancel message type (the API's partial cancellation)
    can = fx.feature(now, n=3, ring=fx.OVER_STATION, message='Update', vtec_action='CAN', sent_ago=30)
    assert parse([warning, can], now) == []
    # An EXP message for the same event ends it too
    exp = fx.feature(now, n=4, ring=fx.OVER_STATION, message='Update', vtec_action='EXP', sent_ago=30)
    assert parse([warning, exp], now) == []


def test_expired_by_expires_or_ends_is_dropped():
    now = time.time()
    assert parse([fx.feature(now, ring=fx.OVER_STATION, ends_in=-10)], now) == []
    assert parse([fx.feature(now, ring=fx.OVER_STATION, ends_in=600, expires_in=-1)], now) == []
    [live] = parse([fx.feature(now, ring=fx.OVER_STATION, ends_in=600, expires_in=900)], now)
    assert live['expires'] == int(now + 600)          # `ends` is when the warning ends


def test_pds_and_emergency_variants():
    now = time.time()
    emergency = fx.feature(now, ring=fx.NEARBY, params={'tornadoDamageThreat': ['CATASTROPHIC']})
    pds = fx.feature(now, n=2, ring=fx.NEARBY, etn=60, params={'tornadoDamageThreat': ['CONSIDERABLE']})
    ffe = fx.feature(now, n=3, ring=fx.NEARBY, etn=61, event='Flash Flood Warning', phen='FF',
                     params={'flashFloodDamageThreat': ['CATASTROPHIC']})
    svr = fx.feature(now, n=4, ring=fx.NEARBY, etn=62, event='Severe Thunderstorm Warning', phen='SV',
                     params={'thunderstormDamageThreat': ['DESTRUCTIVE'], 'maxHailSize': ['2.00'],
                             'maxWindGust': ['80 MPH'], 'tornadoDetection': []})
    items = {i['id'][-8:]: i for i in parse([emergency, pds, ffe, svr], now)}
    labels = sorted((i['label'], i['level'], i['threat']) for i in items.values())
    assert labels == [('Flash Flood Emergency', 'emergency', 'emergency'),
                      ('PDS Tornado Warning', 'warning', 'pds'),
                      ('Severe Thunderstorm Warning', 'warning', 'destructive'),
                      ('Tornado Emergency', 'emergency', 'emergency')]
    svr_item = next(i for i in items.values() if i['kind'] == 'severe')
    assert svr_item['detail'] == 'hail 2 in · gusts 80 mph'
    # the emergency sorts first, by severity
    assert parse([pds, emergency], now)[0]['label'] == 'Tornado Emergency'


def test_warnings_outside_the_reachable_area_are_not_included():
    # At zoom 8 the camera cannot reach Florida from Puget Sound (at zoom 4 it can).
    now = time.time()
    assert parse([fx.feature(now, ring=fx.FAR)], now, reach=nw.Reach(*STATION, 8)) == []
    assert len(parse([fx.feature(now, ring=fx.FAR)], now)) == 1


def test_malformed_features_are_skipped_not_fatal():
    now = time.time()
    junk = [None, {}, {'properties': None}, {'properties': {'status': 'Actual', 'event': 'Tornado Warning',
             'messageType': 'Alert', 'id': 'x', 'expires': 'not a time'}, 'geometry': {'type': 'Polygon',
             'coordinates': [[['a', 1], [2]]]}}, fx.feature(now, ring=fx.OVER_STATION)]
    assert len(parse(junk, now)) == 1
    assert parse('not a list', now) == []


def test_until_text_follows_the_clock_helper():
    now = 1_791_576_900                        # 2026-10-09 13:15 PDT
    [item] = parse([fx.feature(now, ring=fx.OVER_STATION, ends_in=1800)], now,
                   until=lambda ts: ae._clock(__import__('datetime').datetime.fromtimestamp(
                       ts, __import__('pytz').timezone('America/Los_Angeles')), '12 hr'))
    assert item['until'] == '1:45 PM'


# ------------------------------------------------------------ size bounds
def test_payload_keeps_all_reachable_items_with_bounded_geometry_and_bytes():
    now = time.time()
    features = [fx.feature(now, n=k + 1, etn=100 + k, event='Severe Thunderstorm Warning', phen='SV',
                           ring=fx.big_ring(-122.0 + (k % 5) * 0.9, 47.0 + (k // 5) * 0.9, radius=0.3))
                for k in range(40)]
    items = parse(features, now)
    assert len(items) == len(features)
    for item in items:
        assert sum(len(r) for r in item['polygon']) <= nw.MAX_ITEM_VERTICES
        assert all(len(r) <= nw.MAX_RING_VERTICES for r in item['polygon'])
    assert sum(len(r) for i in items for r in i['polygon']) <= max(
        nw.TARGET_TOTAL_VERTICES, sum(nw.MAX_ITEM_VERTICES if i['_near'] else nw.MIN_RING_VERTICES for i in items))
    size = len(json.dumps(dict(available=True, fetchedTs=int(now), stale=False, items=items), separators=(',', ':')))
    assert size < 60_000, size            # worst case; a typical storm day is a few hundred bytes per warning


def test_simplification_preserves_small_polygons_exactly():
    ring = fx.OVER_STATION + [fx.OVER_STATION[0]]
    assert nw.simplify(ring) == [[round(x, 2), round(y, 2)] for x, y in ring]


# ----------------------------------------------------------- area selection
def test_area_codes_for_a_washington_station():
    # The station's own view reaches its neighbours only ...
    home = nw.area_codes(HOME)
    assert {'WA', 'OR', 'ID', 'PZ'} <= set(home)
    assert not {'TX', 'FL', 'NY', 'GM', 'AN', 'LS', 'HI'} & set(home)
    # ... but the camera, zoomed out to 4 and panned, reaches every NWS area.
    codes = nw.area_codes(REACH)
    assert set(codes) == set(nw.AREAS)
    url = nw.query_url(codes)
    q = parse_qs(urlsplit(url).query)
    assert q['status'] == ['actual'] and q['message_type'] == ['alert,update,cancel']
    assert q['area'][0].split(',') == list(codes)
    assert q['event'][0].split(',') == list(nw.STORM_EVENTS)


def test_area_codes_for_a_non_us_station():
    # No NWS area in a non-US station's own view ...
    assert nw.area_codes(nw.Reach(51.5, -0.12, radar_engine._radar_zoom_for(51.5))) == ()     # London
    assert nw.area_codes(nw.Reach(-33.9, 151.2, radar_engine._radar_zoom_for(-33.9))) == ()   # Sydney
    # ... yet at zoom 4 a London camera can pan to New England: it is in reach.
    assert {'MA', 'ME', 'AN'} <= set(nw.area_codes(nw.Reach(51.5, -0.12)))


def test_reach_covers_the_page_camera_clamp():
    # radarClampCamera: 1.5 viewport diagonals of pan (screen px at the zoom
    # shown) around the station, plus the half viewport on screen, from the
    # camera floor radar.zoomMin.
    assert nw.MIN_ZOOM == radar_engine.RADAR_MIN_ZOOM == 4
    world = 256 * 2 ** 4
    assert REACH.pan == pytest.approx(1.5 * (956 ** 2 + 490 ** 2) ** .5 / world)
    assert (REACH.hw, REACH.hh) == (pytest.approx(478 / world), pytest.approx(245 / world))


# ------------------------------------------------------------- the emitter
def emitter(make_emitter, **config):
    cfg = make_config(Display={'TimeFormat': '12 hr'}, **config)
    e = make_emitter(scn.all_none(), config=cfg)
    spawned = []
    e.radar._spawn = lambda key, worker: spawned.append((key, worker))
    e._spawned = spawned
    return e


def test_fetch_success_publishes_radar_warnings(make_emitter, monkeypatch):
    net = Net(monkeypatch)
    now = time.time()
    net.answers.append(FakeResp(json.dumps(fx.collection(fx.feature(now, ring=fx.OVER_STATION))), etag='W/"a"'))
    e = emitter(make_emitter)
    e.radar._check_warnings()
    [(key, worker)] = e._spawned
    assert key == 'warnings'
    worker()
    req, timeout = net.requests[0]
    assert '/alerts/active?' in req.full_url and 'point=' not in req.full_url
    assert req.get_header('User-agent') == ae.ALERTS_UA_FALLBACK and timeout == nw.FETCH_DEADLINE_SEC
    w = e._build_payload()['radar']['warnings']
    assert w['available'] is True and w['stale'] is False and w['fetchedTs'] is not None
    [item] = w['items']
    assert item['affectsStation'] is True and item['until'][-3:] in ('\u00a0PM', '\u00a0AM')
    json.dumps(w, allow_nan=False)


def test_no_nws_area_in_reach_makes_no_request_and_no_error(make_emitter, monkeypatch):
    net = Net(monkeypatch)
    e = emitter(make_emitter, Station={'Latitude': '51.5', 'Longitude': '-0.12', 'Timezone': 'Europe/London'})
    monkeypatch.setattr(nw, 'area_codes', lambda reach: ())       # a camera that cannot reach NWS
    e.radar._check_warnings()
    assert e._spawned == [] and net.requests == []
    w = e._build_payload()['radar']['warnings']
    assert w == dict(available=False, fetchedTs=w['fetchedTs'], staleAt=None, stale=False, refreshFailedAt=None, items=[])


def test_conditional_get_sends_the_etag_and_handles_304(make_emitter, monkeypatch):
    net = Net(monkeypatch)
    now = time.time()
    net.answers.append(FakeResp(json.dumps(fx.collection(fx.feature(now, ring=fx.NEARBY))), etag='W/"v1"'))
    e = emitter(make_emitter)
    reach, home, codes, url = e.radar._warnings_query()
    e.radar._do_warnings(url, reach, home)
    first = e.radar._warnings.fetched
    net.answers.append(urllib.error.HTTPError(url, 304, 'Not Modified', {}, None))
    e.radar._do_warnings(url, reach, home)
    assert net.requests[1][0].get_header('If-none-match') == 'W/"v1"'
    assert e.radar._warnings.fetched >= first and e.radar._warnings.failures == 0
    assert len(e.radar._warnings.payload(time.time())['items']) == 1      # kept, current


def test_outage_keeps_last_good_reports_the_failure_and_backs_off(make_emitter, monkeypatch):
    net = Net(monkeypatch)
    now = time.time()
    net.answers.append(FakeResp(json.dumps(fx.collection(fx.feature(now, ring=fx.NEARBY, ends_in=7200)))))
    e = emitter(make_emitter)
    reach, home, codes, url = e.radar._warnings_query()
    e.radar._do_warnings(url, reach, home)
    t = e.radar._warnings
    t.fetched -= 400                                   # last success 400 s ago
    net.answers.append(urllib.error.URLError('network down'))
    e.radar._do_warnings(url, reach, home)
    p = t.payload(time.time())
    # last good kept; the failure is reported, the deadline from the last success still stands
    assert p['stale'] is False and len(p['items']) == 1 and p['refreshFailedAt'] is not None
    assert t.payload(p['staleAt'])['stale'] is True
    # backoff: 120, 240, 480, 960, 1800, 1800 - never the 90 s fast cadence
    waits = []
    for n in range(1, 7):
        t.failures, t.attempted = n, 1000.0
        waits.append(t.next_due(1000.0, True, url) - 1000.0)
    assert waits == [120, 240, 480, 960, 1800, 1800]
    # Retry-After is honoured (within the 30-minute ceiling) when it asks for longer
    t.failures = 0
    net.answers.append(urllib.error.HTTPError(url, 503, 'busy', {'Retry-After': '600'}, None))
    e.radar._do_warnings(url, reach, home)
    assert t.next_due(time.time(), True, url) - t.attempted == pytest.approx(600, abs=1)


def test_a_failure_is_reported_not_stale_until_the_deadline():
    t = nw.Tracker()
    t.succeeded(1000.0, [], 'u')
    assert t.stale(1100.0) is False
    t.failed(1100.0, 'x')
    assert t.stale(1100.0) is False and t.payload(1100.0)['refreshFailedAt'] == 1100
    assert t.stale(1000.0 + nw.SLOW_SEC + nw.STALE_GRACE_SEC) is True


def test_never_fetched_is_stale_and_empty():
    t = nw.Tracker()
    assert t.payload(5.0) == dict(available=True, fetchedTs=None, staleAt=None, stale=True, refreshFailedAt=None, items=[])


def test_client_side_expiry_in_the_engine_payload(make_emitter, monkeypatch):
    net = Net(monkeypatch)
    now = time.time()
    net.answers.append(FakeResp(json.dumps(fx.collection(fx.feature(now, ring=fx.NEARBY, ends_in=60)))))
    e = emitter(make_emitter)
    reach, home, codes, url = e.radar._warnings_query()
    e.radar._do_warnings(url, reach, home)
    assert len(e.radar._warnings.payload(now + 30)['items']) == 1
    assert e.radar._warnings.payload(now + 61)['items'] == []


def test_malformed_response_is_a_failure_not_a_crash(make_emitter, monkeypatch):
    net = Net(monkeypatch)
    net.answers += [FakeResp('<html>'), FakeResp(json.dumps({'features': 'x'})),
                    FakeResp(b'{' * (nw.MAX_BODY_BYTES + 2))]
    e = emitter(make_emitter)
    reach, home, codes, url = e.radar._warnings_query()
    for _ in range(3):
        e.radar._do_warnings(url, reach, home)
    assert e.radar._warnings.failures == 3 and e.radar._warnings.items == []


# ------------------------------------------------------------- cadence
def test_cadence_fast_when_viewed_weather_or_a_warning_in_reach(make_emitter, monkeypatch):
    Net(monkeypatch)
    e = emitter(make_emitter)
    now = time.time()
    e.radar._attention.tier = 'rest'
    assert e.radar._warnings_fast(now) is False
    for tier in ('warm', 'live'):
        e.radar._attention.tier = tier
        assert e.radar._warnings_fast(now) is True
    e.radar._attention.tier = 'watch'
    e.radar._attention.wet_until = now + 600            # rain hold: weather nearby
    assert e.radar._warnings_fast(now) is True
    e.radar._attention.wet_until = 0
    assert e.radar._warnings_fast(now) is False
    e.radar._warnings.succeeded(now, parse([fx.feature(now, ring=fx.NEARBY)], now), 'u')
    assert e.radar._warnings_fast(now) is True


def test_due_follows_the_cadence_and_a_changed_area():
    t = nw.Tracker()
    assert t.due(0.0, False, 'u')                          # never fetched: now
    t.succeeded(1000.0, [], 'u')
    assert not t.due(1000.0 + 89, True, 'u') and t.due(1000.0 + 90, True, 'u')
    assert not t.due(1000.0 + 899, False, 'u') and t.due(1000.0 + 900, False, 'u')
    assert t.due(1001.0, False, 'other')                   # a different query: ask now


def test_check_spawns_only_when_due(make_emitter, monkeypatch):
    Net(monkeypatch)
    e = emitter(make_emitter)
    e.radar._attention.tier = 'rest'
    reach, home, codes, url = e.radar._warnings_query()
    e.radar._warnings.succeeded(time.time(), [], url)
    e.radar._check_warnings()
    assert e._spawned == []                                # slow cadence, just fetched
    e.radar._attention.tier = 'live'
    e.radar._warnings.succeeded(time.time() - 95, [], url)
    e.radar._check_warnings()
    assert [k for k, _ in e._spawned] == ['warnings']      # live: 90 s


# ------------------------------------------------- station strip unchanged
def test_station_point_strip_is_unchanged(make_emitter, monkeypatch):
    from tests.fixtures import nws_alerts as nws
    net = Net(monkeypatch)
    body = json.dumps({'features': [{'properties': nws.air_quality_1()}, {'properties': nws.air_quality_2()}]})
    net.answers.append(FakeResp(body))
    e = emitter(make_emitter)
    e._do_alerts()
    assert 'alerts/active?point=47.61,-122.33' in net.requests[0][0].full_url
    p = e._build_payload()
    assert p['alertCount'] == 1 and p['alerts'][0]['tone'] == 'brass' and p['alerts'][0]['level'] == 'advisory'
    assert {'alerts', 'alertCount', 'alertsStale', 'alertsAgeSec', 'alertsAsOf'} <= set(p)
    assert 'warnings' not in p['alerts'][0]


def test_a_new_warning_over_the_station_refreshes_the_strip_once(make_emitter, monkeypatch):
    net = Net(monkeypatch)
    now = time.time()
    body = json.dumps(fx.collection(fx.feature(now, ring=fx.OVER_STATION)))
    net.answers += [FakeResp(body), FakeResp(body)]
    e = emitter(make_emitter)
    calls = []
    e._check_alerts = lambda *a: calls.append(1)
    reach, home, codes, url = e.radar._warnings_query()
    e.radar._do_warnings(url, reach, home)
    e.radar._do_warnings(url, reach, home)
    assert calls == [1]


def test_radar_off_publishes_no_warnings(make_emitter, monkeypatch):
    monkeypatch.setattr(radar_engine, 'RADAR_ENABLED', False)
    e = emitter(make_emitter)
    assert 'warnings' not in e._build_payload()['radar']


def test_health_reports_the_warnings_fetch(make_emitter, monkeypatch):
    Net(monkeypatch)
    e = emitter(make_emitter)
    reach, home, codes, url = e.radar._warnings_query()
    e.radar._do_warnings(url, reach, home)
    h = e.radar._warnings.health(time.time())
    assert h['coverage'] is True and h['failures'] == 0 and h['query'] == url
