"""Final warning-box review regressions. Synthetic CAP records; no I/O."""
import itertools
import json
import time

import pytest

from lib import nws_warnings as nw
from tests.fixtures import nws_warnings as fx
from tests.test_radar_warnings import FakeResp, Net, emitter
from tests.test_radar_warnings_review import REACH, HOME, ids, parse

NOW = 1_791_576_900


def segment(n, ring, ugc, **kw):
    f = fx.feature(NOW, n=n, ring=ring, message='Update', vtec_action='CON', **kw)
    f['properties']['geocode']['UGC'] = ugc
    return f


def cancellation(n, ring, ugc, **kw):
    f = segment(n, ring, ugc, **kw)
    f['properties']['parameters']['VTEC'][0] = f['properties']['parameters']['VTEC'][0].replace('.CON.', '.CAN.')
    return f


def split_predecessor():
    """Station/southern county cancelled; northern county continues."""
    whole = [[-122.5, 47.5], [-122.1, 47.5], [-122.1, 47.9], [-122.5, 47.9]]
    south = [[-122.5, 47.5], [-122.1, 47.5], [-122.1, 47.7], [-122.5, 47.7]]
    north = [[-122.5, 47.7], [-122.1, 47.7], [-122.1, 47.9], [-122.5, 47.9]]
    return [segment(1, whole, ['WAC033', 'WAC061'], sent_ago=600),
            cancellation(2, south, ['WAC033'], sent_ago=30, references=(1,)),
            segment(3, north, ['WAC061'], sent_ago=30, references=(1,))]


@pytest.mark.parametrize('order', list(itertools.permutations(range(3))))
def test_split_updates_collectively_supersede_the_referenced_predecessor(order):
    features = split_predecessor()
    result = parse([features[k] for k in order], NOW)
    assert ids(result) == [features[2]['properties']['id']]
    assert result[0]['affectsStation'] is False


def test_collective_replacement_preserves_unreferenced_segment_of_same_event():
    features = split_predecessor()
    keep = segment(4, fx.OVER_STATION, ['WAC033'], sent_ago=600)
    result = parse(features + [keep], NOW)
    assert set(ids(result)) == {features[2]['properties']['id'], keep['properties']['id']}
    assert result[0]['id'] == keep['properties']['id']
    assert result[0]['affectsStation'] is True


@pytest.mark.parametrize('missing', ['ugc', 'geometry'])
def test_split_replacement_requires_geometry_but_allows_missing_ugc(missing):
    features = split_predecessor()
    for f in features:
        if missing == 'ugc':
            f['properties']['geocode'] = {}
        elif f is features[1]:
            f['geometry'] = None
    expected = [features[2]['properties']['id']]
    if missing == 'geometry':
        expected.insert(0, features[0]['properties']['id'])
    assert ids(parse(features, NOW)) == expected


def test_collective_county_coverage_cannot_justify_uncovered_shrinkage():
    features = split_predecessor()
    features[2]['geometry'] = fx.multipolygon(fx.big_ring(-122.3, 47.8, vertices=8, radius=.02))
    assert ids(parse(features, NOW)) == [features[0]['properties']['id'], features[2]['properties']['id']]


@pytest.mark.parametrize('action', ['CAN', 'EXP'])
@pytest.mark.parametrize('with_ugc', [False, True])
def test_ending_segments_collectively_cover_original_geometry(action, with_ugc):
    features = split_predecessor()
    vtec = features[2]['properties']['parameters']['VTEC']
    vtec[0] = vtec[0].replace('.CON.', '.' + action + '.')
    if not with_ugc:
        for f in features:
            f['properties']['geocode'] = {}
    for order in itertools.permutations(features):
        assert parse(list(order), NOW) == []
    # Equal county coverage cannot hide a gap between ending polygons.
    features[2]['geometry']['coordinates'][0][0][1] += .02
    features[2]['geometry']['coordinates'][0][-1][1] += .02
    assert ids(parse(features, NOW)) == [features[0]['properties']['id']]


def test_polygon_union_containment_checks_interior_gaps_and_wrap():
    square = [[0, 0], [4, 0], [4, 4], [0, 4]]
    # These triangles cover every edge of the square, but enclose a diamond gap.
    corners = [[[0, 0], [4, 0], [2, 1]], [[4, 0], [4, 4], [3, 2]],
               [[4, 4], [0, 4], [2, 3]], [[0, 4], [0, 0], [1, 2]]]
    assert not nw._rings_cover([square], corners)
    # Overlapping triangles introduce edge-intersection scan levels.
    assert nw._rings_cover([square], [[[0, 0], [8, 0], [0, 8]], [[4, 4], [-4, 4], [4, -4]]])
    across = [[179, 50], [-179, 50], [-179, 52], [179, 52]]
    south = [[179, 50], [-179, 50], [-179, 51], [179, 51]]
    north = [[179, 51], [-179, 51], [-179, 52], [179, 52]]
    assert nw._rings_cover([across], [north, south])


