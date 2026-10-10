"""Exercise actual acquisition, retries, and promotion with simulated transport."""
import json

import pytest

from lib import almanac_emit as ae
from lib import radar_engine
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_attention_engine import active, tier, tile_requests  # noqa: F401


@pytest.mark.parametrize('name,hour,target,viewed', [
    ('warm', 14, 4, False), ('watch', 14, 8, False),
    ('warm', 14, 4, True), ('watch', 2, 1, False), ('live', 14, 8, False)])
def test_history_builds_exact_tier_target(make_emitter, hybrid, active, name, hour, target, viewed):
    e = make_emitter(); e._runtime.running = True
    tier(e, name, hour)
    if viewed: hybrid.view()
    e.radar._acquire(intent_triggered=False)
    assert sum(f['complete'] for f in e.radar._result.frames) == target
    assert not any(e.radar._pending.get(k) for k in ('newest', 'four', 'eight'))
    assert 'radar' not in e._runtime.retries


def test_quiet_clears_old_history_pending_and_does_not_retry(make_emitter, hybrid, active):
    e = make_emitter(); e._runtime.running = True
    tier(e, 'dormant')
    e.radar._pending = dict(newest=True, four=True, eight=True, optional=True)
    e.radar._acquire(intent_triggered=False)
    assert e.radar._pending == {}
    assert 'radar' not in e._runtime.retries


def test_boot_and_preference_changes_do_not_bypass_dormant(make_emitter, hybrid, active, tmp_path):
    e = make_emitter(); e._runtime.running = True
    tier(e, 'dormant'); e.radar._zoom_stamp = None
    e.radar._acquire()
    (tmp_path / 'radar_zoom').write_text('7')
    e.radar._acquire()
    assert not tile_requests(hybrid.calls)
    assert len(hybrid.calls) == 1  # changing preferences cannot bypass listing interval either


def test_rearming_quiet_discovery_keeps_absolute_deadline(make_emitter, hybrid, active):
    e = make_emitter(); e._runtime.running = True
    tier(e, 'rest'); e.radar._sentinel = dict(at=ae.time.time(), echo=False)
    e.radar._acquire(intent_triggered=False)
    due = e.radar._discovery.due
    hybrid.mono += 30
    e.radar._arm_discovery()
    assert e.radar._discovery.due == due


def test_weather_promotion_wakes_dormant_even_with_pass_inflight(make_emitter, hybrid, active):
    e = make_emitter(); e._runtime.running = True
    tier(e, 'dormant'); e.radar._attention.since -= 600
    e.radar._discovery.due = ae.time.time()+3600
    e._runtime.inflight.add('radar')
    callbacks = []
    e._runtime.schedule = lambda cb, delay, interval=False: callbacks.append((cb, delay))
    e.radar._attention_tick(dict(radar={}, obsTs=ae.time.time(), obsAgeSec=0, rainRateMm=1), ae.time.time(), None)
    wakes = [cb for cb, delay in callbacks if delay <= .1]
    assert wakes, callbacks
    wakes[0](0)
    assert e.radar._acquisition_pending
    assert e.radar._discovery_floor_until <= ae.time.time()+5   # the wakeup is prompt; the schedule's own due is its own


def test_view_start_promotes_before_intent_pass(make_emitter, hybrid, active, tmp_path):
    e = make_emitter(); e._runtime.running = True
    tier(e, 'dormant')
    hybrid.view()
    (tmp_path / 'radar_viewing').write_text(json.dumps(dict(since=ae.time.time(), last=ae.time.time())))
    e.radar._acquire(intent_triggered=True, view_started=True)
    assert e.radar._attention.tier == 'live'
    assert sum(f['complete'] for f in e.radar._result.frames) == 8


