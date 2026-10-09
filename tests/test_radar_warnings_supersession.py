"""Fail-safe supersession: scope and polygon coverage are both required."""
import itertools
import json
import time

import pytest

from lib import nws_warnings as nw
from tests.fixtures import nws_warnings as fx
from tests.test_radar_warnings_remaining import NOW, cancellation, segment, split_predecessor
from tests.test_radar_warnings_review import ids, parse


def incomplete_union():
    features = split_predecessor()
    features[1]['geometry'] = fx.multipolygon(
        [[-122.5, 47.5], [-122.4, 47.5], [-122.4, 47.8], [-122.5, 47.8]])
    return features


def station_notch():
    features = split_predecessor()
    whole = [[-122.34, 47.4], [-121.74, 47.4], [-121.74, 47.8], [-122.34, 47.8]]
    can = [[-122.34, 47.4], [-121.74, 47.4], [-121.74, 47.65],
           [-122.34, 47.65], [-122.34, 47.62], [-122.32, 47.61], [-122.34, 47.60]]
    con = [[-122.34, 47.65], [-121.74, 47.65], [-121.74, 47.8], [-122.34, 47.8]]
    for feature, ring in zip(features, (whole, can, con)):
        feature['geometry'] = fx.multipolygon(ring)
    return features


def independent_segment():
    whole = [[-122.1, 47.8], [-121.7, 47.8], [-121.7, 48.2], [-122.1, 48.2]]
    south = [[-122.1, 47.8], [-121.7, 47.8], [-121.7, 48.0], [-122.1, 48.0]]
    north = [[-122.1, 48.0], [-121.7, 48.0], [-121.7, 48.2], [-122.1, 48.2]]
    return [segment(1, fx.OVER_STATION, ['WAC033'], sent_ago=600),
            segment(2, whole, ['WAC033', 'WAC061'], sent_ago=600),
            cancellation(3, south, ['WAC033'], sent_ago=30, references=(1, 2)),
            segment(4, north, ['WAC061'], sent_ago=30, references=(1, 2))]


REPRODUCTIONS = [
    pytest.param(factory, order, id=f'{factory.__name__}-{"".join(map(str, order))}')
    for factory, size in ((incomplete_union, 3), (independent_segment, 4), (station_notch, 3))
    for order in itertools.permutations(range(size))
]


@pytest.mark.parametrize('factory,order', REPRODUCTIONS)
def test_incomplete_or_unrelated_union_keeps_station_predecessor(factory, order):
    features = factory()
    old = nw._record(features[0])
    assert all(not nw._supersedes(nw._record(f), old) for f in features[1:])
    result = parse([features[k] for k in order], NOW)
    assert ids(result) == [features[0]['properties']['id'], features[-1]['properties']['id']]
    assert result[0]['affectsStation'] is True
    assert result[1]['affectsStation'] is False


@pytest.mark.parametrize('action', ['CAN', 'CON', 'EXT'])
@pytest.mark.parametrize('gap,removed', [(0, True), (.0002, True), (.0008, False), (.008, False)])
def test_union_rounding_tolerance_is_bounded_for_endings_and_continuations(action, gap, removed):
    features = split_predecessor()
    # 0.0002 / 0.4 = 0.05% missing; 0.0008 / 0.4 = 0.2% missing.
    north = [[-122.5, 47.7 + gap], [-122.1, 47.7 + gap], [-122.1, 47.9], [-122.5, 47.9]]
    features[2]['geometry'] = fx.multipolygon(north)
    vtec = features[2]['properties']['parameters']['VTEC']
    vtec[0] = vtec[0].replace('.CON.', f'.{action}.')
    result = ids(parse(features, NOW))
    assert (features[0]['properties']['id'] not in result) is removed


def test_area_union_does_not_double_count_overlap_or_lose_small_components():
    square = [[0, 0], [4, 0], [4, 4], [0, 4]]
    half = [[0, 0], [2, 0], [2, 4], [0, 4]]
    assert not nw._rings_cover([square], [half, half, half])
    island = [[5, 0], [5.01, 0], [5.01, .01], [5, .01]]
    assert not nw._rings_cover([square, island], [square])
    assert nw._rings_cover([square, island], [square, island])
    assert not nw._rings_cover([square], [])
    assert not nw._rings_cover([], [square])
    assert not nw._rings_cover([[[0, 0], [1, 0], [2, 0]]], [square])


@pytest.mark.parametrize('action', ['CAN', 'CON', 'EXT'])
def test_disjoint_county_update_cannot_contribute_even_when_its_geometry_covers(action):
    features = split_predecessor()
    features[0]['properties']['geocode']['UGC'] = ['WAC033']
    vtec = features[2]['properties']['parameters']['VTEC']
    vtec[0] = vtec[0].replace('.CON.', f'.{action}.')
    # The southern CAN is in scope but partial. Northern WAC061 geometry
    # completes the polygon union, yet is out of scope for THIS predecessor.
    assert features[0]['properties']['id'] in ids(parse(features, NOW))


@pytest.mark.parametrize('action', ['CAN', 'CON', 'EXT'])
@pytest.mark.parametrize('references', [(), (1,)])
def test_single_update_also_requires_full_geometry(action, references):
    old, can, _ = split_predecessor()
    old['properties']['geocode']['UGC'] = ['WAC033']
    update = fx.feature(NOW, n=4, ring=fx.OVER_STATION, message='Update',
                        sent_ago=10, references=references, vtec_action=action)
    assert old['properties']['id'] in ids(parse([old, update], NOW))
    update['geometry'] = old['geometry']
    assert old['properties']['id'] not in ids(parse([old, update], NOW))


