"""Silent-path regressions: real TLS on loopback only; no provider or LAN I/O."""
from concurrent.futures import ThreadPoolExecutor
import socket
import threading
import time
import urllib.request
from unittest.mock import Mock

import pytest

from lib import almanac_emit as ae, radar_http as http
from lib import radar_engine
from tests.test_radar_keepalive import origin, get  # noqa: F401


def prime(session, origin, count=6):
    # Keep all leases until six distinct TCP/TLS connections are established.
    responses = [session.open(urllib.request.Request(origin.url+'/prime'), 2)
                 for _ in range(count)]
    for response in responses:
        response.read()
        response.close()
    return {ident for ident, _, _ in origin.requests}


@pytest.mark.parametrize('method', ['GET', 'HEAD'])
def test_reused_hang_retries_once_on_fresh_tls(origin, method):
    session = http.RadarSession(first_byte_timeout=.12)
    try:
        origin.hang_ids = prime(session, origin, 1)
        start = time.monotonic()
        get(session, origin, method)
        elapsed = time.monotonic()-start
        assert .1 <= elapsed < .7
        assert session.retries == session.stale_first_byte_retries == 1
        assert [r[0] for r in origin.requests] == [1, 1, 2]
    finally:
        session.close()


@pytest.mark.parametrize('reused', [False, True])
def test_fresh_hang_and_hanging_retry_never_replayed(origin, reused):
    session = http.RadarSession(first_byte_timeout=.1)
    try:
        if reused:
            prime(session, origin, 1)
        origin.hang = True
        start = time.monotonic()
        with pytest.raises(socket.timeout):
            get(session, origin, timeout=.4)
        elapsed = time.monotonic()-start
        assert .35 <= elapsed < .7
        assert session.retries == session.stale_first_byte_retries == int(reused)
        assert origin.connections == 1 + int(reused)
    finally:
        session.close()


@pytest.mark.parametrize('fresh_hangs', [False, True])
def test_six_hanging_reused_workers_share_deadline(make_emitter, origin, monkeypatch, tmp_path, fresh_hangs):
    emitter = make_emitter()
    monkeypatch.setattr(radar_engine, 'RADAR_DIR', str(tmp_path/'radar'))
    # This measures the shared deadline and retry accounting. A hedge is legal
    # here whenever one worker's retry frees a warm socket before another has
    # waited RADAR_HEDGE_SEC; the six hang in lockstep only on an idle machine
    # (a loaded CI runner staggered them and hedged once, 2026-09-25). Hedging
    # has its own tests; keep it out of this timeline.
    monkeypatch.setattr(radar_engine, 'RADAR_HEDGE_SEC', 60)
    infos = []
    monkeypatch.setattr(ae.Logger, 'info', infos.append)
    source = 'iem-mrms-lcref'
    session = http.RadarSession(on_retry=lambda end, **kw: emitter.radar._transport_retry(source, end, **kw))
    emitter.radar._session = session
    try:
        origin.hang_ids = prime(session, origin)
        # Priming through the raw session bypasses emitter health; account for
        # those six real successful requests as production admission does.
        for _ in origin.hang_ids:
            emitter.radar._health.record(source, origin.url, True)
        origin.hang = fresh_hangs
        ctx = dict(zoom=8, tiles=[(i, 1, 0, 0) for i in range(12)], tile_workers=6)
        start = time.monotonic()
        deadline = start + 3.7
        session.begin_pass(deadline)
        result = lambda: list(emitter.radar._tile_batch(source, 1, ctx, deadline,
                              lambda x, y: origin.url+f'/tile/{x}', None))
        if fresh_hangs:
            with pytest.raises((socket.timeout, radar_engine._RadarBudget)):
                result()
        else:
            assert len(result()) == 12
        elapsed = time.monotonic()-start
        print(f'six reused TLS sockets, fresh_hangs={fresh_hangs}: {elapsed:.3f}s')
        assert (3.5 if fresh_hangs else 3) <= elapsed < 4.2
        assert session.retries == session.stale_first_byte_retries == 0
        assert emitter.radar._health.hedges == 0
        assert emitter.radar._health.retries == 6
        assert not infos  # tiles use the v4.9 race; pass logging lives in _do_radar
        assert not session._busy  # executor drained, no abandoned socket workers
        assert all(ident > 6 for ident, _, _ in origin.requests[12:])
        if fresh_hangs:
            assert len(origin.requests) == 18  # six prime, six dead, six sequential retries
        else:
            assert len(origin.requests) == 24  # second tile wave also succeeds
    finally:
        session.close()