def test_waking_does_not_clear_on_partial_success(make_emitter, hybrid, active):
    e = make_emitter(); tier(e, 'warm')
    e.radar._waking_since = ae.time.time()-5
    e.radar._health.last_success = ae.time.time()
    e.radar._result = radar_engine._RADAR_NONE._replace(available=True, ts_frame=hybrid.latest,
        frames=({'ts': hybrid.latest, 'complete': False},))
    p = dict(radar={})
    e.radar._attention_tick(p, ae.time.time(), None)
    assert p['radar']['attention']['waking']


def test_quiet_cold_engine_remains_accessible_after_startup_timeout(make_emitter, hybrid, active):
    e = make_emitter(); tier(e, 'dormant')
    hybrid.mono += radar_engine.RADAR_STARTING_MAX_SEC+1
    e.radar._cache_ready.set()
    e.radar._acquire(intent_triggered=False)
    p = e._build_payload()['radar']
    assert p['available'] or p['starting'] or p['attention'].get('waiting')


def test_daytime_expansion_is_not_mistaken_for_unchanged_discovery(make_emitter, hybrid, active):
    e = make_emitter(); tier(e, 'watch', hour=2)
    e.radar._acquire(intent_triggered=False)
    assert sum(f['complete'] for f in e.radar._result.frames) == 1
    e.radar._local_hour = 14
    e.radar._acquire(intent_triggered=False, discovery=True)
    assert sum(f['complete'] for f in e.radar._result.frames) == 8


def test_demotion_during_history_yields_and_retires_pending(make_emitter, hybrid, active):
    e = make_emitter(); tier(e, 'watch', hour=14)
    original = e.radar._fill_frame
    def demote(*args, **kwargs):
        frame = original(*args, **kwargs)
        if e.radar._result.frames and args[1] != hybrid.latest:
            e.radar._attention.tier = 'dormant'
        return frame
    e.radar._fill_frame = demote
    e.radar._acquire(intent_triggered=False)
    assert e.radar._pass['outcome'] == 'superseded'
    calls = len(hybrid.calls)
    e.radar._acquire(intent_triggered=False)
    assert not e.radar._pending and 'radar' not in e._runtime.retries
    assert not tile_requests(hybrid.calls[calls:])


def test_quiet_worker_yields_to_new_view_at_tile_boundary(make_emitter, hybrid, active):
    e = make_emitter(); tier(e, 'rest')
    original = e.radar._request
    def wake(source, url, *args, **kwargs):
        raw = original(source, url, *args, **kwargs)
        if 'mrms::' in url:
            e.radar._attention.tier = 'live'
        return raw
    e.radar._request = wake
    e.radar._acquire(intent_triggered=False)
    assert e.radar._pass['outcome'] == 'superseded'
    assert e.radar._restart
    assert len(tile_requests(hybrid.calls)) == 1


def test_forced_dormant_has_no_tiles_even_with_recent_view(make_emitter, hybrid, active):
    e = make_emitter(); tier(e, 'dormant')
    e.radar._attention.forced = 'dormant'
    hybrid.view(); e.radar._acquire(intent_triggered=True)
    assert not tile_requests(hybrid.calls)


def test_same_second_complete_frame_ends_waking(make_emitter, hybrid, active):
    e = make_emitter(); tier(e, 'warm')
    e.radar._waking_since = ae.time.time()
    e.radar._acquire(intent_triggered=False)
    assert e.radar._current_complete(ae.time.time())
    assert not e._build_payload()['radar']['attention']['waking']


def test_sentinel_local_failures_are_backed_off(make_emitter, hybrid, active):
    e = make_emitter(); e._runtime.running = True; tier(e, 'rest')
    def fail(req, timeout):
        raise radar_engine.LocalTransportError('no route')
    hybrid.failure = fail
    e.radar._acquire(intent_triggered=False)
    assert e.radar._local_failure_streak == 1
    assert len(hybrid.calls) == 1  # failed listing does not fan out to sentinel
    assert e.radar._discovery_floor_until >= ae.time.time()+900   # the wakeup carries the floor, not the schedule's own due
    assert 'radar' not in e._runtime.retries
