"""Repair must preserve published site-scan identities and visible completeness."""
from copy import deepcopy

import pytest

from lib import almanac_emit as ae
from lib import radar_engine
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401


@pytest.mark.parametrize('damage', [False, True])
def test_history_rebuilds_when_requested_site_scan_pairs_change(make_emitter, hybrid, multisite, monkeypatch, damage):
    hybrid.view()
    emitter = make_emitter()
    monkeypatch.setattr(emitter.radar, '_prefetch', lambda *args: None)
    emitter.radar._acquire()
    previous = emitter.radar._result
    historical = deepcopy(previous.frames[0])
    assert historical['complete']
    assert historical['siteScans'][0] == dict(id='KMID', ts=hybrid.latest-720)
    # A late listing changes the nearest eligible neighbour for this OLD scan.
    multisite.scans['KMID'].insert(1, historical['ts'])
    if damage:
        key = next(k for k in emitter.radar._disk_inventory.records
                   if k[1] == 'KMID' and k[2] == radar_engine._radar_stamp_text(hybrid.latest-720))
        assert emitter.radar._invalidate_tile(key)
    emitter.radar._acquire(intent_triggered=False)
    repaired = next(f for f in emitter.radar._result.frames if f['ts'] == historical['ts'])
    assert repaired['complete']
    assert repaired['siteScans'] != historical['siteScans']
    assert next(p['ts'] for p in repaired['siteScans'] if p['id'] == 'KMID') == historical['ts']
    assert not any(emitter.radar._pending[k] for k in ('newest', 'four', 'eight'))


def test_partial_publications_count_exact_visible_completeness(make_emitter, hybrid, monkeypatch):
    emitter = make_emitter()
    seen = []
    monkeypatch.setattr(emitter.radar, '_emit_now', lambda: seen.append(deepcopy(emitter._build_payload()['radar'])))
    emitter.radar._acquire()
    measurements = [r for r in seen if r.get('tiles', {}).get('frames')]
    assert measurements
    for r in measurements:
        frames = r['tiles']['frames']
        complete = sum(f['levels'][str(r['tiles']['z'])] for f in frames)
        assert r['completeFrameCount'] == complete


def test_unchanged_site_discovery_does_not_reenter_frame_acquisition(make_emitter, hybrid, multisite, monkeypatch):
    hybrid.view()
    emitter = make_emitter()
    monkeypatch.setattr(emitter.radar, '_prefetch', lambda *args: None)
    emitter.radar._acquire()
    assert all(f['complete'] for f in emitter.radar._result.frames)
    builds = []
    fill = emitter.radar._fill_frame
    def counted(*args, **kwargs):
        builds.append(args[1])
        return fill(*args, **kwargs)
    monkeypatch.setattr(emitter.radar, '_fill_frame', counted)
    emitter.radar._acquire(intent_triggered=False, discovery=True)
    assert not builds, 'identical site listing reentered acquisition'
    assert emitter.radar._pass['outcome'] == 'unchanged'
