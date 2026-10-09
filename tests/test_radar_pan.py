"""Transient center intent, crop registration and identity through hermetic adapters."""
import os
import subprocess
import urllib.error
from pathlib import Path

import pytest
from PIL import Image

from lib import almanac_emit as ae
from lib.radar_geometry import MAX_LAT, world_point, world_inverse
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_freshness_health import _load_serve, _payload, _get, serve_at  # noqa: F401


@pytest.mark.parametrize('value,valid', [('station', True), ('47.61,-122.33', True),
    ('-85.05112878,180', True), ('85.05112878,-180', True), ('0,0', True),
    ('85.05112879,0', False), ('-85.052,0', False), ('0,180.01', False),
    ('0,-181', False), ('NaN,0', False), ('inf,0', False), ('1e1,0', False),
    ('+1,0', False), ('.5,0', False), ('1.,0', False), ('1, 0', False),
    ('1,0\n', False), ('station ', False), ('1,0junk', False), ('', False),
    ('١,0', False), ('0000,0', False)])
@pytest.mark.parametrize('address', ['127.0.0.1', '::1', '::ffff:127.0.0.1', '198.51.100.2'])
def test_loopback_center_validation(monkeypatch, tmp_path, value, valid, address):
    from urllib.parse import urlencode
    module = _load_serve(monkeypatch, tmp_path, _payload())
    served = []
    monkeypatch.setattr(module.http.server.SimpleHTTPRequestHandler, 'do_GET', lambda h: served.append(h.path))
    h = object.__new__(module.Handler); h.client_address = (address, 1)
    h.path = '/wx.json?' + urlencode({'radarCenter': value})
    h.do_GET(); module._flush_preferences()  # preference writer thread: wait for the durable write
    marker = tmp_path / 'radar_center'
    assert marker.exists() is (valid and address in module.LOOPBACK)
    if marker.exists():
        assert marker.read_text().strip() == ('station' if value == 'station' else ','.join(str(float(n)) for n in value.split(',')))
        assert not marker.is_symlink()
    assert served == [h.path]


def test_duplicate_center_and_atomic_dedup(serve_at, tmp_path, monkeypatch):
    module, url = serve_at(_payload())
    assert _get(url + '/wx.json?radarCenter=1,2&radarCenter=3,4')[0] == 200
    marker = tmp_path / 'radar_center'; assert not marker.exists()
    replace = os.replace; calls = []
    def replacing(src, dst):
        assert Path(src).parent == Path(dst).parent == tmp_path
        assert Path(src).read_text() in ('1.0,2.0\n', 'station\n')
        calls.append(dst); replace(src, dst)
    monkeypatch.setattr(module.os, 'replace', replacing)
    for value in ('1,2', '1.0,2.00', '1,2', 'station', 'station'):
        assert _get(url + '/wx.json?radarCenter=' + value)[0] == 200
    assert len(calls) == 2 and marker.read_text() == 'station\n'
    assert not list(tmp_path.glob('radar_center.tmp.*'))


def test_center_never_follows_durable_symlink(monkeypatch, tmp_path):
    module = _load_serve(monkeypatch, tmp_path, _payload())
    durable = tmp_path / 'durable'; durable.mkdir()
    saved = durable / 'radar_center'; saved.write_text('1.0,2.0\n')
    marker = tmp_path / 'radar_center'; marker.symlink_to(saved)
    module._write_radar_center(['1,2']); module._flush_preferences()  # preference writer thread: wait for the durable write
    assert not marker.is_symlink() and saved.read_text() == '1.0,2.0\n'
    module._write_radar_center(['station']); module._flush_preferences()
    assert saved.read_text() == '1.0,2.0\n'


def test_runtime_recreation_keeps_zoom_only(monkeypatch, tmp_path):
    script = Path('design/almanac/kiosk/almanac-kiosk.sh').read_text()
    setup = 'RADAR_STATE=' + script.split('RADAR_STATE=', 1)[1].split('ln -sfn "$RADAR_STATE/radar_zoom" "$DATA_DIR/radar_zoom"', 1)[0]
    setup += 'ln -sfn "$RADAR_STATE/radar_zoom" "$DATA_DIR/radar_zoom"'
    assert 'radar_center' not in script
    runtime = tmp_path / 'runtime'; runtime.mkdir()
    env = dict(os.environ, XDG_STATE_HOME=str(tmp_path / 'durable'), DATA_DIR=str(runtime))
    subprocess.run(['bash', '-c', setup], env=env, check=True)
    module = _load_serve(monkeypatch, runtime, _payload())
    module._write_radar_zoom(['8']); module._write_radar_center(['1,2']); module._flush_preferences()  # preference writer thread: wait for the durable write
    assert not (runtime / 'radar_center').is_symlink()
    for item in runtime.iterdir(): item.unlink()
    runtime.rmdir(); runtime.mkdir()
    subprocess.run(['bash', '-c', setup], env=env, check=True)
    assert not (runtime / 'radar_center').exists()
    assert (runtime / 'radar_zoom').read_text() == '8\n'


@pytest.mark.parametrize('raw', [None, 'station', 'junk', '86,0', '0,181', b'\xff',
    '1,2' + ' ' * 128 + 'junk', '', 'NaN,0'])
def test_invalid_or_absent_center_is_station(make_emitter, hybrid, tmp_path, raw):
    if raw is not None:
        (tmp_path / 'radar_center').write_bytes(raw if isinstance(raw, bytes) else raw.encode())
    emitter = make_emitter(); emitter._do_radar()
    r = emitter._build_payload()['radar']
    assert r['available'] and 'centered' not in r
    assert r['center'] == dict(lat=47.61, lon=-122.33) and 'marker' not in r


