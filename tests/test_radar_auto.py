"""Automatic source decisions and adapter wiring. All transport is simulated."""
import json

import pytest

from lib import almanac_emit as ae, radar_auto as auto
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401
from tests.test_freshness_health import _load_serve


@pytest.mark.parametrize('zoom,showing,expected', [
    (4, None, 'mosaic'), (6, 'site', 'mosaic'), (7, None, 'mosaic'),
    (7, 'mosaic', 'mosaic'), (7, 'site', 'site'), (8, 'mosaic', 'site'),
    (9, None, 'site'), (10, 'mosaic', 'site')])
def test_zoom_table(zoom, showing, expected):
    assert auto.choose(zoom, showing, True, 1.) == expected


@pytest.mark.parametrize('available,coverage', [(False, 1.), (False, 0.), (True, .849999)])
@pytest.mark.parametrize('showing', [None, 'site', 'mosaic'])
def test_coverage_and_reporting_are_safety_guards(available, coverage, showing):
    expected = 'site' if showing == 'site' and available and coverage >= auto.STAY_COVERAGE else 'mosaic'
    assert auto.choose(10, showing, available, coverage, 0, 0) == expected


@pytest.mark.parametrize('age,moved,expected', [
    (None, 0, 'site'), (0, 0, 'mosaic'), (9.999, 1, 'mosaic'),
    (10, 0, 'site'), (0, 2, 'site'), (0, -2, 'site')])
def test_reverse_guard(age, moved, expected):
    assert auto.choose(8, 'mosaic', True, .85, age, moved) == expected


def test_downward_guard_and_cold_band():
    assert auto.choose(6, 'site', True, 1, 9, -1) == 'site'
    assert auto.choose(6, 'site', True, 1, 9, -2) == 'mosaic'
    assert auto.choose(7, None, True, 1, None, 0) == 'mosaic'


def test_spherical_union_coverage_overlap_and_antimeridian():
    bounds = dict(w=-1, e=1, s=-1, n=1)
    site = dict(lat=0, lon=0)
    assert auto.coverage_fraction(bounds, [], 230000) == 0
    assert auto.coverage_fraction(bounds, [site], 230000) == pytest.approx(1)
    half = auto.coverage_fraction(bounds, [dict(lat=0, lon=-1)], 110000)
    assert .3 < half < .5
    assert auto.coverage_fraction(bounds, [dict(lat=0, lon=-1)]*2, 110000) == half
    assert auto.coverage_fraction(dict(w=179, e=-179, s=-1, n=1), [dict(lat=0, lon=180)], 230000) == pytest.approx(1)
    assert auto.coverage_fraction(dict(w=-1, e=1, s=80, n=81), [dict(lat=80.5, lon=0)], 230000) == pytest.approx(1)


@pytest.fixture(autouse=True)
def real_auto(request):
    """The transport fixtures pin a source; these tests exercise Auto itself."""
    if 'multisite' in request.fixturenames:
        request.getfixturevalue('multisite')
    if 'hybrid' in request.fixturenames:
        request.getfixturevalue('hybrid').pin(None)


def intent(root, zoom, seq=1):
    record = dict(seq=seq, zoom=zoom, center='station')
    (root/'radar_intent').write_text(json.dumps(record))
    return record


def test_default_auto_uses_settled_intent_not_moving_activity(make_emitter, hybrid, multisite, tmp_path, monkeypatch):
    monkeypatch.setattr(auto, 'coverage_fraction', lambda *args: 1.)
    intent(tmp_path, 7)
    (tmp_path/'radar_activity').write_text(json.dumps(dict(zoom=10, moving=True, at=hybrid.now)))
    emitter = make_emitter(); emitter._do_radar()
    assert emitter._radar_result.source_mode == 'mosaic'
    assert 'sourcePref' not in emitter._build_payload()['radar']
    intent(tmp_path, 8, seq=2)
    emitter._do_radar()
    assert emitter._radar_result.source_mode == 'site'
    assert emitter._radar_result.tiles['camera']['zoom'] == 8
    assert 'source' not in emitter._radar_result.tiles['intent']
    intent(tmp_path, 7, seq=3)
    hybrid.mono += 11
    emitter._do_radar()
    assert emitter._radar_result.source_mode == 'site'
    intent(tmp_path, 6, seq=4)
    emitter._do_radar()
    assert emitter._radar_result.source_mode == 'mosaic'
    intent(tmp_path, 10, seq=5)
    emitter._do_radar()
    assert emitter._radar_result.source_mode == 'site'


