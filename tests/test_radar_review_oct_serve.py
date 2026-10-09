"""October radar review: serve.py hardening (B5) and /health radar block (B6).

Real handlers, either over loopback sockets (serve_at) or invoked in memory
like the remote-control tests. Nothing here leaves the machine.
"""
import io
import json
import os
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest

from tests.test_freshness_health import serve_at, _get, _load_serve, _payload  # noqa: F401
from tests.test_radar_remote_serve import A, B, camera, server  # noqa: F401


def _raw(url, data, read=True, timeout=5):
    host, port = url.rsplit('/', 1)[1].split(':')
    sock = socket.create_connection((host, int(port)), timeout=timeout)
    try:
        sock.sendall(data)
        if not read:
            return sock
        chunks = []
        while True:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b''.join(chunks)
    finally:
        if read:
            sock.close()


def _status(response):
    return int(response.split(b' ', 2)[1]) if response.startswith(b'HTTP/') else None


def _handler(module, address='192.168.1.20', headers=None, **params):
    h = object.__new__(module.Handler)
    h.client_address = (address, 1234)
    h.path = '/wx.json?' + urlencode(params, doseq=True)
    h.headers = headers or {}
    h.reads, h.headers_sent = [], {}
    h.send_header = lambda k, v: h.headers_sent.__setitem__(k, v)
    h.do_GET()
    h.end_headers()
    assert h.reads, 'the read must always be served'
    return h.headers_sent


# --- B5(a) connection deadlines and admission --------------------------------

def test_trickled_headers_hit_one_deadline_not_a_per_read_timeout(serve_at):
    module, url = serve_at(_payload())
    module.HEADER_DEADLINE_SEC = 0.6
    sock = _raw(url, b'GET /wx.json HTTP/1.1\r\n', read=False)
    try:
        started, closed = time.monotonic(), False
        # One header byte every 0.2 s: each read is quick, the request never is.
        for byte in b'X-Slow: ' + b'a' * 40:
            try:
                sock.sendall(bytes([byte]))
            except OSError:
                closed = True
                break
            time.sleep(0.2)
            sock.settimeout(0.01)
            try:
                if sock.recv(1) == b'':
                    closed = True
                    break
            except socket.timeout:
                pass
            except OSError:
                closed = True
                break
        assert closed, 'a trickling header kept its connection'
        assert time.monotonic() - started < 3
    finally:
        sock.close()


def test_idle_keepalive_connection_is_closed_and_quietly(serve_at, capsys):
    module, url = serve_at(_payload())
    module.KEEPALIVE_IDLE_SEC = 0.3
    response = _raw(url, b'GET /wx.json HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n')
    assert _status(response) == 200          # served, then closed for idleness
    assert 'timed out' not in capsys.readouterr().err


def test_kiosk_has_its_own_pool_and_lan_clients_are_capped(serve_at, monkeypatch):
    module, _ = serve_at(_payload())
    monkeypatch.setattr(module, 'LAN_CONNECTIONS', 3)
    monkeypatch.setattr(module, 'LAN_CLIENT_CONNECTIONS', 2)
    monkeypatch.setattr(module, 'LOCAL_CONNECTIONS', 2)
    srv = module.Server(('127.0.0.1', 0), module.Handler)
    try:
        a1, a2 = srv._admit('192.168.1.5'), srv._admit('::ffff:192.168.1.5')
        assert a1 and a2 and srv._admit('192.168.1.5') is None     # per-client cap (mapped alias too)
        b1 = srv._admit('192.168.1.6')
        assert b1 and srv._admit('192.168.1.7') is None              # LAN pool full
        # The LAN pool is full; the kiosk is still admitted from its own pool.
        k1, k2 = srv._admit('127.0.0.1'), srv._admit('::1')
        assert k1 and k2 and srv._admit('127.0.0.1') is None
        srv._release(a1)
        assert srv._admit('192.168.1.7')
        for ticket in (a2, b1, k1, k2):
            srv._release(ticket)
        assert srv._local == 0 and srv._lan == 1 and set(srv._clients) == {'192.168.1.7'}
    finally:
        srv.server_close()


def test_rejected_connection_is_closed_without_a_thread(serve_at, monkeypatch):
    module, _ = serve_at(_payload())
    srv = module.Server(('127.0.0.1', 0), module.Handler)
    try:
        monkeypatch.setattr(module, 'LAN_CONNECTIONS', 0)
        closed, started = [], []
        monkeypatch.setattr(srv, 'shutdown_request', closed.append)
        monkeypatch.setattr(module.threading, 'Thread', lambda *a, **k: started.append(a) or pytest.fail('thread'))
        srv.process_request('sock', ('192.168.1.9', 1))
        assert closed == ['sock'] and not started
    finally:
        srv.server_close()


