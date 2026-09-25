"""Remote admission and shared ownership, using real handlers without sockets."""
import importlib.util
import json
from pathlib import Path
from urllib.parse import urlencode

import pytest

A, B = 'remote-session-a-123', 'remote-session-b-123'


@pytest.fixture
def server(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('remote_serve', Path('design/almanac/kiosk/serve.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, '_DEFAULT_GATEWAYS', set())
    module.DATA = str(tmp_path / 'wx.json')
    (tmp_path / 'wx.json').write_text('{"ts":1}')
    # Persistence is checked explicitly; no timer outlives its fixture.
    class Timer:
        def __init__(self, delay, fn): self.function = fn
        def start(self): pass
        def cancel(self): pass
    monkeypatch.setattr(module.threading, 'Timer', Timer)
    monkeypatch.setattr(module.http.server.SimpleHTTPRequestHandler, 'do_GET', lambda h: h.reads.append(Path(module.DATA).read_text()))
    monkeypatch.setattr(module.http.server.SimpleHTTPRequestHandler, 'end_headers', lambda h: None)
    return module


def request(server, address='192.168.1.20', **params):
    h = object.__new__(server.Handler)
    h.client_address = (address, 1234)
    h.path = '/wx.json?' + urlencode(params, doseq=True)
    h.reads, h.headers_sent = [], {}
    h.send_header = lambda k, v: h.headers_sent.__setitem__(k, v)
    h.do_GET()
    h.end_headers()
    assert h.reads == ['{"ts":1}']
    return h.headers_sent


def camera(session=A, generation=1, claim='', epoch=0, **extra):
    return dict(radarSession=session, radarGeneration=generation, radarHeartbeat=generation,
                radarClaim=claim, radarClaimEpoch=epoch, radarCommit=1, radarPolicy='manual', radarSource='auto',
                view='radar', radarTheme='paper', radarGeoZoom=8,
                radarGeoCenter='47,-122', radarMoving=0, **extra)


@pytest.mark.parametrize('address', [
    '127.0.0.1', '127.9.8.7', '::1', '::ffff:127.0.0.1',
    '10.0.0.1', '10.255.255.254', '172.16.0.1', '172.31.255.254', '192.168.0.1',
    'fc00::1', 'fdff::1', 'fe80::1', 'febf::1', 'fe80::1%eth0',
    '::ffff:192.168.1.1', '::ffff:ac10:1', '::ffff:a00:1',
])
def test_controllers_can_commit_and_receive_all_acknowledgements(server, tmp_path, address):
    assert server._is_controller(address)
    headers = request(server, address, **camera())
    assert set(headers) == {'X-Radar-Intent', 'X-Radar-Smooth', 'X-View-Session', 'X-Radar-Panel'}
    assert json.loads(headers['X-Radar-Intent'])['session'] == A
    assert server._read_radar_intent()['zoom'] == 8
    if server._is_loopback(address):
        assert json.loads((tmp_path/'radar_activity').read_text())['zoom'] == 8
    else:
        assert not (tmp_path/'radar_activity').exists()
    server._camera_persist_timer.function()
    assert (tmp_path/'radar_zoom').read_text().strip() == '8'


@pytest.mark.parametrize('address', [
    '8.8.8.8', '172.15.255.255', '172.32.0.1', '192.169.0.1', '100.64.0.1',
    '169.254.1.1', '0.0.0.0', '192.0.2.1', '198.51.100.1', '203.0.113.1',
    '2001:4860::1', '2001:db8::1', 'fec0::1', 'ff02::1', '::',
    '::ffff:8.8.8.8', '::ffff:192.0.2.1', 'not-an-ip', '192.168.1', '010.0.0.1',
])
def test_other_addresses_are_read_only_with_no_headers(server, tmp_path, address):
    assert not server._is_controller(address)
    assert request(server, address, **camera(touch=1, r=1, radarSmooth='on', radarRender='v2')) == {}
    assert {p.name for p in tmp_path.iterdir()} == {'wx.json'}
    assert server._renders == 0 and server._polls == 1


def test_last_user_commit_wins_and_old_owner_cannot_write(server):
    request(server, **camera())
    before = server._read_radar_intent()
    # Reload/poll claims, even with the right owner, are inert.
    request(server, **dict(camera(B, 0, A, epoch=1), radarCommit=0))
    assert server._read_radar_intent() == before
    request(server, **dict(camera(B, 1, A, epoch=1), radarGeoZoom=6, radarSource='mosaic'))
    accepted = server._read_radar_intent()
    assert accepted['session'] == B and accepted['zoom'] == 6
    request(server, **camera(A, 99, ''))
    assert server._read_radar_intent() == accepted
    # A's next explicit action acknowledges B and starts its own next generation.
    request(server, **camera(A, 2, B, epoch=2))
    assert server._read_radar_intent()['session'] == A


def test_invalid_claim_never_transfers_owner_or_activity(server, tmp_path):
    request(server, **camera())
    before = server._read_radar_intent()
    for changes in ({'radarClaim': ''}, {'radarMoving': 1}, {'radarGeoZoom': 11},
                    {'radarGeoCenter': '91,0'}, {'radarPolicy': 'bogus'},
                    {'radarSource': ['auto', 'site']}, {'radarSession': [B, A]},
                    {'radarGeneration': ['1', '2']}, {'radarHeartbeat': 'bad'}):
        request(server, **dict(camera(B, 1, A, epoch=1), **changes))
        assert server._read_radar_intent() == before
        assert not (tmp_path/'radar_activity').exists()
        assert server._radar_owner['session'] == A


def test_nonowner_preferences_need_valid_session_but_not_camera_acceptance(server, tmp_path):
    request(server, **camera())
    intent = server._read_radar_intent()
    headers = request(server, radarSession=B, radarSmooth='on', radarRender='v2')
    assert headers['X-Radar-Smooth'] == 'on' and 'X-Radar-Render' not in headers
    for session in (None, '', 'short', [A, B]):
        params = dict(radarSmooth='off', radarRender='v1')
        if session is not None: params['radarSession'] = session
        request(server, **params)
    for smooth, render in (('ON', 'V2'), (['off', 'on'], ['v1', 'v2']), ('', '')):
        request(server, radarSession=B, radarSmooth=smooth, radarRender=render)
    assert (tmp_path/'radar_smooth').read_text().strip() == 'on'
    assert not (tmp_path/'radar_render').exists()
    assert server._read_radar_intent() == intent


def test_panel_viewing_is_independent_of_remote_camera_owner(server, tmp_path):
    request(server, **camera())
    assert not (tmp_path/'radar_viewed').exists()
    request(server, '127.0.0.1', radarSession=B, radarGeneration=0, viewSession=B,
            viewSeq=1, view='radar', radarTheme='night', r=1)
    before = (tmp_path/'radar_viewing').read_text()
    assert (tmp_path/'radar_viewed').exists() and server._renders == 1
    request(server, viewSession=A, viewClaim=B, viewSeq=99, view='none', r=1)
    assert (tmp_path/'radar_viewing').read_text() == before
    assert server._view_owner['session'] == B and server._renders == 1
    request(server, '::ffff:127.0.0.1', viewSession=B, viewSeq=2, view='none')
    assert not (tmp_path/'radar_viewing').exists()


def test_lan_touch_expires_source_before_refreshing_presence(server, tmp_path, monkeypatch):
    now = 1800000000
    monkeypatch.setattr(server.time, 'time', lambda: now)
    (tmp_path/'presence').write_text(str(now-2700))
    record = dict(seq=1, session=A, generation=1, source='site', sourceAcceptedAt=now-3000)
    (tmp_path/'radar_intent').write_text(json.dumps(record))
    (tmp_path/'radar_source').write_text('site')
    request(server, touch=1)
    assert float((tmp_path/'presence').read_text()) == now
    accepted = server._read_radar_intent()
    assert accepted['source'] == 'auto' and accepted['generation'] == 1
    assert accepted['seq'] == 2
    assert not (tmp_path/'radar_viewing').exists()


def test_rate_limit_rejects_writes_not_reads_or_acknowledgements(server, tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(server.time, 'monotonic', lambda: clock[0])
    for _ in range(int(server._CONTROL_BURST)):
        request(server, radarSession=A, radarSmooth='off')
    request(server, '::ffff:192.168.1.20', **camera(touch=1, radarSmooth='on'))
    assert not (tmp_path/'radar_intent').exists()
    assert not (tmp_path/'presence').exists()
    assert (tmp_path/'radar_smooth').read_text().strip() == 'off'
    assert 'X-Radar-Intent' in request(server)
    request(server, '192.168.1.21', radarSession=B, radarSmooth='on')
    assert (tmp_path/'radar_smooth').read_text().strip() == 'on'
    clock[0] += 1
    request(server, **camera(touch=1))
    assert server._read_radar_intent()['session'] == A
    assert (tmp_path/'presence').exists()


def test_failed_commit_cannot_claim_and_two_claimants_compare_the_same_owner(server, monkeypatch):
    request(server, **camera())
    before = server._read_radar_intent()
    writer = server._write_settled_camera
    monkeypatch.setattr(server, '_write_settled_camera', lambda *args: False)
    request(server, **camera(B, 1, A, epoch=1))
    assert server._read_radar_intent() == before and server._radar_owner['session'] == A
    monkeypatch.setattr(server, '_write_settled_camera', writer)
    request(server, **camera(B, 1, A, epoch=1))
    request(server, **camera('remote-session-c-123', 1, A, epoch=1))
    assert server._read_radar_intent()['session'] == B


def test_owner_generation_and_heartbeat_fences_protect_camera(server, tmp_path):
    request(server, **camera())
    request(server, **camera(A, 2))
    before = server._read_radar_intent()
    for generation, heartbeat in ((1, 99), (2, 1), (2, 2)):
        request(server, **dict(camera(A, generation), radarHeartbeat=heartbeat, radarMoving=1, radarCommit=0))
        assert server._read_radar_intent() == before
    request(server, **dict(camera(A, 2), radarHeartbeat=3, radarMoving=1, radarCommit=0))
    assert server._radar_owner['heartbeat'] == 3
    assert not (tmp_path/'radar_activity').exists()
    assert server._read_radar_intent() == before


def test_restart_does_not_let_a_legacy_poll_overwrite_ordered_owner(server):
    request(server, **camera())
    before = server._read_radar_intent()
    server._radar_owner = None
    request(server, view='radar', radarTheme='paper', radarGeoZoom=5, radarGeoCenter='0,0')
    assert server._read_radar_intent() == before


def test_rate_limiter_bounds_memory_and_reclaims_idle_clients(server, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(server.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(server, '_CONTROL_CLIENTS', 2)
    assert server._allow_control_write('192.168.1.1')
    assert server._allow_control_write('192.168.1.2')
    assert not server._allow_control_write('192.168.1.3')
    assert len(server._control_buckets) == 2
    clock[0] += 60
    assert server._allow_control_write('192.168.1.3')
    assert len(server._control_buckets) == 1


def test_bad_tile_reports_have_a_separate_write_bucket(server, tmp_path, monkeypatch):
    import io
    monkeypatch.setattr(server.time, 'monotonic', lambda: 100.0)
    for _ in range(int(server._CONTROL_BURST)):
        assert server._allow_control_write('127.0.0.1')
    h = object.__new__(server.Handler)
    h.client_address, h.path = ('::ffff:127.0.0.1', 1), '/radar-bad-tile'
    body = b'radar/t/123456789abc/iem-mrms-lcref/-/202609251200/8/1/1.png'
    h.rfile, h.headers = io.BytesIO(body), {'Content-Length': str(len(body))}
    errors = []
    h.send_error = lambda code, *args: errors.append(code)
    h.send_response = lambda code: errors.append(code)
    h.send_header = lambda *args: None
    h.end_headers = lambda: None
    h.do_POST()
    assert errors == [204] and (tmp_path/'radar_bad_tiles').exists()
