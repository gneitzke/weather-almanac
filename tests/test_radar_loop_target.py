"""The published radar contract says how many frames the engine is building.

Live bug (2026-10-09): a LAN browser on Radar showed "Refreshing · frame 4 of 8"
forever. The warm attention tier builds a four-frame loop, so the engine sat
idle by design, yet the payload listed eight frames (four of them incomplete,
never to be fetched) and frameTotal 8. The page waited for frames that would
never come. The engine now publishes radar.loopFrames (the target in force),
lists only the newest loopFrames slots plus frames already complete, and sizes
refresh.frameTotal / frameIndex to that loop.
"""
from datetime import timezone

import pytest

from lib import almanac_emit as ae
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_attention_engine import active, tier  # noqa: F401


def payload(e):
    return e._radar_payload(e._radar_result, ae.time.time(), timezone.utc, e._radar_refresh)


def complete_at_zoom(r):
    z = str(r['tiles']['z'])
    return [f['levels'][z] for f in r['tiles']['frames']]


def run(e, name, hybrid, viewed=False):
    tier(e, name)
    if viewed:
        hybrid.view()
    e._do_radar()
    # Live goes on to deep history after its loop and may yield for budget.
    assert e._radar_pass['outcome'] in (('ok', 'deferred') if name == 'live' else ('ok',)), e._radar_pass
    return payload(e)


def test_live_state_warm_idle_is_not_refreshing(make_emitter, hybrid, active):
    """The exact live state: warm tier, engine idle with four complete frames.
    Nothing incomplete may be listed, and every counter agrees on four."""
    e = make_emitter(); e._running = True
    r = run(e, 'warm', hybrid)
    assert len(e._radar_result.frames) > 8            # the hour holds many more slots
    assert r['loopFrames'] == r['refresh']['loopFrames'] == 4
    assert r['refresh']['state'] == 'idle'
    assert r['refresh']['frameTotal'] == 4 and r['refresh']['frameIndex'] == 4
    assert not any(r['refresh']['pending'].get(k) for k in ('newest', 'four', 'eight'))
    assert len(r['tiles']['frames']) == 4
    assert r['frameCount'] == len(e._radar_result.frames)   # candidate slots, as documented
    assert r['completeFrameCount'] == 4
    assert all(complete_at_zoom(r)), 'an incomplete frame the engine will not fetch was published'
    assert r['tiles']['frames'][-1]['ts'] == r['observedTs']


def test_unfetched_slots_are_withheld_but_complete_leftovers_stay(make_emitter, hybrid, active):
    """Trimming is a publication rule over the engine's own snapshot: the newest
    loopFrames slots (being completed) plus any older frame already complete."""
    e = make_emitter(); e._running = True
    run(e, 'warm', hybrid)
    snap = e._radar_result
    frames = [dict(f) for f in snap.frames]
    # An older frame left complete by an earlier, larger target; a slot inside
    # the target that is still incomplete (the engine is fetching it).
    frames[-7]['complete'] = True
    frames[-2]['complete'] = False
    snap = snap._replace(frames=tuple(frames))
    refresh = dict(e._radar_refresh, state='history')
    r = e._radar_payload(snap, ae.time.time(), timezone.utc, refresh)
    listed = [f['ts'] for f in r['tiles']['frames']]
    assert listed == [frames[-7]['ts']] + [f['ts'] for f in frames[-4:]]
    assert len(listed) == 5 and r['loopFrames'] == 4


def test_payload_without_a_target_lists_everything(make_emitter, hybrid, active):
    e = make_emitter(); e._running = True
    run(e, 'warm', hybrid)
    refresh = {k: v for k, v in e._radar_refresh.items() if k != 'loopFrames'}
    r = e._radar_payload(e._radar_result, ae.time.time(), timezone.utc, refresh)
    assert r['loopFrames'] is None
    assert len(r['tiles']['frames']) == len(e._radar_result.frames)


def test_warm_to_live_publishes_the_larger_loop_while_it_loads(make_emitter, hybrid, active, monkeypatch):
    e = make_emitter(); e._running = True
    warm = run(e, 'warm', hybrid)
    assert warm['loopFrames'] == 4
    # The tier rises; until a pass starts, nothing changes on the wire.
    tier(e, 'live')
    assert payload(e)['loopFrames'] == 4 and len(payload(e)['tiles']['frames']) == 4
    seen = []
    original = e._radar_publish_refresh
    def spy(ctx, snapshot=None, **changes):
        original(ctx, snapshot=snapshot, **changes)
        seen.append(payload(e))
    monkeypatch.setattr(e, '_radar_publish_refresh', spy)
    hybrid.view()
    e._do_radar()
    history = [r for r in seen if r['refresh']['state'] == 'history']
    assert history, [r['refresh'] for r in seen]
    # While the four extra frames load, the wire says 8 and lists them.
    loading = [r for r in history if r['refresh']['frameIndex'] < 8]
    assert loading and all(r['loopFrames'] == 8 and r['refresh']['frameTotal'] == 8
                           and len(r['tiles']['frames']) == 8 for r in loading)
    assert {r['refresh']['frameIndex'] for r in loading} >= {4}
    final = payload(e)
    assert final['refresh']['state'] == 'idle' and final['loopFrames'] == 8
    assert final['refresh']['frameIndex'] == final['refresh']['frameTotal'] == 8
    assert len(final['tiles']['frames']) == 8 and all(complete_at_zoom(final))


def test_live_to_warm_keeps_every_complete_frame(make_emitter, hybrid, active):
    e = make_emitter(); e._running = True
    live = run(e, 'live', hybrid, viewed=True)
    stamps = [f['ts'] for f in live['tiles']['frames'][-8:]]
    assert live['loopFrames'] == 8 and len(stamps) == 8
    tier(e, 'warm')
    assert [f['ts'] for f in payload(e)['tiles']['frames'][-8:]] == stamps   # before the pass
    warm = run(e, 'warm', hybrid)
    assert warm['loopFrames'] == 4
    assert warm['refresh']['frameTotal'] == 4 and warm['refresh']['frameIndex'] == 4
    assert warm['refresh']['state'] == 'idle'
    # The eight complete frames stay listed: nothing blanks or shrinks on screen.
    assert [f['ts'] for f in warm['tiles']['frames'][-8:]] == stamps
    assert all(complete_at_zoom(warm))


@pytest.mark.parametrize('name,hour,loop', [('warm', 14.0, 4), ('watch', 14.0, 8), ('watch', 2.0, 1), ('live', 2.0, 8)])
def test_loop_frames_follows_each_tier(make_emitter, hybrid, active, name, hour, loop):
    e = make_emitter(); e._running = True
    tier(e, name, hour)
    if name == 'live':
        hybrid.view()
    e._do_radar()
    r = payload(e)
    assert r['loopFrames'] == loop
    assert r['refresh']['frameTotal'] == loop
    assert all(complete_at_zoom(r))