def test_pass_pool_waiters_do_not_get_new_budgets(origin):
    session = http.RadarSession()
    origin.hang = True
    try:
        start = time.monotonic()
        session.begin_pass(start+.4)
        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = [pool.submit(get, session, origin, timeout=25) for _ in range(12)]
            for future in futures:
                with pytest.raises(socket.timeout):
                    future.result(timeout=1)
        assert time.monotonic()-start < .8
        assert len(origin.requests) == 6
        assert session.retries == 0 and not session._busy
        with pytest.raises(socket.timeout):
            get(session, origin, timeout=25)
        assert len(origin.requests) == 6
    finally:
        session.close()


@pytest.mark.parametrize('phase', ['headers', 'body'])
def test_trickled_response_obeys_absolute_deadline(origin, phase):
    session = http.RadarSession(first_byte_timeout=.06)
    try:
        prime(session, origin, 1)
        if phase == 'headers':
            origin.drip = (b'HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\nx', .03)
        else:
            # Header + body bytes still require repeated raw reads.
            origin.drip = (b'HTTP/1.1 200 OK\r\nContent-Length: 999\r\n\r\n'+b'x'*999, .001)
        start = time.monotonic()
        with pytest.raises(socket.timeout):
            get(session, origin, timeout=.3)
        assert .25 <= time.monotonic()-start < .6
        assert session.retries == session.stale_first_byte_retries == 0
    finally:
        session.close()


def test_first_byte_restores_full_budget(origin):
    session = http.RadarSession(first_byte_timeout=.1)
    try:
        prime(session, origin, 1)
        raw = b'HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\nx'
        origin.drip = (raw, .008)
        start = time.monotonic()
        assert get(session, origin, timeout=1) == b'x'
        assert time.monotonic()-start > .25
        assert session.retries == 0
    finally:
        session.close()