def test_admitted_connection_releases_its_slot(serve_at):
    module, _ = serve_at(_payload())
    # Slots come back after each request on a real server.
    srv = module.Server(('127.0.0.1', 0), module.Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        port = srv.server_address[1]
        for _ in range(5):
            assert _get(f'http://127.0.0.1:{port}/health')[0] == 200
        deadline = time.monotonic() + 2
        while srv._local and time.monotonic() < deadline:
            time.sleep(0.01)
        assert srv._local == 0
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(5)


# --- B5(b) cross-site GETs cannot mutate -------------------------------------

@pytest.mark.parametrize('headers', [
    {'Sec-Fetch-Site': 'cross-site'}, {'Sec-Fetch-Site': 'same-site'},
    {'Origin': 'http://evil.example', 'Host': '192.168.1.40:8137'},
    {'Origin': 'null', 'Host': '192.168.1.40:8137'},
])
def test_cross_site_poll_is_read_only(server, tmp_path, headers):
    sent = _handler(server, headers=headers, **camera(touch=1, radarSmooth='on', viewSession=A, viewSeq=1))
    server._flush_preferences(force=True)
    # last_viewer is the wifi keepalive's hint that a LAN device exists, not control.
    assert {p.name for p in tmp_path.iterdir()} <= {'wx.json', 'last_viewer'}
    assert 'X-Radar-Throttled' not in sent
    # The kiosk's render acknowledgement is a mutation too (the watchdog trusts it).
    _handler(server, '127.0.0.1', headers=headers, r=1)
    assert server._renders == 0


@pytest.mark.parametrize('headers', [
    {'Sec-Fetch-Site': 'same-origin'}, {'Sec-Fetch-Site': 'none'},
    {'Origin': 'http://192.168.1.40:8137', 'Host': '192.168.1.40:8137'}, {},
])
def test_same_origin_poll_still_steers(server, tmp_path, headers):
    _handler(server, headers=headers, **camera(touch=1))
    assert server._read_radar_intent()['session'] == A
    assert (tmp_path/'presence').exists()


def test_cross_site_bad_tile_report_is_refused(server, tmp_path):
    h = object.__new__(server.Handler)
    body = b'radar/t/123456789abc/iem-mrms-lcref/-/202609251200/8/1/1.png'
    h.client_address, h.path = ('127.0.0.1', 1), '/radar-bad-tile'
    h.rfile, h.headers = io.BytesIO(body), {'Content-Length': str(len(body)), 'Sec-Fetch-Site': 'cross-site'}
    codes = []
    h.send_error = lambda code, *args: codes.append(code)
    h.do_POST()
    assert codes == [403] and not (tmp_path/'radar_bad_tiles').exists()


# --- B5(c) Host allow-list ------------------------------------------------------

@pytest.mark.parametrize('host,allowed', [
    ('127.0.0.1:8137', True), ('localhost:8137', True), ('LOCALHOST', True), ('192.168.1.40:8137', True),
    ('[::1]:8137', True), ('[fe80::1%eth0]:8137', True), ('10.0.0.5', True),
    ('weather', True), ('weather.local:8137', True), ('weather.local.', True), ('pi.example.net', True),
    ('evil.example', False), ('weather.evil.example', False), ('192.168.1.40.nip.io', False),
    ('', False), ('[::1', False), ('localhost:http', False), (None, True),
])
def test_host_allow_list(server, monkeypatch, host, allowed):
    monkeypatch.setattr(server.socket, 'gethostname', lambda: 'weather')
    monkeypatch.setattr(server, 'ALLOWED_HOSTS', frozenset({'pi.example.net'}))
    assert server._host_allowed(host) is allowed


def test_rebound_host_is_refused_over_the_wire(serve_at):
    _, url = serve_at(_payload())
    for host in (b'evil.example', b'attacker.example:8137'):
        response = _raw(url, b'GET /health HTTP/1.1\r\nHost: ' + host + b'\r\nConnection: close\r\n\r\n')
        assert _status(response) == 421 and b'"station"' not in response
    port = url.rsplit(':', 1)[1].encode()
    ok = _raw(url, b'GET /health HTTP/1.1\r\nHost: 127.0.0.1:' + port + b'\r\nConnection: close\r\n\r\n')
    assert _status(ok) == 200
    own = socket.gethostname().encode()
    assert _status(_raw(url, b'GET /health HTTP/1.1\r\nHost: ' + own + b'\r\nConnection: close\r\n\r\n')) == 200


def test_env_override_adds_a_host(monkeypatch, tmp_path):
    module = _load_serve(monkeypatch, tmp_path, _payload(), WFP_ALLOWED_HOSTS='Pi.Example.NET, other.lan')
    assert module.ALLOWED_HOSTS == {'pi.example.net', 'other.lan'}
    assert module._host_allowed('pi.example.net:8137') and module._host_allowed('other.lan')


# --- B5(d) no directory listings -----------------------------------------------

def test_directory_listing_is_off(serve_at, tmp_path):
    _, url = serve_at(_payload())
    (tmp_path/'radar'/'t').mkdir(parents=True)
    (tmp_path/'secret').mkdir()
    (tmp_path/'secret'/'notes.txt').write_text('x')
    for path in (b'/secret/', b'/radar/', b'/'):
        response = _raw(url, b'GET ' + path + b' HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n')
        assert _status(response) == 404 and b'notes.txt' not in response and b'wx.json' not in response
    (tmp_path/'index.html').write_text('<!doctype html>page')
    response = _raw(url, b'GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n')
    assert _status(response) == 200 and response.endswith(b'page')


# --- B5(e) Smooth: owner only, small budget, debounced durable write ---------------

def test_smooth_budget_is_far_below_the_control_limiter(server, tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(server.time, 'monotonic', lambda: clock[0])
    request = lambda generation, value: _handler(server, **camera(A, generation, radarSmooth=value))
    staged = []
    for generation in range(1, 21):           # 20 toggles inside the control budget
        request(generation, 'on' if generation % 2 else 'off')
        staged.append(server._read_preference('radar_smooth'))
    changes = sum(1 for a, b in zip([None]+staged, staged) if a != b)
    assert changes == int(server._SMOOTH_BURST)
    clock[0] += 1/server._SMOOTH_RATE
    request(21, 'off' if staged[-1] == 'on' else 'on')
    assert server._read_preference('radar_smooth') != staged[-1]
    # A repeat of the current value costs nothing.
    before = dict(server._smooth_buckets)
    request(22, server._read_preference('radar_smooth'))
    assert server._smooth_buckets == before


def test_smooth_reaches_disk_once_after_the_debounce(server, tmp_path, monkeypatch):
    # The writer thread owns the debounce (adversarial review): no per-change timers.
    clock = [100.0]
    monkeypatch.setattr(server.time, 'monotonic', lambda: clock[0])
    writes = []
    persist = server._persist_preference
    monkeypatch.setattr(server, '_persist_preference', lambda name, value: writes.append((name, value)) or persist(name, value))
    for generation, value in ((1, 'on'), (2, 'off'), (3, 'on')):
        headers = _handler(server, **camera(A, generation, radarSmooth=value))
        assert headers['X-Radar-Smooth'] == value          # acknowledged before it lands
    server._flush_preferences()                             # nothing is due yet
    assert not (tmp_path/'radar_smooth').exists() and not writes
    clock[0] += server._SMOOTH_DEBOUNCE_SEC
    server._flush_preferences()                             # or the writer, whichever is first
    deadline = time.time() + 5
    while not writes and time.time() < deadline:
        time.sleep(.01)
    server._flush_preferences()
    assert writes == [('radar_smooth', 'on')]
    assert (tmp_path/'radar_smooth').read_text() == 'on\n'


def test_racing_flushes_keep_the_newest_decision(server, tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    persist = server._persist_preference
    def slow(name, value):
        if value == '6':
            entered.set()
            assert release.wait(5)
        persist(name, value)
    monkeypatch.setattr(server, '_persist_preference', slow)
    worker = threading.Thread(target=server._write_radar_zoom, args=(['6'],))
    worker.start()
    assert entered.wait(5)
    server._stage_preference('radar_zoom', ['7'])
    release.set()
    worker.join(5)
    server._flush_preferences()
    assert (tmp_path/'radar_zoom').read_text() == '7\n'
    assert server._read_preference('radar_zoom') == '7' and not server._pref_pending


# --- B5(f) no fsync while holding _count_lock --------------------------------------

def test_no_fsync_under_the_control_lock(server, tmp_path, monkeypatch):
    # Stronger since the adversarial review: no request thread fsyncs at all,
    # with or without _count_lock; the preference writer thread does.
    synced, requesting = [], threading.Event()
    real = os.fsync
    def fsync(fd):
        assert not (requesting.is_set() and threading.current_thread() is threading.main_thread()), \
            'a request thread waited on an SD-card fsync'
        synced.append(threading.current_thread().name)
        real(fd)
    monkeypatch.setattr(server.os, 'fsync', fsync)
    def handle(*args, **kwargs):
        requesting.set()
        try:
            return _handler(server, *args, **kwargs)
        finally:
            requesting.clear()
    def landed(name, value):
        deadline = time.time() + 5
        while time.time() < deadline:
            if (tmp_path/name).exists() and (tmp_path/name).read_text().strip() == value:
                return True
            time.sleep(.01)
        return False
    # Camera commit (debounced durable zoom), the legacy intent path (an old
    # page's radarSource is ignored) and an owner's Smooth change.
    handle('127.0.0.1', **camera())
    server._camera_persist_timer.function()
    assert landed('radar_zoom', '8')
    (tmp_path/'radar_intent').unlink()
    server._radar_owner = None
    handle('127.0.0.1', radarSeq=5, radarZoom=7, radarSource='site', radarCenter='station')
    assert landed('radar_zoom', '7') and not (tmp_path/'radar_source').exists()
    handle('127.0.0.1', **camera(A, 2, radarSmooth='on'))
    server._flush_preferences(force=True)
    assert (tmp_path/'radar_smooth').read_text().strip() == 'on'
    assert len(synced) >= 3 and 'preference-writer' in synced  # zoom, zoom, smooth


def test_response_headers_never_wait_for_a_durable_write(server, tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    real = os.fsync
    def stalled(fd):
        entered.set()
        assert release.wait(5)
        real(fd)
    monkeypatch.setattr(server.os, 'fsync', stalled)
    worker = threading.Thread(target=server._write_radar_zoom, args=(['9'],))
    worker.start()
    try:
        assert entered.wait(5)
        done = threading.Event()
        def poll():
            _handler(server, **camera())
            done.set()
        threading.Thread(target=poll, daemon=True).start()
        assert done.wait(2), 'a poll waited on an SD-card fsync'
    finally:
        release.set()
        worker.join(5)


# --- B5(g) radar tiles are served without access-time writes ------------------

def test_radar_tiles_skip_utime_geography_keeps_its_lru_clock(serve_at, tmp_path, monkeypatch):
    module, url = serve_at(_payload())
    radar = tmp_path/'radar'
    radar.mkdir()
    (radar/'.tile-revision').write_text('123456789abc')
    (radar/'.geo-revision').write_text('abcdef012345')
    tile = radar/'t/123456789abc/iem-mrms-lcref/-/202609251200/8/1/1.png'
    geo = radar/'geo/abcdef012345/paper/8/1/1.png'
    for path in (tile, geo):
        path.parent.mkdir(parents=True)
        path.write_bytes(b'\x89PNG')
        os.utime(path, (1000, 1000))
    for path in (tile, geo):
        response = _raw(url, b'GET /' + str(path.relative_to(tmp_path)).encode() + b' HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n')
        assert _status(response) == 200 and b'immutable' in response
    assert os.stat(tile).st_atime == 1000
    assert os.stat(geo).st_atime > 1000


# --- B6 /health radar block -------------------------------------------------------

def test_health_reports_radar_health_file(serve_at, tmp_path):
    _, url = serve_at(_payload())
    now = time.time()
    summary = dict(state='failed', newestObservationTs=now-4000, newestObservationAgeSec=4000, lastSuccessTs=None,
                   source='iem-nexrad-n0b', fallbackReason='level3 unavailable', coverage='partial',
                   nextAttemptTs=now+60, attentionTier='rest', attentionReason='idle', initError=None)
    (tmp_path/'radar-health.json').write_text(json.dumps(dict(summary=summary, writtenTs=now-5, hedges=3, breaker='open')))
    status, health = _get(url + '/health')
    # A failed radar is reported, but the engine verdict and HTTP code are the engine's.
    assert (status, health['status']) == (200, 'ok')
    radar = health['radar']
    assert radar['available'] is True and radar['summary'] == summary and radar['breaker'] == 'open'
    assert 4 <= radar['fileAgeSec'] <= 10


@pytest.mark.parametrize('content,reason', [
    (None, 'missing'), ('{', 'unreadable'), ('[1, 2]', 'not an object'),
    ('{"summary": {"newestObservationAgeSec": NaN}}', 'unreadable'),
    ('{"writtenTs": Infinity}', 'unreadable'), (b'\xff\xfe', 'unreadable'),
])
def test_health_radar_unavailable_never_breaks_health(serve_at, tmp_path, content, reason):
    _, url = serve_at(_payload())
    path = tmp_path/'radar-health.json'
    if isinstance(content, bytes):
        path.write_bytes(content)
    elif content is not None:
        path.write_text(content)
    status, health = _get(url + '/health')
    assert (status, health['status']) == (200, 'ok')
    assert health['radar']['available'] is False and reason in health['radar']['reason']


def test_health_radar_survives_an_engine_error(serve_at, tmp_path):
    _, url = serve_at(None)
    (tmp_path/'radar-health.json').write_text(json.dumps(dict(summary=dict(state='current'), writtenTs=time.time())))
    status, health = _get(url + '/health')
    assert (status, health['status']) == (503, 'error')
    assert health['radar']['available'] is True


def test_health_no_longer_reads_radar_from_wx_json(serve_at):
    _, url = serve_at(_payload(radar=dict(health=dict(hedges=99))))
    _, health = _get(url + '/health')
    assert health['radar'] == dict(available=False, reason='radar-health.json missing')
