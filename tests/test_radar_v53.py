"""Single settled camera, warm-only hedges and retained source transitions."""
import json
import socket
import ssl
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest

from lib import almanac_emit as ae, radar_http as http
from lib.radar_fetch import Attempt, HostHealth, tile_race
from tests.test_freshness_health import serve_at, _get  # noqa: F401
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_keepalive import origin, get  # noqa: F401


def test_settled_report_is_intent_and_durable_is_only_default(serve_at, make_emitter, hybrid, tmp_path):
    (tmp_path/'radar_zoom').write_text('8')
    e = make_emitter()
    e._do_radar()
    assert e._radar_result.zoom == 8
    (tmp_path/'radar_intent').write_text(json.dumps(dict(seq=1, zoom=8, center='station')))
    module, url = serve_at({})
    q = '/wx.json?view=radar&radarTheme=paper&radarGeoZoom=7&radarGeoCenter=47.61,-122.33'
    _get(url+q+'&radarMoving=1')
    assert e._radar_read_intent()['zoom'] == 8
    _get(url+q+'&radarMoving=0')
    assert e._radar_read_intent()['zoom'] == 7
    e._do_radar()
    assert e._radar_result.zoom == 7
    module._camera_persist_timer.join(2); module._flush_preferences()  # preference writer thread: wait for the durable write
    assert (tmp_path/'radar_zoom').read_text().strip() == '7'
    stamp = e._radar_preference_stamp()
    _get(url+q+'&radarMoving=0')
    assert e._radar_preference_stamp() == stamp
    # Old zoom endpoint and out-of-band durable writes cannot fight live intent.
    _get(url+'/wx.json?radarSeq=999999999998&radarZoom=9&radarSource=mosaic&radarCenter=station')
    (tmp_path/'radar_zoom').write_text('9')
    assert e._radar_read_intent()['zoom'] == 7
    e._do_radar()
    assert e._radar_result.zoom == 7


def test_handshake_admission_two_and_ticket_context(origin, monkeypatch):
    active = peak = 0
    lock = threading.Lock()
    original = ssl.SSLSocket.do_handshake
    def slow(sock, *args, **kwargs):
        nonlocal active, peak
        if sock.server_side:
            return original(sock, *args, **kwargs)
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(.08)
            return original(sock, *args, **kwargs)
        finally:
            with lock:
                active -= 1
    monkeypatch.setattr(ssl.SSLSocket, 'do_handshake', slow)
    session = http.RadarSession()
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            assert all(pool.map(lambda _: get(session, origin, timeout=3), range(6)))
        assert peak == 2
        conns = session.connections[('localhost', int(origin.url.rsplit(':', 1)[1]))]
        assert len({id(c._context) for c in conns}) == 1
        assert session._tls_sessions  # TLS 1.3 tickets are received while reading
        # Force a fresh connection with the captured ticket and inspect resumption.
        control = Attempt(fresh=True)
        req = urllib.request.Request(origin.url+'/resumed'); req.radar_attempt = control
        with session.open(req, 2) as response:
            response.read()
            assert response.conn.sock.session_reused
    finally:
        session.close()


def test_hedge_never_connects_or_waits_for_pool(origin, monkeypatch):
    session = http.RadarSession()
    assert session.reserve_hedge(origin.url) is None
    get(session, origin)
    lease = session.reserve_hedge(origin.url)
    assert lease is not None
    def forbidden(*args):
        pytest.fail('hedge attempted a connection/handshake')
    monkeypatch.setattr(http._Connection, 'connect', forbidden)
    req = urllib.request.Request(origin.url+'/hedge')
    req.radar_attempt = Attempt(hedged=True, warm_lease=lease)
    try:
        with session.open(req, 2) as response:
            assert response.read()
        assert origin.connections == 1
    finally:
        req.radar_attempt.cancel()
        session.close()