@pytest.mark.parametrize('problem', ['dark', 'stale', 'refused', 'coverage'])
def test_auto_refuses_unsafe_site(make_emitter, hybrid, multisite, monkeypatch, tmp_path, problem):
    intent(tmp_path, 8)
    if problem == 'dark': multisite.scans['KNEA'] = []
    if problem == 'stale': multisite.scans['KNEA'] = [hybrid.now-ae.RADAR_SITE_MAX_AGE_SEC]
    if problem == 'coverage': monkeypatch.setattr(auto, 'coverage_fraction', lambda *args: .84)
    emitter = make_emitter()
    if problem == 'refused':
        original = emitter._radar_site_listing
        def listing(ctx, site):
            if site['id'] == 'KNEA':
                site.update(reporting=False, reason='scan unavailable', newestTs=None)
                return site['id'], (), False
            return original(ctx, site)
        monkeypatch.setattr(emitter, '_radar_site_listing', listing)
    emitter._do_radar()
    assert emitter._radar_result.source_mode == 'mosaic'
    assert not [c for c in multisite.calls if c[0] == 'tile']


def test_failed_auto_candidate_retains_published_frames(make_emitter, hybrid, multisite, monkeypatch, tmp_path):
    intent(tmp_path, 6)
    emitter = make_emitter(); emitter._do_radar()
    before = emitter._radar_result
    intent(tmp_path, 8, seq=2)
    def fail(ctx):
        assert ctx['staging_source'] == 'iem-nexrad-n0b'
        assert emitter._radar_result.frames == before.frames
        raise TimeoutError('candidate newest unavailable')
    monkeypatch.setattr(emitter, '_radar_site_frames', fail)
    emitter._do_radar()
    assert emitter._radar_result.frames == before.frames
    assert emitter._radar_result.source_mode == 'mosaic'
    assert emitter._radar_auto_switch is None  # failed attempts never reset dwell


def test_moving_or_duplicate_camera_reports_cannot_write(tmp_path, monkeypatch):
    server = _load_serve(monkeypatch, tmp_path, {})
    params = dict(radarSession=['auto-session-12345'], radarGeneration=['1'], radarHeartbeat=['1'], radarClaim=[''], radarClaimEpoch=['0'], radarCommit=['1'], radarPolicy=['manual'])
    activity = dict(moving=False, zoom=8, center=dict(lat=47, lon=-122))
    assert server._camera_transaction(activity, params)
    server._camera_persist_timer.cancel()
    accepted = server._read_radar_intent()
    params.update(radarGeneration=['2'], radarHeartbeat=['2'])
    assert not server._camera_transaction(dict(activity, moving=True), params)
    assert server._read_radar_intent() == accepted
    params.update(radarGeneration=['1'], radarHeartbeat=['3'])  # a repeated generation is a heartbeat
    server._camera_transaction(dict(activity, zoom=9), params)
    assert server._read_radar_intent() == accepted


def test_guard_deadline_wakes_existing_watcher(make_emitter, monkeypatch, tmp_path):
    emitter = make_emitter(); emitter._running = True
    emitter._radar_zoom_stamp = emitter._radar_preference_stamp()
    scheduled = []
    monkeypatch.setattr(emitter, '_spawn', lambda key, work: scheduled.append(key))
    emitter._check_radar_zoom()
    assert scheduled == []
    emitter._radar_auto_due = ae.time.monotonic()-1
    emitter._check_radar_zoom()
    assert scheduled == ['radar'] and emitter._radar_auto_due is None


def test_failed_closest_site_is_not_retried_per_auto_pass(make_emitter, hybrid, multisite, tmp_path):
    intent(tmp_path, 8)
    multisite.scans['KNEA'] = []
    emitter = make_emitter(); emitter._do_radar()
    multisite.calls.clear()
    emitter._do_radar()
    assert not [call for call in multisite.calls if call[0] == 'list']
    # Discovery after a scan cadence refreshes evidence and permits recovery.
    hybrid.mono += ae._RADAR_SOURCES['iem-nexrad-n0b']['cadence']
    multisite.scans['KNEA'] = [hybrid.latest]
    emitter._do_radar(discovery=True, intent_triggered=False)
    assert emitter._radar_result.source_mode == 'site'
