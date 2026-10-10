"""View-start latency, retained hour and playback intent regressions."""
import json
import os
from pathlib import Path
from datetime import datetime, timezone
import subprocess
import time

import pytest
from lib import almanac_emit as ae
from lib import radar_engine
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_emitter_lifecycle import FakeClock




def test_view_transition_one_tick_and_single_flight(make_emitter, hybrid, tmp_path, monkeypatch):
    e=make_emitter(); e._runtime.running=True; e.radar._zoom_stamp=e.radar._preference_stamp()
    clock=FakeClock(); calls=[]
    monkeypatch.setattr(e.radar,'_spawn',lambda name,fn: calls.append(fn))
    clock.schedule_interval(e.radar._check_zoom,radar_engine.RADAR_INTENT_CHECK_SEC)
    clock.advance(.1); assert not calls
    hybrid.view(); clock.advance(.1); assert len(calls)==1
    for _ in range(30): hybrid.view(); clock.advance(.1)
    assert len(calls)==1
    (tmp_path/'radar_viewed').write_text(str(ae.time.time()-radar_engine.RADAR_VIEW_TTL))
    clock.advance(.1); e._runtime.inflight.add('radar'); hybrid.view(); clock.advance(.1)
    assert len(calls)==1 and e.radar._view_pending
    e._runtime.inflight.remove('radar'); clock.advance(.1)
    assert len(calls)==2 and not e.radar._view_pending
    # Real server sessions wake a return inside the 15-minute demand TTL.
    for since in (hybrid.now-600,hybrid.now):
        (tmp_path/'radar_viewing').write_text(json.dumps(dict(since=since,last=hybrid.now)))
        clock.advance(.1)
    assert len(calls)==4
    clock.advance(1); assert len(calls)==4


def test_unviewed_hour_accumulates_and_warm_view_has_zero_http(make_emitter, hybrid, tmp_path, monkeypatch):
    e=make_emitter()
    # Cadence passes fetch only newest; two hours demonstrate the steady bound.
    for i in range(62):
        if i: hybrid.latest+=120; hybrid.mono+=120
        hybrid.calls.clear(); e.radar._acquire(intent_triggered=False)
        assert e._build_payload()['radar']['completeFrameCount']==min(i+1,31)
        tile_calls=[c for c in hybrid.calls if '/mrms::' in c[2]]
        assert len(tile_calls)==12
        assert len(list(Path(radar_engine.RADAR_DIR).rglob('*.png'))) <= 8000
    files=list(Path(radar_engine.RADAR_DIR).rglob('*.png'))
    current=[p for p in files if hybrid.latest-3600<=datetime.strptime(p.parts[-4],'%Y%m%d%H%M').replace(tzinfo=timezone.utc).timestamp()<=hybrid.latest]
    assert len(current)==31*12
    hybrid.calls.clear(); hybrid.view(); e._runtime.running=True
    start=time.perf_counter(); e.radar._check_zoom()
    deadline=start+1
    while 'radar' in e._runtime.inflight and time.perf_counter()<deadline: time.sleep(.001)
    elapsed=(time.perf_counter()-start)*1000
    assert e._build_payload()['radar']['completeFrameCount']>=8
    assert elapsed<300 and not hybrid.calls
    print(f'WARM VIEW: {elapsed:.1f} ms, 31 complete frames, 0 HTTP')
    # A retained off-tab hour receives grace when its geometry is superseded.
    (tmp_path/'radar_viewed').unlink(); e.radar._acquire(intent_triggered=False)
    (tmp_path/'radar_zoom').write_text('7'); e.radar._acquire()
    assert all(p.exists() for p in current)
    hybrid.mono+=radar_engine.RADAR_CACHE_GRACE_SEC; e.radar._prune(e.radar._result)
    assert all(p.exists() for p in current)  # past protection expires; LRU evicts only when the cap needs space
    e.stop()