def test_local_setup_timeout_is_not_host_failure(origin, monkeypatch):
    original = ssl.SSLSocket.do_handshake
    def timeout(sock, *args, **kwargs):
        if not sock.server_side:
            raise socket.timeout('fake slow handshake')
        return original(sock, *args, **kwargs)
    monkeypatch.setattr(ssl.SSLSocket, 'do_handshake', timeout)
    session = http.RadarSession()
    health = HostHealth()
    try:
        for _ in range(7):
            with pytest.raises(http.LocalTransportError) as caught:
                get(session, origin)
            health.record('iem', origin.url, False, caught.value)
        h = health.snapshot()
        assert h['breaker'] == 'closed' and h['successRate60s'] is None and h['localFailures'] == 7
        health.record('iem', origin.url, False, ConnectionResetError('remote reset'))
        assert health.snapshot()['successRate60s'] == 0
    finally:
        session.close()


def test_adaptive_hedging_rolling_window_and_five_minute_suspension(monkeypatch):
    now = [100.]
    monkeypatch.setattr(time, 'monotonic', lambda: now[0])
    h = HostHealth()
    for _ in range(4): h.issue_hedge()
    h.discard_hedges(2)
    assert h.hedge_allowed()  # exactly 50% is permitted
    h.discard_hedges(1)
    assert not h.hedge_allowed()
    now[0] += 299.9
    assert not h.hedge_allowed()
    now[0] += .1
    assert h.hedge_allowed()
    h.issue_hedge(); now[0] += 61
    assert h.hedge_allowed()


def test_failed_pass_hysteresis_and_local_exclusion(make_emitter):
    e = make_emitter()
    for _ in range(9):
        assert not e._radar_failed_pass('iem', http.LocalTransportError('TLS'), {})
    assert 'iem' not in e._radar_transport_failures
    assert not e._radar_failed_pass('iem', OSError('host'), {})
    assert not e._radar_failed_pass('iem', OSError('host'), {})
    assert e._radar_failed_pass('iem', OSError('host'), {})


def test_alternating_passes_retain_source_three_failures_then_dwell(make_emitter, hybrid, tmp_path, monkeypatch):
    (tmp_path/'radar_viewed').write_text(str(hybrid.now))
    e = make_emitter()
    e._do_radar()
    assert sum(f['complete'] for f in e._radar_result.frames) >= 8
    primary = e._radar_iem_frames
    def failed(ctx): raise ConnectionResetError('fake MRMS reset')
    for _ in range(4):
        monkeypatch.setattr(e, '_radar_iem_frames', failed)
        e._do_radar(intent_triggered=False)
        assert e._radar_result.source_id == 'iem-mrms-lcref'
        monkeypatch.setattr(e, '_radar_iem_frames', primary)
        e._do_radar(intent_triggered=False)
    old = e._radar_result
    published = []
    monkeypatch.setattr(e, '_radar_emit_now', lambda: published.append(e._radar_result))
    messages = []
    monkeypatch.setattr(ae.Logger, 'info', messages.append)
    monkeypatch.setattr(e, '_radar_iem_frames', failed)
    for _ in range(2):
        e._do_radar(intent_triggered=False)
        assert e._radar_result is old
    # Give this fallback pass a fresh rolling budget, without changing dwell.
    e._radar_request_times.clear()
    e._do_radar(intent_triggered=False)
    assert e._radar_result.source_id == 'rainviewer'
    # Auto publishes a switch once its newest frame is complete; the page keeps
    # the old loop on screen until four of the new one are decoded.
    assert all(s is old or s.source_id == 'rainviewer' and s.frames[-1]['complete'] for s in published)
    assert any('SWITCH iem-mrms-lcref -> rainviewer; reason=' in m and '3 consecutive' in m for m in messages)
    monkeypatch.setattr(e, '_radar_iem_frames', primary)
    e._do_radar(intent_triggered=False)
    assert e._radar_result.source_id == 'rainviewer'
    hybrid.mono += 301
    hybrid.latest += 240
    (tmp_path/'radar_viewed').write_text(str(hybrid.now+hybrid.mono))
    e._do_radar(intent_triggered=False)
    assert e._radar_result.source_id == 'iem-mrms-lcref'
    assert any('reason=preferred source recovered after 300s dwell' in m for m in messages)


