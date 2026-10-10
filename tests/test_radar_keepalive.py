"""Real TLS keep-alive regressions, including response-byte and worker fences."""
from http import client
import json
import socket
import ssl
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from lib import almanac_emit as ae, radar_http as http
from lib import radar_engine
from tests.test_emitter_lifecycle import FakeClock
from tests.test_radar_hybrid import hybrid, png  # noqa: F401


@pytest.fixture
def origin(tmp_path, monkeypatch):
    cert, key = tmp_path/'cert.pem', tmp_path/'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-keyout', str(key), '-out', str(cert), '-days', '1',
                    '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'],
                   check=True, capture_output=True)
    state = SimpleNamespace(connections=0, requests=[], closed=0, idle=2,
                            close_after=0, alternate=False, fail_fresh=False,
                            headers={}, second=None, delay=0, active=0, peak=0, hold=0, body=png(),
                            newest_ts=None, behavior=None, response=None, path_counts={}, hang=False, hang_ids=set(), hang_path=None, release=threading.Event(), drip=None)
    lock = threading.Lock()
    gate = threading.Condition(lock)  # hold: tile requests wait until `hold` are in flight (deterministic peak)
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def setup(self):
            super().setup()
            with lock:
                state.connections += 1
                self.ident = state.connections
            self.count = 0
            self.connection.settimeout(state.idle)
        def finish(self):
            try:
                super().finish()
            finally:
                with lock: state.closed += 1
        def log_message(self, *args): pass
        def handle(self):
            try:
                super().handle()
            except (ConnectionError, ssl.SSLError):
                pass  # the client deliberately drops timed-out/partial responses
        def do_HEAD(self): self.do_GET()
        def do_GET(self):
            self.count += 1
            with lock: state.requests.append((self.ident, self.command, self.path))
            with lock:
                state.path_counts[self.path] = state.path_counts.get(self.path, 0)+1
                ordinal = state.path_counts[self.path]
            behavior = state.behavior(self.path, ordinal) if state.behavior else 'normal'
            if behavior == 'fail':
                self.send_error(503)
                return
            if behavior in ('slow', 'body'):
                self.send_response(200)
                self.send_header('Content-Length', str(len(state.body)))
                self.end_headers()
                self.wfile.flush()
                state.release.wait(getattr(state, "stall_seconds", 4))
                try:
                    self.wfile.write(state.body)
                except OSError:
                    pass
                return
            if behavior == 'hang' or state.hang or self.ident in state.hang_ids or (state.hang_path and self.path.startswith(state.hang_path)):
                # Read the complete request, then send nothing and keep TLS open.
                state.release.wait(40)
                self.close_connection = True
                return
            if state.drip is not None:
                data, interval = state.drip
                try:
                    for byte in data:
                        self.wfile.write(bytes([byte])); self.wfile.flush()
                        if state.release.wait(interval): break
                except (OSError, ssl.SSLError):
                    pass
                self.close_connection = True
                return
            if state.fail_fresh:
                self.close_connection = True
                return
            if state.second is not None and self.count == 2:
                self.wfile.write(state.second)
                self.wfile.flush()
                time.sleep(.2)
                self.close_connection = True
                return
            with gate:
                state.active += 1
                state.peak = max(state.peak, state.active)
                if state.hold and self.path.startswith('/tile'):
                    # A barrier, not a sleep: concurrency is asserted by construction, so a slow
                    # CI runner cannot turn "six workers" into a wall-clock coincidence.
                    deadline = time.monotonic() + 5
                    while state.active < state.hold and time.monotonic() < deadline:
                        gate.wait(timeout=.05)
                    gate.notify_all()
            time.sleep(state.delay)
            with lock: state.active -= 1
            raw = state.body
            if self.path == '/metadata':
                stamp = state.newest_ts or int(time.time())//120*120
                raw = json.dumps(dict(meta=dict(product='lcref', units='0.5 dBZ',
                    end_valid=datetime.fromtimestamp(stamp, timezone.utc).isoformat()))).encode()
            if state.response is not None:
                raw = state.response(self.path, raw)
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            for k, v in state.headers.items(): self.send_header(k, v)
            self.end_headers()
            if self.command != 'HEAD': self.wfile.write(raw)
            if state.close_after and self.count >= state.close_after and (not state.alternate or self.ident % 2 == 0):
                # Deliberately omit Connection: close, as an idle upstream can do.
                self.close_connection = True
    try:
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    except PermissionError:
        # Some hermetic runners prohibit even loopback listeners. This fixture
        # tests a real local TLS transport, so it cannot provide useful cover
        # there; do not mistake sandbox policy for a product failure.
        pytest.skip('loopback sockets unavailable in this environment')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    original = http._Connection
    class LocalConnection(original):
        def __init__(self, *args):
            super().__init__(*args)
            self._context = ssl.create_default_context(cafile=str(cert))
    monkeypatch.setattr(http, '_Connection', LocalConnection)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f'https://localhost:{server.server_port}'
    try:
        yield state
    finally:
        state.release.set()
        server.shutdown(); server.server_close(); thread.join(5)


