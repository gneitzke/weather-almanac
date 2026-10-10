"""A history pass refused by the request gate must wait for real headroom.

2026-09-16: after a three-step zoom-out the gate refused a history frame
before its cost was priced, the pass yielded with needed=0, and the retry
fired 2 s later into the same full window: one metadata request per pass for
as long as the window stayed full, with the caption stuck on "Retrying view ·
work budget · next attempt now"."""
from lib import almanac_emit as ae
from lib import radar_engine
from tests.test_radar_hybrid import hybrid  # noqa: F401


def test_history_deferred_by_the_gate_waits_for_a_frame_of_headroom(make_emitter, hybrid):
    e = make_emitter()
    e._runtime.running = True
    hybrid.view()
    e.radar._acquire(intent_triggered=True)                 # newest frame lands
    assert e.radar._result.available and e.radar._result.frames
    # The gate refuses inside the history loop BEFORE the frame's cost is priced
    # (the checkpoint at the top of the loop), with the 60 s window nearly full.
    now = ae.time.monotonic()
    e.radar._request_times = [now - 1.0] * (radar_engine.RADAR_REQUESTS_PER_MIN - 1)
    e.radar._clear_retry()
    checkpoint = e.radar._checkpoint
    def refuse_once(ctx):
        checkpoint(ctx)
        if ctx.get('request_reserve') and not ctx.get('_refused'):
            ctx['_refused'] = True
            raise radar_engine._RadarBudget('radar request budget/cooldown')
    e.radar._checkpoint = refuse_once
    hybrid.latest += 120; hybrid.mono += 0.5
    e.radar._acquire(intent_triggered=True)
    err = e.radar._pass['error'] or ''
    assert e.radar._pass['outcome'] == 'deferred', (e.radar._pass['outcome'], err)
    assert 'radar request budget/cooldown' in err and '_history' in err, err
    assert 'needed=0' not in err, err
    assert 'radar' in e._runtime.retries
    delay = e.radar._next_retry - ae.time.time()
    assert delay > 2.5, (delay, err)                  # real headroom, not the 2 s fallback