def test_cold_dns_wait_is_bounded(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def lookup(*args):
        entered.set()
        release.wait(2)
        raise socket.gaierror('test resolver released')
    monkeypatch.setattr(http.socket, 'getaddrinfo', lookup)
    session = http.RadarSession()
    try:
        start = time.monotonic()
        session.begin_pass(start+.1)
        with pytest.raises(socket.timeout):
            session.open(urllib.request.Request('https://resolver.invalid/tile'), 25)
        assert entered.is_set() and time.monotonic()-start < .4
    finally:
        release.set()
        session.close()


def test_keepalive_enabled_on_real_pooled_socket(origin):
    session = http.RadarSession()
    try:
        get(session, origin)
        conn = next(iter(session._used))
        assert conn.sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
        for name in ('TCP_KEEPIDLE', 'TCP_KEEPINTVL', 'TCP_KEEPCNT'):
            option = getattr(socket, name, None)
            if option is not None:
                assert conn.sock.getsockopt(socket.IPPROTO_TCP, option) == 2
    finally:
        session.close()


def test_linux_keepalive_options_and_unsupported_platforms(monkeypatch):
    sock = Mock()
    for i, name in enumerate(('TCP_KEEPIDLE', 'TCP_KEEPINTVL', 'TCP_KEEPCNT'), 100):
        monkeypatch.setattr(socket, name, i, raising=False)
    http._tcp_keepalive(sock)
    assert [c.args for c in sock.setsockopt.call_args_list] == [
        (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
        (socket.IPPROTO_TCP, 100, 2), (socket.IPPROTO_TCP, 101, 2), (socket.IPPROTO_TCP, 102, 2)]
    sock.setsockopt.side_effect = OSError('unsupported')
    http._tcp_keepalive(sock)
    for name in ('TCP_KEEPIDLE', 'TCP_KEEPINTVL', 'TCP_KEEPCNT'):
        monkeypatch.delattr(socket, name)
    http._tcp_keepalive(sock)


def test_retried_pass_succeeds_without_failure_note(make_emitter, origin, monkeypatch, tmp_path):
    # Region transport; Auto would also list sites.
    monkeypatch.setattr(radar_engine.RadarEngine, '_auto_source', lambda self, ctx, site_ok: 'mosaic')
    emitter = make_emitter()
    session = http.RadarSession(first_byte_timeout=.12)
    emitter.radar._session, emitter.radar._provider = session, radar_engine._RADAR_SOURCES['iem-mrms-lcref']['provider']
    monkeypatch.setattr(radar_engine, 'RADAR_DIR', str(tmp_path/'radar'))
    monkeypatch.setattr(radar_engine, 'RADAR_IEM_METADATA_URL', origin.url+'/metadata')
    monkeypatch.setattr(radar_engine, 'RADAR_IEM_ARCHIVE_TEMPLATE', origin.url+'/archive/%Y%m%d%H%M')
    monkeypatch.setattr(radar_engine, 'RADAR_IEM_TILE_TEMPLATE', origin.url+'/tile/{stamp}/{z}/{x}/{y}')
    warnings, infos = [], []
    monkeypatch.setattr(ae.Logger, 'warning', warnings.append)
    monkeypatch.setattr(ae.Logger, 'info', infos.append)
    try:
        origin.hang_ids = prime(session, origin, 1)
        emitter.radar._acquire()
        assert emitter.radar._result.ts_frame is not None
        assert emitter.radar._refresh['state'] == 'idle'
        assert not warnings and not emitter.radar._transport_failures
        assert session.retries == emitter.radar._stale_first_byte_retries == 1
        assert len(infos) == 2 and 'stale_first_byte_retries=1' in infos[0]
        assert 'radar pass' in infos[1]
    finally:
        session.close()


@pytest.mark.parametrize('budget', [.8, 25])
def test_full_engine_pass_deadline_includes_all_tile_workers(make_emitter, origin, monkeypatch, tmp_path, budget):
    assert radar_engine.RADAR_BUILD_DEADLINE_SEC == radar_engine.RADAR_PRIMARY_DEADLINE_SEC == 25
    emitter = make_emitter()
    monkeypatch.setattr(radar_engine, 'RADAR_DIR', str(tmp_path/'radar'))
    monkeypatch.setattr(radar_engine, 'RADAR_IEM_METADATA_URL', origin.url+'/metadata')
    monkeypatch.setattr(radar_engine, 'RADAR_IEM_ARCHIVE_TEMPLATE', origin.url+'/archive/%Y%m%d%H%M')
    monkeypatch.setattr(radar_engine, 'RADAR_IEM_TILE_TEMPLATE', origin.url+'/tile/{stamp}/{z}/{x}/{y}')
    # Exercise both a shortened and the real 25-second pass budget. A larger
    # request cap must never escape it. Metadata and HEAD remain normal.
    monkeypatch.setattr(radar_engine, 'RADAR_BUILD_DEADLINE_SEC', budget)
    monkeypatch.setattr(radar_engine, 'RADAR_PRIMARY_DEADLINE_SEC', budget)
    monkeypatch.setattr(radar_engine, 'RADAR_HTTP_TIMEOUT_SEC', 60)
    monkeypatch.setattr(radar_engine, 'RADAR_RAINVIEWER_MANIFEST_URL', origin.url+'/unavailable-fallback')
    # The default view is in site range, so Auto lists the nearest site's scans.
    # Hermetic: that listing (and anything it could lead to) is the local origin,
    # answering that the site has no recent scans, so the pass stays on Region.
    monkeypatch.setattr(radar_engine, 'RADAR_SITE_LIST_URL', origin.url+'/listing')
    monkeypatch.setattr(radar_engine, 'RADAR_SITE_TILE_TEMPLATE', origin.url+'/site/{site}/{stamp}/{z}/{x}/{y}')
    monkeypatch.setattr(radar_engine, 'RADAR_LEVEL3_BUCKET', origin.url+'/level3/')
    origin.response = lambda path, raw: b'{"scans":[]}' if path.startswith('/listing') else raw
    origin.hang_path = '/tile/'
    start = time.monotonic()
    try:
        emitter.radar._acquire()
        elapsed = time.monotonic()-start
        print(f'full engine pass, six hanging tile reads: {elapsed:.3f}s (budget {budget}s)')
        assert elapsed < budget+.4
        if budget == 25:
            assert elapsed < radar_engine.RADAR_SOURCE_DEADLINE_SEC+.4  # no rescue handshakes at a deadline
        retries = emitter.radar._stale_first_byte_retries
        assert 6 <= sum(path.startswith('/tile/') for _, _, path in origin.requests) <= 24
        assert any(path.startswith('/listing?') for _, _, path in origin.requests), 'the site listing escaped the origin'
        assert emitter.radar._session is None or not emitter.radar._session._busy
    finally:
        if emitter.radar._session:
            emitter.radar._session.close()
