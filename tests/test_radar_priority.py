"""Loop, adjacent newest tiles, then continuously-viewed, paced deep history."""
import json
import re
from datetime import datetime, timezone

import pytest

from lib import almanac_emit as ae
from lib import radar_engine
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401

SOURCE = 'iem-mrms-lcref'


@pytest.fixture(autouse=True)
def isolate_tier_order_from_source_failover_clock(monkeypatch):
    # These tests jump 20 seconds inside a mocked request to mature deep-view
    # demand. Source-deadline/fallback behavior is covered by v4.9 real TLS tests.
    monkeypatch.setattr(radar_engine, 'RADAR_SOURCE_DEADLINE_SEC', 25)


def viewing(hybrid, tmp_path, since=None):
    hybrid.view()
    now = ae.time.time()
    (tmp_path/'radar_viewing').write_text(json.dumps(dict(
        since=hybrid.now if since is None else since, last=now)))


def tiles(calls):
    return [(m[1], int(m[2])) for c in calls
            if (m := re.search(r'mrms::lcref-(\d+)/(\d+)/', c[2]))]


def stamp(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y%m%d%H%M')


def mature_during_newest(hybrid, tmp_path):
    viewing(hybrid, tmp_path)
    def tick(*args):
        if hybrid.mono < 20:
            hybrid.mono = 20
            viewing(hybrid, tmp_path)
    hybrid.failure = tick


def test_request_order_loop_neighbours_deep_and_resume(make_emitter, hybrid, tmp_path, monkeypatch):
    # This test isolates mosaic tiers; cross-mode budgets have dedicated coverage.
    monkeypatch.setattr(radar_engine, '_NEXRAD_SITES', {})
    emitter = make_emitter()
    mature_during_newest(hybrid, tmp_path)
    retries = []
    emitter.radar._schedule_retry = lambda key, callback, delay, **kw: retries.append(delay)
    emitter.radar._acquire()
    newest = hybrid.latest
    expected = [(stamp(newest-120*i),8) for i in range(8) for _ in range(12)]
    expected += [(stamp(newest),8)]*18 + [(stamp(newest),7)]*4 + [(stamp(newest),9)]*4
    expected += [(stamp(newest-120*i),8) for i in range(8,10) for _ in range(12)]
    assert tiles(hybrid.calls) == expected
    assert sum(f['complete'] for f in emitter.radar._frames) == 10
    assert len(emitter.radar._request_times) == 157
    assert radar_engine.RADAR_REQUESTS_PER_MIN-len(emitter.radar._request_times) >= 14+60
    assert retries[-1] == 60
    assert emitter.radar._refresh['state'] == 'idle'
    assert not emitter.radar._negative
    # A retry with no capacity cannot consume any deep-history headroom.
    hybrid.calls.clear(); emitter.radar._acquire(intent_triggered=False)
    assert not tiles(hybrid.calls)
    assert len(emitter.radar._request_times) == 158  # scheduled metadata only
    # Natural expiry resumes the pending older slots, without re-warming this stamp.
    hybrid.mono += 60; viewing(hybrid, tmp_path); hybrid.calls.clear()
    emitter.radar._acquire(intent_triggered=False)
    assert tiles(hybrid.calls)[0] == (stamp(newest-120*10), 8)
    assert {z for _, z in tiles(hybrid.calls)} == {8}
    assert sum(f['complete'] for f in emitter.radar._frames) > 9
    assert len(emitter.radar._request_times) <= radar_engine.RADAR_REQUESTS_PER_MIN-74


def test_scheduled_new_stamp_repeats_tiers_before_deep(make_emitter, hybrid, tmp_path):
    emitter = make_emitter(); mature_during_newest(hybrid, tmp_path)
    emitter.radar._acquire()
    hybrid.latest += 120; hybrid.mono += 120
    viewing(hybrid, tmp_path); hybrid.calls.clear()
    emitter.radar._acquire(intent_triggered=False)
    calls = tiles(hybrid.calls)
    # History grids are warm; a new newest fills its margin before neighbours.
    assert calls[:30] == [(stamp(hybrid.latest),8)]*30
    assert calls[30:38] == [(stamp(hybrid.latest),7)]*4 + [(stamp(hybrid.latest),9)]*4
    assert calls[38:] and all(z==8 and t<stamp(hybrid.latest-7*120) for t,z in calls[38:])
    assert hybrid.calls[0][2] == radar_engine.RADAR_IEM_METADATA_URL


def test_press_during_deep_is_native_tile_cached(make_emitter, hybrid, tmp_path, monkeypatch):
    # This test isolates mosaic tiers; cross-mode budgets have dedicated coverage.
    monkeypatch.setattr(radar_engine, '_NEXRAD_SITES', {})
    emitter = make_emitter(); mature_during_newest(hybrid, tmp_path)
    advance = hybrid.failure
    changed = []
    def press(req, timeout):
        advance(req, timeout)
        if not changed and f'lcref-{stamp(hybrid.latest-8*120)}/8/' in req.full_url:
            changed.append(True)
            (tmp_path/'radar_zoom').write_text('7')
    hybrid.failure = press
    emitter.radar._acquire()
    assert changed and emitter.radar._restart and emitter.radar._refresh['state'] != 'superseded'
    assert 1 <= tiles(hybrid.calls).count((stamp(hybrid.latest-8*120), 8)) <= 4
    assert len([k for k in emitter.radar._tiles if k[3] == hybrid.latest and k[4] == 7]) == 4
    hybrid.failure = advance; hybrid.calls.clear()
    original = emitter.radar._publish_refresh
    published = []
    def publish(ctx, **changes):
        original(ctx, **changes)
        if changes.get('frameIndex') == 1:
            published.append((hybrid.mono, list(hybrid.calls)))
    monkeypatch.setattr(emitter.radar, '_publish_refresh', publish)
    start = hybrid.mono
    emitter.radar._acquire()
    assert emitter.radar._result.zoom == 7
    assert published and published[0][0] == start  # only cold tiles, no metadata or budget wait
    assert all(c[1]!='HEAD' and c[2]!=radar_engine.RADAR_IEM_METADATA_URL for c in published[0][1])
    assert 0 < tiles(hybrid.calls).count((stamp(hybrid.latest), 7)) <= 31


@pytest.mark.parametrize('reset', ['off_tab', 'gap', 'geometry', 'intent', 'bad_marker'])
def test_continuous_view_and_geometry_gate(make_emitter, hybrid, tmp_path, reset):
    emitter = make_emitter(); viewing(hybrid, tmp_path)
    emitter.radar._acquire()
    assert sum(f['complete'] for f in emitter.radar._frames) == 8
    ctx = dict(identity=emitter.radar._view_geometry[0], preference_stamp=emitter.radar._preference_stamp())
    hybrid.mono = 19.999; viewing(hybrid, tmp_path)
    assert 0 < emitter.radar._deep_view_delay(ctx) < .01
    hybrid.mono = 20; viewing(hybrid, tmp_path)
    assert emitter.radar._deep_view_delay(ctx) == 0
    if reset == 'off_tab':
        (tmp_path/'radar_viewing').unlink()
    elif reset == 'gap':
        hybrid.mono += radar_engine.RADAR_VIEW_POLL_GAP_SEC
    elif reset in ('geometry', 'intent'):
        (tmp_path/('radar_zoom' if reset == 'geometry' else 'radar_intent')).write_text('7' if reset == 'geometry' else '2')
        emitter.radar._acquire()
    else:
        (tmp_path/'radar_viewing').write_text('{')
    assert emitter.radar._deep_view_delay(ctx) is None
    viewing(hybrid, tmp_path, since=ae.time.time())
    ctx = dict(identity=emitter.radar._view_geometry[0], preference_stamp=emitter.radar._preference_stamp())
    assert emitter.radar._deep_view_delay(ctx) == 20


def test_off_tab_during_deep_retains_published_loop(make_emitter, hybrid, tmp_path):
    emitter = make_emitter(); mature_during_newest(hybrid, tmp_path)
    advance = hybrid.failure
    def off_tab(req, timeout):
        advance(req, timeout)
        if f'lcref-{stamp(hybrid.latest-8*120)}/8/' in req.full_url:
            (tmp_path/'radar_viewing').unlink(missing_ok=True)
    hybrid.failure = off_tab
    emitter.radar._acquire()
    assert emitter.radar._result.source_id == SOURCE
    assert sum(f['complete'] for f in emitter.radar._frames) == 8
    assert emitter.radar._refresh['state'] == 'idle'
    assert not emitter.radar._negative


def test_deep_transport_retries_preserve_atomic_floor(make_emitter, hybrid, tmp_path, monkeypatch):
    # This test isolates mosaic tiers; cross-mode budgets have dedicated coverage.
    monkeypatch.setattr(radar_engine, '_NEXRAD_SITES', {})
    emitter = make_emitter(); mature_during_newest(hybrid, tmp_path)
    advance = hybrid.failure
    retried = []
    def retry(req, timeout):
        advance(req, timeout)
        if f'lcref-{stamp(hybrid.latest-8*120)}/8/' in req.full_url:
            emitter.radar._session.on_retry(hybrid.mono+timeout)
            retried.append(True)
    hybrid.failure = retry
    emitter.radar._acquire()
    assert retried and emitter.radar._transport_retries
    assert len(emitter.radar._request_times) == 156
    assert emitter.radar._refresh['state'] == 'idle'
    assert sum(f['complete'] for f in emitter.radar._frames) == 9
    assert not emitter.radar._negative


def test_deep_does_not_evict_newest_neighbours(make_emitter, hybrid, tmp_path, monkeypatch):
    emitter = make_emitter(); mature_during_newest(hybrid, tmp_path)
    monkeypatch.setattr(radar_engine, 'RADAR_REQUESTS_PER_MIN', 1000)
    monkeypatch.setattr(radar_engine, 'RADAR_MAX_FRAME_BUILDS_PER_PASS', 31)
    # Previously visited geometry makes this pass exceed the production LRU size.
    for x in range(32):
        emitter.radar._tiles[(SOURCE, None, None, hybrid.latest-4000, 6, x, 1)] = hybrid.tile
    emitter.radar._acquire()
    assert sum(f['complete'] for f in emitter.radar._frames) == 31
    assert len(emitter.radar._tiles) == 400
    for zoom, count in ((7,4), (8,30), (9,4)):
        assert len([k for k in emitter.radar._tiles if k[3] == hybrid.latest and k[4] == zoom]) == count


def test_multisite_retains_eight_frame_cap(make_emitter, hybrid, multisite, tmp_path, monkeypatch):
    emitter = make_emitter(); mature_during_newest(hybrid, tmp_path)
    for site in ('KNEA', 'KMID'):
        multisite.scans[site] = [hybrid.latest-120*i for i in range(31)]
    monkeypatch.setattr(radar_engine, 'RADAR_REQUESTS_PER_MIN', 2000)
    emitter.radar._acquire()
    assert emitter.radar._result.source_id == 'iem-nexrad-n0b'
    assert len(emitter.radar._frames) == 8
    assert sum(f['complete'] for f in emitter.radar._frames) == 8
    assert not emitter.radar._negative


def test_each_settled_geometry_warms_next_neighbours_once(make_emitter, hybrid, tmp_path):
    emitter = make_emitter(); viewing(hybrid, tmp_path)
    emitter.radar._acquire()
    # The next geometry has another likely neighbour (zoom 6), at the SAME stamp.
    hybrid.mono = 60; viewing(hybrid, tmp_path)
    (tmp_path/'radar_zoom').write_text('7'); hybrid.calls.clear()
    emitter.radar._acquire()
    assert 0 < tiles(hybrid.calls).count((stamp(hybrid.latest), 7)) <= 31
    assert (stamp(hybrid.latest), 6) in tiles(hybrid.calls)
    hybrid.calls.clear(); emitter.radar._acquire(intent_triggered=True)
    assert not tiles(hybrid.calls)