def get(session, origin, method='GET', timeout=2):
    with session.open(urllib.request.Request(origin.url+'/tile', method=method), timeout) as response:
        return response.read()


def until(predicate):
    end = time.perf_counter()+2
    while not predicate():
        assert time.perf_counter() < end
        time.sleep(.005)


@pytest.mark.parametrize('mode', ['idle', 'requests'])
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
def test_stale_socket_retried_once(origin, mode, method):
    origin.idle = .06
    origin.close_after = 1 if mode == 'requests' else 0
    session = http.RadarSession()
    try:
        get(session, origin, method)
        until(lambda: origin.closed == 1)
        get(session, origin, method)
        assert session.retries == 1 and origin.connections == 2
        assert len(origin.requests) == 2
    finally: session.close()


def test_fresh_failure_and_failed_retry_propagate(origin):
    origin.close_after = 1
    session = http.RadarSession()
    try:
        get(session, origin)
        until(lambda: origin.closed == 1)
        origin.fail_fresh = True
        with pytest.raises(client.RemoteDisconnected): get(session, origin)
        assert session.retries == 1 and origin.connections == 2
        with pytest.raises(client.RemoteDisconnected): get(session, origin)
        assert session.retries == 1 and origin.connections == 3
    finally: session.close()


@pytest.mark.parametrize('headers,age,reconnect', [
    ({}, 1.9, False), ({}, 2.01, True),
    ({'Keep-Alive': 'max=100, timeout=1'}, .8, True),
    ({'Keep-Alive': 'timeout=75'}, 2.01, True),
    ({'Keep-Alive': 'timeout=0'}, 0, True),
    ({'Connection': 'close'}, 0, True),
])
def test_idle_bounds_and_headers(origin, headers, age, reconnect):
    origin.headers = headers
    session = http.RadarSession()
    try:
        get(session, origin)
        # Age only the pool's idle timestamp; no global/fake network clock.
        for conn in session._used: session._used[conn] -= age
        get(session, origin)
        assert origin.connections == (2 if reconnect else 1)
        assert session.retries == 0
    finally: session.close()


@pytest.mark.parametrize('partial', [b'garbage\r\n', b'H', b'HTTP/1.1 200 OK\r\nX: '])
def test_response_bytes_never_replayed(origin, partial):
    session = http.RadarSession()
    try:
        get(session, origin)
        origin.second = partial
        with pytest.raises((client.BadStatusLine, socket.timeout)):
            get(session, origin, timeout=.1)
        assert session.retries == 0 and origin.connections == 1
    finally: session.close()


def test_zero_byte_timeout_uses_original_deadline(origin):
    session = http.RadarSession()
    try:
        get(session, origin)
        origin.second = b''
        start = time.perf_counter()
        with pytest.raises(socket.timeout): get(session, origin, timeout=.08)
        # No remaining time to retry; never grant a second full timeout.
        assert time.perf_counter()-start < .18
        assert session.retries == 0 and origin.connections == 1
    finally: session.close()


@pytest.mark.parametrize('error', [client.RemoteDisconnected(), client.BadStatusLine(''),
    ConnectionResetError(), BrokenPipeError(), ssl.SSLEOFError(), ssl.SSLZeroReturnError(), socket.timeout()])
def test_zero_byte_failures_retry_but_post_does_not(origin, monkeypatch, error):
    session = http.RadarSession()
    try:
        get(session, origin)
        conn = next(iter(session._used))
        def fail(*args, **kwargs): raise error
        monkeypatch.setattr(conn, 'getresponse', fail)
        get(session, origin)
        assert session.retries == 1
        conn = next(iter(session._used))
        monkeypatch.setattr(conn, 'request', fail)
        with pytest.raises(type(error)): get(session, origin, method='POST')
        assert session.retries == 1
    finally: session.close()