@pytest.mark.parametrize('invalid', ['older', 'unreferenced', 'incomplete_ugc', 'invalid_message'])
def test_incomplete_or_ineligible_split_does_not_supersede_predecessor(invalid):
    features = split_predecessor()
    can = features[1]['properties']
    if invalid == 'older':
        can['sent'] = fx.iso(NOW - 900)
    elif invalid == 'unreferenced':
        can['references'] = []
    elif invalid == 'incomplete_ugc':
        can['geocode']['UGC'] = ['WAC061']
    else:
        can['messageType'] = 'Ack'
    assert features[0]['properties']['id'] in ids(parse(features, NOW))


@pytest.mark.parametrize('order', list(itertools.permutations(range(3))))
def test_review_reproduction_cancel_references_only_nearby_con_segment(order):
    home = segment(1, fx.OVER_STATION, ['WAC033'], sent_ago=600)
    nearby = segment(2, fx.NEARBY, ['WAC061'], sent_ago=600)
    can = cancellation(3, fx.NEARBY, ['WAC061'], sent_ago=30, references=(2,))
    features = [home, nearby, can]
    result = parse([features[k] for k in order], NOW)
    assert ids(result) == [home['properties']['id']]
    assert result[0]['affectsStation'] is True


@pytest.mark.parametrize('references', [(), (1, 2), (2,)])
@pytest.mark.parametrize('same_ugc', [False, True])
def test_ending_scope_uses_geometry_even_for_shared_county_or_product_references(references, same_ugc):
    home = segment(1, fx.OVER_STATION, ['WAC033'], sent_ago=600)
    near_ugc = ['WAC033'] if same_ugc else ['WAC061']
    nearby = segment(2, fx.NEARBY, near_ugc, sent_ago=600)
    can = cancellation(3, fx.NEARBY, near_ugc, sent_ago=30, references=references)
    assert ids(parse([home, nearby, can], NOW)) == [home['properties']['id']]


def test_geometry_free_cancel_cannot_remove_polygons_using_ugc_or_references():
    home = segment(1, fx.OVER_STATION, ['WAC033'], sent_ago=600)
    nearby = segment(2, fx.NEARBY, ['WAC061'], sent_ago=600)
    can = cancellation(3, None, ['WAC061'], sent_ago=30, references=(1, 2))
    assert ids(parse([home, nearby, can], NOW)) == [home['properties']['id'], nearby['properties']['id']]


def test_an_older_reference_cannot_remove_a_newer_segment():
    con = segment(1, fx.OVER_STATION, ['WAC033'], sent_ago=30)
    can = cancellation(2, fx.OVER_STATION, ['WAC033'], sent_ago=600, references=(1,))
    assert ids(parse([con, can], NOW)) == [con['properties']['id']]


@pytest.mark.parametrize('covers', [False, True])
def test_continuation_replaces_only_its_referenced_and_covered_segment(covers):
    home = segment(1, fx.OVER_STATION, ['WAC033'], sent_ago=600)
    near = segment(2, fx.NEARBY, ['WAC061'], sent_ago=600)
    ring = fx.NEARBY if covers else fx.big_ring(-121.8, 48.1, vertices=8)
    update = segment(3, ring, ['WAC061'], sent_ago=30, references=(2,))
    expected = {home['properties']['id'], update['properties']['id']}
    if not covers:
        expected.add(near['properties']['id'])
    assert set(ids(parse([near, update, home], NOW))) == expected


def test_partial_geometry_or_ugc_cannot_end_a_whole_active_segment():
    con = segment(1, fx.OVER_STATION, ['WAC033', 'WAC061'], sent_ago=600)
    partial = cancellation(2, fx.OVER_STATION, ['WAC033'], sent_ago=30, references=(1,))
    assert ids(parse([con, partial], NOW)) == [con['properties']['id']]
    con['properties']['geocode']['UGC'] = ['WAC033']
    partial['geometry'] = fx.multipolygon(fx.big_ring(-122.3, 47.6, vertices=8, radius=.01))
    assert ids(parse([con, partial], NOW)) == [con['properties']['id']]


def test_containment_checks_edges_not_only_vertices_and_handles_wrap():
    # A triangle bridges the missing top of a U shape. Its vertices are inside.
    u = [[0, 0], [4, 0], [4, 4], [3, 4], [3, 1], [1, 1], [1, 4], [0, 4]]
    assert not nw._ring_covered([[.5, 3], [3.5, 3], [2, .5]], u)
    across = [[179, 50], [-179, 50], [-179, 52], [179, 52]]
    inside = [[179.5, 50.5], [-179.5, 50.5], [-179.5, 51.5], [179.5, 51.5]]
    assert nw._ring_covered(inside, across)


