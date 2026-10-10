"""Source warming survives cooldowns, interrupted rounds and warm tab returns."""
from pathlib import Path

import pytest

from lib import almanac_emit as ae
from lib import radar_engine
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401
from tests.test_radar_site_cache import scene, MRMS, SITE  # noqa: F401


def test_current_site_camera_precedes_zoom_neighbours(scene, multisite, hybrid):
    e, ctx = scene
    order = []
    multisite.failure = lambda site, url: order.append(('site', url))
    hybrid.failure = lambda req, _: order.append(('mosaic', req.full_url))
    e.radar._prefetch(MRMS, ctx)
    assert order[0][0] == 'site'
    keys = [k for k in e.radar._tiles if k[0] == SITE and k[4] == ctx['zoom']]
    assert keys
    assert all(radar_engine._radar_tile_path(k[0], k[1], k[3], k[4], k[5], k[6]).is_file() for k in keys)


def test_mosaic_cooldown_does_not_block_site_warm(scene, hybrid, multisite):
    e, ctx = scene
    e.radar._cooldowns[MRMS] = hybrid.mono + 60
    e.radar._prefetch(MRMS, ctx)
    assert any(k[0] == SITE for k in e.radar._tiles)
    assert not hybrid.calls
    assert e.radar._headroom_delay(MRMS, 1) == 60


def test_interrupted_site_round_resumes_missing_tiles(scene, multisite, tmp_path):
    e, ctx = scene
    multisite.failure = lambda site, url: (tmp_path/'radar_intent').write_text('2')
    with pytest.raises(radar_engine._RadarSuperseded):
        e.radar._prefetch(MRMS, ctx)
    key = (SITE, ctx['zoom'], ctx['center']['lat'], ctx['center']['lon'])
    assert key not in e.radar._prefetched
    multisite.failure = None
    ctx['preference_stamp'] = e.radar._preference_stamp()
    e.radar._prefetch(MRMS, ctx)
    assert key in e.radar._prefetched


def test_completed_round_repairs_deleted_disk_tile(scene, multisite):
    e, ctx = scene
    e.radar._prefetch(MRMS, ctx)
    key = next(k for k in e.radar._tiles if k[0] == SITE and k[4] == ctx['zoom'])
    path = radar_engine._radar_tile_path(key[0], key[1], key[3], key[4], key[5], key[6])
    path.unlink()
    e.radar._invalidate_tile((key[0],key[1],radar_engine._radar_stamp_text(key[3]),key[4],key[5],key[6]))
    e.radar._tiles.pop(key)
    multisite.calls.clear()
    e.radar._prefetch(MRMS, ctx)
    assert path.is_file()
    assert [kind for kind, *_ in multisite.calls] == ['tile']


def test_complete_mosaic_return_retries_site_warming(make_emitter, hybrid, multisite, tmp_path):
    hybrid.pin('mosaic')
    hybrid.view()
    e = make_emitter()
    e.radar._cooldowns[SITE] = hybrid.mono + 60
    e.radar._acquire()
    assert e.radar._inventory_valid(e.radar._result)
    assert e.radar._idle_context is not None
    assert not any(k[0] == SITE for k in e.radar._tiles)
    # The existing eight-frame shortcut used to return before this round.
    e.radar._cooldowns.clear()
    e.radar._request_times.clear()
    e.radar._acquire(view_started=True)
    assert e.radar._warm_pending
    e.radar._resume_warm()
    assert any(k[0] == SITE for k in e.radar._tiles)
    assert e.radar._result.source_mode == 'mosaic'


def test_older_site_volume_stages_complete_frames_before_publication(make_emitter, hybrid, multisite, tmp_path, monkeypatch):
    hybrid.pin('mosaic')
    e = make_emitter();e.radar._acquire()
    mosaic_ts = e.radar._result.ts_frame
    multisite.scans['KNEA'] = [mosaic_ts-60]
    hybrid.pin('site')
    seen = []
    def publish():
        r = e.radar._result
        if r.source_mode == 'site' and r.ts_frame is not None:
            seen.append((r.ts_frame, sum(f['complete'] for f in r.frames), len(r.frames)))
            raise radar_engine._RadarSuperseded('stop after first published measurement')
    monkeypatch.setattr(e.radar, '_emit_now', publish)
    e.radar._acquire()
    assert seen and seen[0][0] == mosaic_ts-60
    assert seen[0][1] >= min(4,seen[0][2])


def test_idle_warm_watcher_yields_to_new_intent_and_single_flight(scene, tmp_path, monkeypatch):
    e, ctx = scene
    e._runtime.running = True;e.radar._was_viewed = True
    e.radar._zoom_stamp = e.radar._preference_stamp()
    e.radar._warm_pending = True
    tasks = []
    monkeypatch.setattr(e.radar, '_spawn', lambda lane, work: tasks.append((lane, work)))
    e._runtime.inflight.add('radar');e.radar._check_zoom()
    assert not tasks and e.radar._warm_pending
    e._runtime.inflight.clear();e.radar._check_zoom();e.radar._check_zoom()
    assert len(tasks) == 1 and tasks[0] == ('radar', e.radar._resume_warm)
    tasks.clear();e.radar._warm_pending = True
    (tmp_path/'radar_zoom').write_text('9')
    requested = []
    monkeypatch.setattr(e.radar, '_acquire', lambda **args: requested.append(args))
    e.radar._check_zoom();assert len(tasks) == 1
    tasks[0][1]()
    assert requested == [dict(intent_triggered=True, view_started=False)]
    e._runtime.running = False