def test_two_attempts_have_individual_timeouts_within_batch_deadline(origin, make_emitter, monkeypatch):
    e = make_emitter(); e._radar_session = http.RadarSession()
    monkeypatch.setattr(ae, 'RADAR_TILE_TIMEOUT_SEC', .4)
    origin.behavior = lambda path, ordinal: 'fail' if ordinal == 1 else 'hang'
    deadlines = []
    request = e._radar_request
    def record(source, url, deadline, **kwargs):
        deadlines.append(deadline)
        return request(source, url, deadline, **kwargs)
    monkeypatch.setattr(e, '_radar_request', record)
    started = time.monotonic()
    try:
        ctx = dict(zoom=8, tiles=[(0, 1, 0, 0)], tile_workers=4)
        result = list(e._radar_tile_batch('iem-mrms-lcref', 1, ctx, started+3,
                      lambda x, y: origin.url+'/deadline-tile', None))
        assert not result and ctx['missing_tiles']
        assert time.monotonic()-started < .8
        assert len(origin.requests) == len(deadlines) == 2
        assert started < deadlines[0] <= deadlines[1] < started+.8
    finally:
        e._radar_session.close()


def test_unissued_hedge_does_not_count_as_discarded():
    class Lease:
        def close(self): pass
    discarded = []
    def request(control, retry):
        if retry:
            raise OSError('denied admission')
        time.sleep(.04)
        return b'primary'
    assert tile_race(request, time.monotonic()+1, .01, Lease, discarded.append) == b'primary'
    assert sum(discarded) == 0


def test_late_hedge_admission_is_counted_after_drain():
    class Lease:
        def close(self): pass
    hedge_started = threading.Event()
    lost = []
    def request(control, retry):
        if not retry:
            assert hedge_started.wait(1)
            return b'primary'
        hedge_started.set()
        assert control.cancelled.wait(1)
        control.issued = True  # wire admission overlapped primary completion
        raise OSError('cancelled after admission')
    assert tile_race(request, time.monotonic()+2, .01, Lease, lost.append) == b'primary'
    assert lost == [1]


def test_real_response_deadline_is_host_failure_not_discard(origin, make_emitter, monkeypatch):
    e = make_emitter(); e._radar_session = http.RadarSession()
    monkeypatch.setattr(ae, 'RADAR_TILE_TIMEOUT_SEC', .3)
    origin.hang = True
    try:
        ctx = dict(zoom=8, tiles=[(0, 1, 0, 0)], tile_workers=4)
        list(e._radar_tile_batch('iem-mrms-lcref', 1, ctx, time.monotonic()+2,
             lambda x, y: origin.url+'/response-deadline', None))
        h = e._radar_health.snapshot()
        assert h['localFailures'] == 0 and h['successRate60s'] == 0
        assert h['discardedHedges'] == 0
    finally:
        e._radar_session.close()


def test_real_handshake_deadline_stays_local_during_race_cleanup(origin, make_emitter, monkeypatch):
    e = make_emitter(); e._radar_session = http.RadarSession()
    monkeypatch.setattr(ae, 'RADAR_TILE_TIMEOUT_SEC', .15)
    handshake = ssl.SSLSocket.do_handshake
    def slow_server(sock, *args, **kwargs):
        if sock.server_side:
            time.sleep(.3)  # real client TLS blocks and hits its socket deadline
        return handshake(sock, *args, **kwargs)
    monkeypatch.setattr(ssl.SSLSocket, 'do_handshake', slow_server)
    try:
        ctx = dict(zoom=8, tiles=[(0, 1, 0, 0)], tile_workers=4)
        list(e._radar_tile_batch('iem-mrms-lcref', 1, ctx, time.monotonic()+2,
             lambda x, y: origin.url+'/handshake-deadline', None))
        h = e._radar_health.snapshot()
        assert h['localFailures'] >= 1 and h['successRate60s'] is None
        assert h['breaker'] == 'closed' and h['discardedHedges'] == 0
    finally:
        e._radar_session.close()