def test_twelve_distant_tornadoes_cannot_erase_nearby_severe_review_reproduction():
    far = [fx.feature(NOW, n=k+1, etn=k+1, office='KOUN',
                      ring=fx.big_ring(-98 + k*.1, 35, vertices=8, radius=.02)) for k in range(12)]
    near = fx.feature(NOW, n=50, etn=50, event='Severe Thunderstorm Warning', phen='SV', ring=fx.NEARBY)
    result = parse(far + [near], NOW)
    assert result[0]['id'] == near['properties']['id']
    assert len(result) == 13
    assert {i['id'] for i in result} == {f['properties']['id'] for f in far + [near]}


def test_large_outbreak_scales_geometry_budget_without_losing_reachable_components():
    storms = [fx.feature(NOW, n=k+1, etn=k+1, event='Severe Thunderstorm Warning', phen='SV',
                         ring=fx.big_ring(-100 + k % 10, 30 + (k//10)*.1, vertices=10)) for k in range(320)]
    # Components beyond the former six-ring cap must also remain reachable.
    multi = fx.feature(NOW, n=500, etn=500, geometry=fx.multipolygon(*[
        fx.big_ring(-122 + k*.1, 47.8, vertices=10, radius=.02) for k in range(30)]))
    result = parse(storms + [multi], NOW)
    assert len(result) == 321
    assert len(next(i for i in result if i['id'] == multi['properties']['id'])['polygon']) == 30
    assert all(len(r) >= 4 for i in result for r in i['polygon'])
    assert sum(len(r) for i in result for r in i['polygon']) <= 350 * nw.MIN_RING_VERTICES
    assert len(json.dumps([nw.public(i) for i in result]).encode()) < nw.MAX_PAYLOAD_BYTES


def test_nearby_and_station_warnings_lead_a_mixed_outbreak_independent_of_feed_order():
    local = [fx.feature(NOW, n=k+1, etn=k+1, event='Severe Thunderstorm Warning', phen='SV',
                        ring=fx.big_ring(-122.0, 47.8+k*.01, vertices=6, radius=.01)) for k in range(20)]
    far = fx.feature(NOW, n=40, etn=40, ring=fx.FAR)
    home = fx.feature(NOW, n=41, etn=41, ring=fx.OVER_STATION)
    for features in ([far, home] + local, local[::-1] + [home, far]):
        result = parse(features, NOW)
        assert len(result) == 22
        assert result[0]['id'] == home['properties']['id']
        assert result[-1]['id'] == far['properties']['id']
        assert [i['id'] for i in result[1:-1]] == [f['properties']['id'] for f in local]


def test_output_byte_ceiling_reports_failed_refresh_instead_of_partial_success(make_emitter, monkeypatch):
    net = Net(monkeypatch)
    now = time.time()
    first = fx.feature(now, ring=fx.OVER_STATION)
    net.answers.append(FakeResp(json.dumps(fx.collection(first))))
    e = emitter(make_emitter)
    reach, home, _, url = e._warnings_query()
    e._do_warnings(url, reach, home)
    good = e._warnings.payload(now)['items']
    monkeypatch.setattr(nw, 'MAX_PAYLOAD_BYTES', 1000)
    huge = fx.feature(now, n=2, ring=fx.NEARBY)
    huge['properties']['instruction'] = 'Official action text. ' * 500
    net.answers.append(FakeResp(json.dumps(fx.collection(first, huge))))
    e._do_warnings(url, reach, home)
    payload = e._warnings.payload(time.time())
    assert payload['items'] == good
    assert payload['refreshFailedAt'] is not None
    assert e._warnings.health(time.time())['failures'] == 1


def test_complete_official_instruction_survives_parse_and_tracker():
    text = 'First official instruction. ' * 20 + 'Final official instruction: avoid flooded roads.'
    f = fx.feature(NOW, ring=fx.OVER_STATION)
    f['properties']['instruction'] = text
    tracker = nw.Tracker()
    tracker.succeeded(NOW, parse([f], NOW), 'fixture')
    assert tracker.payload(NOW)['items'][0]['instruction'] == text


def test_distant_outbreak_cannot_consume_local_geometry_detail():
    far = [fx.feature(NOW, n=k+1, etn=k+1, ring=fx.big_ring(-100+k%10, 35, vertices=20)) for k in range(320)]
    ring = fx.big_ring(-122, 47.8, vertices=20, radius=.06)
    local = fx.feature(NOW, n=500, etn=500, event='Severe Thunderstorm Warning', phen='SV', ring=ring)
    result = parse(far + [local], NOW)
    assert result[0]['id'] == local['properties']['id']
    assert result[0]['polygon'] == [nw._published(nw.simplify(ring))]
    assert len(result) == 321