def test_six_workers_complete_frame_with_closing_connections(make_emitter, origin, tmp_path, monkeypatch):
    origin.close_after = 1
    origin.alternate = True
    origin.delay = .02
    origin.hold = 6      # tile requests are released only once six are in flight
    # Region transport; Auto would also list sites.
    monkeypatch.setattr(radar_engine.RadarEngine, '_auto_source', lambda self, ctx, site_ok: 'mosaic')
    monkeypatch.setattr(radar_engine, 'RADAR_DIR', str(tmp_path/'radar'))
    monkeypatch.setattr(radar_engine, 'RADAR_IEM_METADATA_URL', origin.url+'/metadata')
    monkeypatch.setattr(radar_engine, 'RADAR_IEM_ARCHIVE_TEMPLATE', origin.url+'/archive/%Y%m%d%H%M')
    monkeypatch.setattr(radar_engine, 'RADAR_IEM_TILE_TEMPLATE', origin.url+'/tile/{stamp}/{z}/{x}/{y}')
    warnings, infos = [], []
    monkeypatch.setattr(ae.Logger, 'warning', warnings.append)
    monkeypatch.setattr(ae.Logger, 'info', infos.append)
    emitter = make_emitter()
    try:
        emitter.radar._acquire()
        assert emitter.radar._result.available
        assert emitter.radar._result.source_id == 'iem-mrms-lcref'
        assert sum(f['complete'] for f in emitter.radar._result.frames) == 1
        session = emitter.radar._session
        assert origin.peak == 6
        assert len(session.connections[('localhost', int(origin.url.rsplit(':',1)[1]))]) <= 6
        assert emitter.radar._health.retries > 0
        assert len(emitter.radar._request_times) == len(origin.requests) + emitter.radar._health.retries
        assert not warnings and not any('SWITCH' in line for line in infos)
    finally:
        if emitter.radar._session: emitter.radar._session.close()


@pytest.mark.parametrize('phase', ['metadata', 'tile'])
def test_primary_transport_failure_falls_back_in_new_geometry_then_recovers(make_emitter, hybrid, tmp_path, monkeypatch, phase):
    emitter = make_emitter(); emitter.radar._acquire()
    previous = emitter.radar._result
    clock = FakeClock(); monkeypatch.setattr(ae, 'Clock', clock); emitter._runtime.running = True
    (tmp_path/'radar_intent').write_text(json.dumps(dict(seq=1, zoom=7, center='station')))
    def fail(req, timeout):
        if (phase == 'metadata' and req.full_url == radar_engine.RADAR_IEM_METADATA_URL or
                phase == 'tile' and 'mrms::' in req.full_url):
            raise client.RemoteDisconnected('stale socket')
    hybrid.failure = fail
    hybrid.calls.clear()
    # Metadata outages are discovered by scheduled validation; warm intents
    # deliberately make no metadata request. Tile outages still exercise reuse.
    hybrid.view()
    for _ in range(3):
        emitter.radar._acquire(intent_triggered=False if phase == 'metadata' else True)
    assert emitter.radar._result.source_id == 'rainviewer'
    assert emitter.radar._refresh['state'] == 'idle'
    assert not emitter.radar._negative and any(c[0] == 'rainviewer' for c in hybrid.calls)
    hybrid.failure = None
    hybrid.mono += 301  # source dwell, also beyond any host circuit
    hybrid.latest += 240
    emitter.radar._acquire()
    assert emitter.radar._result.zoom == 7 and emitter.radar._result.source_id == 'iem-mrms-lcref'
    assert not emitter.radar._transport_failures
    emitter.stop()


def test_repeated_transport_outage_falls_back_and_recovers(make_emitter, hybrid):
    emitter = make_emitter(); emitter.radar._acquire()
    previous = emitter.radar._result
    def fail(req, timeout):
        if 'iastate.edu' in req.full_url: raise ConnectionResetError('outage')
    hybrid.failure = fail
    hybrid.view()
    for _ in range(3):
        emitter.radar._acquire(intent_triggered=False)
    assert emitter.radar._result.source_id == 'rainviewer'
    hybrid.failure = None
    hybrid.mono += 301
    hybrid.latest += 240
    emitter.radar._acquire()
    assert emitter.radar._result.source_id == 'iem-mrms-lcref'






def test_retry_obeys_shared_request_gate(make_emitter, origin):
    origin.close_after = 1
    emitter = make_emitter()
    source = 'iem-mrms-lcref'
    session = http.RadarSession(on_retry=lambda end, **kw: emitter.radar._transport_retry(source, end, **kw))
    emitter.radar._session = session
    try:
        get(session, origin)
        until(lambda: origin.closed == 1)
        emitter.radar._request_times = [time.monotonic()] * (radar_engine.RADAR_REQUESTS_PER_MIN-1)
        with pytest.raises(radar_engine._RadarBudget):
            emitter.radar._request(source, origin.url+'/tile', time.monotonic()+2)
        assert len(emitter.radar._request_times) == radar_engine.RADAR_REQUESTS_PER_MIN
        assert session.retries == emitter.radar._transport_retries == 0
        assert origin.connections == 1
    finally: session.close()