@pytest.mark.parametrize('center', [(47.61, -122.6), (48.1, -122.33), (47.61, -122.33)])
def test_report_changes_tile_grid_without_changing_station_or_tile_bytes(make_emitter, hybrid, tmp_path, center):
    emitter=make_emitter();emitter._do_radar();before=emitter._radar_result
    old={p:p.read_bytes() for p in Path(ae.RADAR_DIR).glob('t/*/*/*/8/*/*.png')}
    hybrid.mono+=60;(tmp_path/'radar_center').write_text(','.join(map(str,center)))
    emitter._do_radar();after=emitter._radar_result;r=emitter._build_payload()['radar']
    assert r['center']==dict(lat=47.61,lon=-122.33) and 'marker' not in r
    _,mpp,bounds,_=ae._radar_viewport(*center,before.zoom,956,490)
    assert after.bounds==bounds and after.mpp==mpp and after.nexrad==before.nexrad
    assert all(p.read_bytes()==raw for p,raw in old.items())
    (tmp_path/'radar_center').write_text('station');emitter._do_radar()
    assert emitter._radar_result.center==before.center


def test_pan_failure_keeps_entire_previous_snapshot(make_emitter, hybrid, tmp_path):
    emitter = make_emitter(); emitter._do_radar(); previous = emitter._radar_result
    (tmp_path / 'radar_center').write_text('48,-122')
    hybrid.failure = lambda *_: (_ for _ in ()).throw(urllib.error.URLError('offline'))
    emitter._do_radar()
    assert emitter._radar_result.ts_frame==previous.ts_frame
    assert emitter._radar_result.ts_fetch==previous.ts_fetch


def test_center_wakes_same_single_flight_worker(make_emitter, tmp_path, monkeypatch):
    emitter = make_emitter(); emitter._running = True
    seen = []; monkeypatch.setattr(emitter, '_spawn', lambda key, worker: seen.append(True))
    emitter._radar_zoom_stamp = emitter._radar_preference_stamp()
    emitter._inflight.add('radar')
    marker = tmp_path / 'radar_center'; marker.write_text('1,2'); emitter._check_radar_zoom()
    marker.write_text('3,4'); emitter._check_radar_zoom(); assert not seen
    emitter._inflight.clear(); emitter._check_radar_zoom(); emitter._check_radar_zoom()
    assert seen == [True]
    marker.unlink(); emitter._check_radar_zoom(); assert len(seen) == 2


@pytest.mark.parametrize('zoom', [4, 7, 10])
@pytest.mark.parametrize('lat,lon', [(0, 0), (47.61, -122.33), (-33.87, 151.21), (MAX_LAT, 180), (-MAX_LAT, -180)])
def test_mercator_inverse_roundtrip(lat, lon, zoom):
    assert world_inverse(*world_point(lat, lon, zoom), zoom) == pytest.approx((lat, lon))


def test_small_decimal_center_survives_writer_and_emitter(make_emitter, hybrid, tmp_path, monkeypatch):
    module = _load_serve(monkeypatch, tmp_path, _payload())
    module._write_radar_center(['0.0000000001,-0.000000001']); module._flush_preferences()  # preference writer thread: wait for the durable write
    assert (tmp_path / 'radar_center').read_text() == '0.0000000001,-0.000000001\n'
    emitter = make_emitter(); emitter._do_radar()
    assert emitter._radar_result.center == dict(lat=47.61,lon=-122.33)
    assert emitter._radar_result.bounds==ae._radar_viewport(1e-10,-1e-9,8,956,490)[2]


def test_site_pan_beyond_all_range_circles_falls_back(make_emitter, hybrid, tmp_path, monkeypatch):
    # v3 queries only circles intersecting the viewport. Far offshore there is
    # no reporting site to drive a timeline, so the existing mosaic fallback applies.
    original = ae.RadarSession.open
    def fetch(self, req, timeout):
        assert 'operation=list' not in req.full_url and 'ridge::' not in req.full_url
        return original(self, req, timeout)
    monkeypatch.setattr(ae.RadarSession, 'open', fetch)
    (tmp_path / 'radar_source').write_text('site')
    (tmp_path / 'radar_center').write_text('47.61,-130')
    emitter = make_emitter(); emitter._do_radar(); r = emitter._build_payload()['radar']
    assert r['available'] and r['sourceMode'] == 'mosaic'
    assert r['sitesConsidered'] == r['sitesDrawn'] == 0 and r['sites'] == []
    assert r['sources'][1]['reason'] == 'out of view'
    assert r['center'] == dict(lat=47.61, lon=-122.33) and 'centered' not in r


@pytest.mark.parametrize('failure', ['open', 'replace'])
def test_center_io_error_preserves_marker_and_poll(serve_at, tmp_path, monkeypatch, failure):
    import builtins
    module, url = serve_at(_payload()); marker = tmp_path / 'radar_center'; marker.write_text('1,2')
    if failure == 'replace':
        monkeypatch.setattr(module.os, 'replace', lambda *_: (_ for _ in ()).throw(OSError('read only')))
    else:
        original = builtins.open
        def opening(path, *args, **kwargs):
            if 'radar_center.tmp' in str(path): raise OSError('read only')
            return original(path, *args, **kwargs)
        monkeypatch.setattr(builtins, 'open', opening)
    assert _get(url + '/wx.json?radarCenter=3,4')[0] == 200
    module._flush_preferences()  # preference writer thread: wait for the failed write
    assert marker.read_text() == '1,2' and not list(tmp_path.glob('radar_center.tmp.*'))
