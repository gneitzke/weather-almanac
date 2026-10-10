"""Scheduled scans keep the published hour, including during slow native I/O."""
from datetime import timezone
from pathlib import Path

import pytest

from lib import almanac_emit as ae
from lib import radar_engine
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401


@pytest.mark.parametrize('site,spacing', [(False, 120), (True, 120), (True, 600)])
@pytest.mark.parametrize('delay', [0, 25])
def test_first_publish_slides_warm_hour(make_emitter, hybrid, multisite, tmp_path, monkeypatch, site, spacing, delay):
    count = 3600 // spacing + 1
    hybrid.pin('site' if site else 'mosaic')
    if site:
        multisite.colors['KFAR'] = (12, 145, 16, 255)
        for name in multisite.scans:
            multisite.scans[name] = [hybrid.latest-i*spacing for i in range(count-1, -1, -1)]
    e = make_emitter()
    contexts = []
    publish = e.radar._publish_refresh
    def capture(ctx, **changes):
        contexts.append(ctx)
        publish(ctx, **changes)
    monkeypatch.setattr(e.radar, '_publish_refresh', capture)
    monkeypatch.setattr(e.radar, '_prefetch', lambda *args: None)
    e.radar._acquire()
    ctx = contexts[-1]
    snap = e.radar._result
    source = snap.source_id
    # Seed a complete cached hour with actual immutable native tiles, including
    # exact per-site pairs from the previously published scans.
    frames = []
    for ts in range(hybrid.latest-3600, hybrid.latest+1, spacing):
        pairs = [(p['id'], ts) for p in snap.frames[-1]['siteScans']]
        f = radar_engine._radar_frame(source, ts, ctx, pairs)
        f['complete'] = True
        for name, scan in pairs or [(None, ts)]:
            for x, y, *_ in radar_engine._radar_site_tiles(ctx, name):
                dest = radar_engine._radar_tile_path(source, name, scan, ctx['zoom'], x, y)
                if not dest.exists():
                    origin = next(Path(radar_engine.RADAR_DIR).glob('t/*/*/*/*/*/*/*.png'))
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(origin.read_bytes())
                    e.radar._disk_inventory.add((source,name,radar_engine._radar_stamp_text(scan),ctx["zoom"],x,y),dest,dest.stat().st_size,radar_engine._radar_tile_metadata(dest,source))
        frames.append(f)
    e.radar._result = snap._replace(frames=tuple(frames), tiles=radar_engine._radar_tile_manifest(source, frames, ctx))
    previous = e.radar._result
    hybrid.latest += spacing; hybrid.mono += spacing
    if site:
        # Newly listed secondary scans must not rewrite old siteScans identities.
        for name in multisite.scans:
            multisite.scans[name].append(hybrid.latest if name == 'KNEA' else hybrid.latest-60)
    seen = []; held = []
    def observe():
        s = e.radar._result
        seen.append(s)
        assert len(s.frames) >= count-1
    monkeypatch.setattr(e.radar, '_emit_now', observe)
    batch = e.radar._tile_batch
    delayed = False
    def slow(*args, **kwargs):
        nonlocal delayed
        pause = delay if not delayed else 0
        delayed = True
        for _ in range(pause):
            hybrid.mono += 1
            s = e.radar._result
            held.append(s)
            assert len(s.frames) == count and sum(f['complete'] for f in s.frames) == count-1
        yield from batch(*args, **kwargs)
    monkeypatch.setattr(e.radar, '_tile_batch', slow)
    e.radar._acquire(intent_triggered=False)
    advanced = [s for s in seen if s.frames and s.frames[-1]['ts'] == hybrid.latest]
    assert advanced
    first = advanced[0]
    payload = e.radar._payload(first, ae.time.time(), timezone.utc)
    assert (payload['frameCount'], payload['completeFrameCount']) == (count, count-1)
    assert not first.frames[-1]['complete']
    assert not first.tiles['frames'][-1]['levels'][str(first.zoom)]
    assert first.frames[:-1] == previous.frames[1:]
    assert all(len(s.frames) == count for s in advanced)
    assert all(s.frames[:-1] == previous.frames[1:] for s in advanced)
    if delay:
        # The existing primary deadline is 25s. Its budget exit keeps the full
        # pending window; the next scheduled pass completes that scan in place.
        hybrid.mono += 60
        e.radar._acquire(intent_triggered=False)
    assert e.radar._result.frames[-1]['complete']
    assert len(held) >= delay


@pytest.mark.parametrize('change', ['cold', 'zoom', 'center', 'revision', 'legend', 'source', 'site'])
def test_window_identity_reset(make_emitter, hybrid, tmp_path, monkeypatch, change):
    e = make_emitter(); contexts = []
    publish = e.radar._publish_refresh
    def capture(ctx, **changes):
        contexts.append(ctx); publish(ctx, **changes)
    monkeypatch.setattr(e.radar, '_publish_refresh', capture)
    e.radar._acquire(); ctx = dict(contexts[-1]); source = e.radar._result.source_id
    assert e.radar._sliding_frames(source, hybrid.latest+120, ctx)
    if change == 'cold': e.radar._result = radar_engine._RADAR_NONE
    elif change == 'zoom': ctx['zoom'] -= 1
    elif change == 'center': ctx['bounds'] = dict(ctx['bounds'], n=0)
    elif change == 'revision': monkeypatch.setattr(radar_engine, '_radar_render_revision', lambda smooth=False: 'new-revision')
    elif change == 'legend': e.radar._result = e.radar._result._replace(legend={'id':'changed'})
    elif change == 'source': source = 'rainviewer'
    elif change == 'site': e.radar._result = e.radar._result._replace(site_id='different')
    assert not e.radar._sliding_frames(source, hybrid.latest+120, ctx)


def test_cold_and_intent_geometry_first_publish(make_emitter, hybrid, tmp_path, monkeypatch):
    e = make_emitter(); seen = []
    monkeypatch.setattr(e.radar, '_emit_now', lambda: seen.append(e.radar._result))
    e.radar._acquire()
    first = next(s for s in seen if s.frames)
    assert len(first.frames) == 1 and not first.frames[0]['complete']
    old = e.radar._result
    assert len(old.frames) == 31
    (tmp_path/'radar_zoom').write_text('7')
    seen.clear(); e.radar._acquire(intent_triggered=True)
    first = next(s for s in seen if s.frames and s.zoom == 7)
    assert len(first.frames) == 1 and not first.frames[0]['complete']
    assert first.bounds != old.bounds
    assert first.ts_frame == old.ts_frame