@pytest.mark.parametrize('action', ['CON', 'EXT'])
@pytest.mark.parametrize('order', list(itertools.permutations(range(3))))
def test_later_continuation_after_cancel_survives_in_every_order(action, order):
    old = segment(1, fx.OVER_STATION, ['WAC033'], sent_ago=900)
    can = cancellation(2, fx.OVER_STATION, ['WAC033'], sent_ago=600, references=(1,))
    later = fx.feature(NOW, n=3, ring=fx.OVER_STATION, message='Update',
                       sent_ago=10, references=(2,), vtec_action=action)
    features = [old, can, later]
    result = parse([features[k] for k in order], NOW)
    assert ids(result) == [later['properties']['id']]
    assert result[0]['affectsStation'] is True


def test_area_allowance_applies_only_when_station_is_covered_exactly():
    old, can, con = [nw._record(f)['rings'] for f in station_notch()]
    assert nw._rings_cover(old, can + con)  # only 0.0833% is missing
    assert not nw._rings_cover(old, can + con, station=(-122.33, 47.61))
    assert nw._rings_cover(old, can + con, station=(-122.30, 47.61))
    # Neither area tolerance nor an edge-distance epsilon may bridge this gap.
    square = [[0, 0], [1, 0], [1, 1], [0, 1]]
    almost = [[1e-12, 0], [1, 0], [1, 1], [1e-12, 1]]
    assert not nw._rings_cover([square], [almost], station=(5e-13, .5))
    assert nw._rings_cover([square], [almost], station=(1e-12, .5))
    # Same exact check at the antimeridian.
    across = [[179, 0], [-179, 0], [-179, 1], [179, 1]]
    left = [[179, 0], [179.9999, 0], [179.9999, 1], [179, 1]]
    right = [[-179.9999, 0], [-179, 0], [-179, 1], [-179.9999, 1]]
    assert not nw._rings_cover([across], [left, right], station=(-180, .5))


def outbreak_pairs(vertices=1500, count=50):
    features = []
    for k in range(count):
        ring = fx.big_ring(-100 + (k % 10) * .4, 35 + (k // 10) * .4, vertices=vertices)
        old = fx.feature(NOW, n=2*k+1, etn=k+1, ring=ring, sent_ago=600)
        new = fx.feature(NOW, n=2*k+2, etn=k+1, ring=ring, sent_ago=30,
                         message='Update', vtec_action='CON', references=(2*k+1,))
        features.extend((old, new))
    return features


def test_50_pairs_of_1500_vertices_finish_within_ci_wall_time():
    body = json.dumps(fx.collection(*outbreak_pairs()), separators=(',', ':')).encode()
    assert len(body) == 2_984_114
    assert len(body) < nw.MAX_BODY_BYTES
    start = time.perf_counter()
    result = parse(json.loads(body)['features'], NOW)
    elapsed = time.perf_counter() - start
    assert len(result) == 100  # uncertainty retains every predecessor
    assert elapsed < 5.0, f'outbreak parse took {elapsed:.3f}s'


def test_dense_update_exhausts_budget_before_any_edge_pairs():
    old, new = [nw._record(f) for f in outbreak_pairs(count=1)]
    budget = nw._GeometryBudget()
    assert not nw._replacement_covers([new], old, budget=budget)
    assert budget.remaining == 0
    square = [[0, 0], [1, 0], [1, 1], [0, 1]]
    assert not nw._rings_cover([square], [square], budget=budget)


def test_feed_budget_is_shared_and_fresh_for_each_feed(monkeypatch):
    monkeypatch.setattr(nw, 'SUPERSESSION_WORK_BUDGET', 300)
    features = outbreak_pairs(vertices=4, count=3)
    # First exact pair fits; exhaustion keeps all remaining predecessors.
    expected = {f['properties']['id'] for f in features[1:]}
    assert set(ids(parse(features, NOW))) == expected
    assert set(ids(parse(features, NOW))) == expected


def test_scanline_work_also_exhausts_budget():
    square = [[0, 0], [1, 0], [1, 1], [0, 1]]
    budget = nw._GeometryBudget(70)  # unwrap + pairs fit, integration doesn't
    assert not nw._rings_cover([square], [square], budget=budget)
    assert budget.remaining == 0


def test_dense_predecessor_uses_outer_bound_without_area_allowance():
    dense = fx.big_ring(0, 0, vertices=64)
    budget = nw._GeometryBudget(10_000_000)
    assert not nw._rings_cover([dense], [dense], budget=budget)
    assert budget.remaining > 0  # conservative bound, not exhaustion
    w, e = min(p[0] for p in dense), max(p[0] for p in dense)
    s, n = min(p[1] for p in dense), max(p[1] for p in dense)
    outer = [[w, s], [e, s], [e, n], [w, n]]
    assert nw._rings_cover([dense], [outer])
    # Applying the 0.1% allowance to an enlarged predecessor is unsafe.
    almost_outer = [[w + .0001, s], [e, s], [e, n], [w + .0001, n]]
    assert not nw._rings_cover([dense], [almost_outer])
