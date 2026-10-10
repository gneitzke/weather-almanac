"""Auto is the only radar source policy. Engine, server and launcher sides.
Simulated transport and temp directories only; nothing leaves the machine."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lib import almanac_emit as ae, radar_auto as auto
from lib import radar_engine
from tests.test_freshness_health import _load_serve
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401
from tests.test_radar_remote_serve import A, camera, request, server  # noqa: F401

RETIRED_PAYLOAD = ('sourcePref', 'sitePreferred', 'sourceFallback', 'siteResumeZoom')


# --- the default view is the high-resolution one -----------------------------------

def test_auto_picks_site_at_the_default_kiosk_zoom_with_a_reporting_site(make_emitter, hybrid, multisite, tmp_path):
    hybrid.pin(None)  # the real policy, no pinned verdict
    assert not any((tmp_path/name).exists() for name in ('radar_zoom', 'radar_intent', 'radar_source'))
    emitter = make_emitter(); emitter.radar._acquire()
    snap = emitter.radar._result
    assert snap.zoom_desired is None and snap.zoom_auto_level == radar_engine._radar_zoom_for(47.61) == auto.UP_ZOOM
    assert snap.source_mode == 'site' and snap.site_id == 'KNEA'
    radar = emitter._build_payload()['radar']
    assert radar['sourceMode'] == 'site' and not set(RETIRED_PAYLOAD) & set(radar)
    assert 'reason' not in radar['refresh']


@pytest.mark.parametrize('lat,lon', [(47.742, -121.986), (39.74, -104.99), (41.88, -87.63)])  # Duvall, Denver, Chicago
def test_real_site_geometry_puts_the_default_view_on_site(lat, lon):
    """Numbers, not a retune: the latitude-auto zoom is UP_ZOOM and the nearest
    radar alone covers more than MIN_COVERAGE of the default viewport."""
    zoom = radar_engine._radar_zoom_for(lat)
    assert zoom == auto.UP_ZOOM
    _, _, bounds, _ = radar_engine._radar_viewport(lat, lon, zoom, radar_engine.RADAR_VIEWPORT_W, radar_engine.RADAR_VIEWPORT_H)
    nearest = radar_engine._radar_nexrad(lat, lon, 'mi')
    sites = [s for s in radar_engine._radar_sites((lat, lon), bounds)[0] if s['id'] == nearest['id']]
    coverage = auto.coverage_fraction(bounds, sites, radar_engine.RADAR_SITE_RANGE_METERS)
    assert coverage >= auto.MIN_COVERAGE
    assert auto.choose(zoom, None, True, coverage) == 'site'


def test_a_pre_upgrade_intent_record_cannot_choose_the_source(make_emitter, hybrid, multisite, tmp_path):
    hybrid.pin(None)
    (tmp_path/'radar_intent').write_text(json.dumps(dict(seq=7, zoom=5, source='site', sourceAcceptedAt=1., center='station')))
    emitter = make_emitter(); emitter.radar._acquire()
    assert emitter.radar._read_intent()['zoom'] == 5
    assert emitter.radar._result.source_mode == 'mosaic'  # zoom 5 is Region, whatever the old record says


# --- old clients ---------------------------------------------------------------------

@pytest.mark.parametrize('values', [['site'], ['mosaic'], ['auto'], ['bogus'], ['site', 'mosaic'], ['']])
def test_old_radar_source_params_are_ignored(server, tmp_path, values):
    def commit(generation, **extra):
        params = {k: v for k, v in camera(generation=generation).items() if k != 'radarSource'}
        return request(server, '127.0.0.1', **params, **extra)
    plain = commit(1)
    assert json.loads(plain['X-Radar-Intent'])['session'] == A
    reference = server._read_radar_intent()
    old = commit(2, radarSource=values)
    intent = server._read_radar_intent()
    assert json.loads(old['X-Radar-Intent'])['acceptedGeneration'] == 2, 'an old page was refused'
    assert 'source' not in intent and 'sourceAcceptedAt' not in intent
    assert {k: v for k, v in intent.items() if k not in ('seq', 'generation', 'acceptedAt')} == \
           {k: v for k, v in reference.items() if k not in ('seq', 'generation', 'acceptedAt')}
    server._camera_persist_timer.function(); server._flush_preferences()
    assert not (tmp_path/'radar_source').exists()


def test_old_legacy_poll_with_a_source_is_answered_and_ignored(server, tmp_path):
    request(server, '127.0.0.1', radarSeq=9, radarZoom=7, radarSource='site', radarCenter='station')
    server._flush_preferences()
    assert server._read_radar_intent() == dict(seq=9, zoom=7, center='station')
    assert not (tmp_path/'radar_source').exists()
    # Only a source, from a LAN browser, with no camera: a plain read.
    assert request(server, '192.168.1.20', radarSource='site', touch=1)
    assert not (tmp_path/'radar_source').exists()


# --- retired files -------------------------------------------------------------------

def _retired_state(tmp_path):
    durable = tmp_path/'state'/'wfpiconsole'; durable.mkdir(parents=True)
    (durable/'radar_source').write_text('site\n')
    (tmp_path/'radar_source').symlink_to(durable/'radar_source')
    for name in ('.radar-lease-presence-0123456789abcdef01234567', '.radar-lease-source-0123456789abcdef01234567', '.radar-lease.lock'):
        (tmp_path/name).write_text('1000.0')
    (tmp_path/'radar_zoom').write_text('8\n')
    return durable


def test_a_stale_radar_source_marker_is_removed_at_startup(tmp_path, monkeypatch):
    durable = _retired_state(tmp_path)
    module = _load_serve(monkeypatch, tmp_path, {'ts': 1})
    module._remove_retired_markers()
    assert not os.path.lexists(tmp_path/'radar_source') and not (durable/'radar_source').exists()
    assert not list(tmp_path.glob('.radar-lease*'))
    assert (tmp_path/'radar_zoom').read_text() == '8\n'  # live preferences are untouched


def test_a_plain_marker_goes_and_a_foreign_link_target_stays(tmp_path, monkeypatch):
    other = tmp_path/'elsewhere'; other.write_text('keep')
    (tmp_path/'radar_source').symlink_to(other)
    module = _load_serve(monkeypatch, tmp_path, {'ts': 1})
    module._remove_retired_markers()
    assert not os.path.lexists(tmp_path/'radar_source') and other.read_text() == 'keep'
    (tmp_path/'radar_source').write_text('mosaic')
    module._remove_retired_markers()
    assert not (tmp_path/'radar_source').exists()


def test_the_server_entry_point_removes_the_marker(tmp_path):
    durable = _retired_state(tmp_path)
    (tmp_path/'wx.json').write_text('{"ts": 1}')
    script = r'''
import os, runpy, signal, sys, threading, time
def stop():
    for _ in range(500):
        module = sys.modules.get('__main__')
        if hasattr(module, 'Server') and not os.path.lexists(os.path.join(sys.argv[2], 'radar_source')):
            break
        time.sleep(.01)
    os.kill(os.getpid(), signal.SIGTERM)
threading.Thread(target=stop, daemon=True).start()
runpy.run_path(sys.argv[1], run_name='__main__')
'''
    env = dict(os.environ, WFP_DATA=str(tmp_path/'wx.json'), WFP_WEB=str(tmp_path), WFP_PORT='0', WFP_BIND='127.0.0.1')
    result = subprocess.run([sys.executable, '-c', script, str(Path('design/almanac/kiosk/serve.py').resolve()), str(tmp_path)],
                            env=env, capture_output=True, text=True, timeout=30, cwd=str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert not os.path.lexists(tmp_path/'radar_source') and not (durable/'radar_source').exists()
    assert not list(tmp_path.glob('.radar-lease*'))


def test_the_launcher_no_longer_links_radar_source(tmp_path):
    runtime = tmp_path/'runtime'; runtime.mkdir()
    state = tmp_path/'state'/'wfpiconsole'; state.mkdir(parents=True)
    (state/'radar_source').write_text('site\n')
    (runtime/'radar_source').symlink_to(state/'radar_source')
    script = Path('design/almanac/kiosk/almanac-kiosk.sh').read_text()
    setup = script[script.index('RADAR_STATE='):script.index('cp -f "$APP/design/almanac/console_live.html"')]
    env = dict(os.environ, XDG_STATE_HOME=str(tmp_path/'state'), DATA_DIR=str(runtime))
    subprocess.run(['bash', '-c', setup], env=env, check=True, capture_output=True, text=True)
    assert not os.path.lexists(runtime/'radar_source') and not (state/'radar_source').exists()
    assert (runtime/'radar_zoom').is_symlink()  # durable zoom still survives a reboot


def test_no_manual_source_machinery_remains():
    assert not hasattr(auto, 'source_preference') and not hasattr(auto, 'MANUAL_HOLD_SEC')
    assert not any(name.startswith('_lease') for name in vars(auto))
    serve = Path('design/almanac/kiosk/serve.py').read_text()
    for name in ('_write_radar_source', '_expire_radar_source', '_persist_runtime', "'radarSource'"):
        assert name not in serve, name
    emitter = Path('lib/almanac_emit.py').read_text() + Path('lib/radar_engine.py').read_text()
    for name in ('source_pref', '_radar_refuse_dark_site', '_refuse_dark_site', 'site-zoom-floor', 'site-not-reporting', *RETIRED_PAYLOAD):
        assert name not in emitter, name
