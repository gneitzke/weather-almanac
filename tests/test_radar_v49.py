"""Hostile origin scenarios: real HTTPS and health HTTP, loopback only."""
import json
import os
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from lib import almanac_emit as ae, radar_http as http
from lib.radar_fetch import HostHealth, CircuitOpen
from tests.test_radar_keepalive import origin  # noqa: F401
from tests.test_freshness_health import serve_at, _get  # noqa: F401


def batch(emitter, origin, count=10, stamp=1, seconds=9, ctx=None):
    ctx = ctx or dict(zoom=8, tiles=[(i, 1, 0, 0) for i in range(count)], tile_workers=6)
    deadline = time.monotonic()+seconds
    emitter._radar_session.begin_pass(deadline)
    result = list(emitter._radar_tile_batch('iem-mrms-lcref', stamp, ctx, deadline,
                  lambda x, y: origin.url+f'/tile/{stamp}/{x}', None))
    return result, ctx


@pytest.fixture
def engine(make_emitter, origin):
    e = make_emitter()
    e._radar_session = http.RadarSession()
    yield e
    e._radar_session.close()


# Real TLS sockets and wall-clock hedge timers: this pair passes 20/20 in fresh processes but is
# load-sensitive under the full suite (a busy runner delays the origin's threads past the 2 s hedge
# boundary). The deterministic sibling covers the behaviour; this is the integration exercise.
# Run it on purpose: RADAR_SOCKET_TEST=1 pytest tests/test_radar_v49.py -k real_warm
@pytest.mark.skipif(os.environ.get('RADAR_SOCKET_TEST') != '1', reason='real-socket timing exercise; opt in with RADAR_SOCKET_TEST=1')
@pytest.mark.parametrize('behavior', ['hang', 'slow'])
def test_three_bad_tiles_real_warm_connections(engine, origin, behavior, monkeypatch):
    # Real sockets, deterministic inactivity: headers/body cannot accidentally
    # finish before the race, and hedge time advances only after warm leases AND
    # all three silent/header-only primaries have reached the origin.
    import threading
    from lib import radar_fetch as fetch
    ready = threading.Event()
    lock = threading.Lock()
    successes, controls = [], []
    clock = [0.]
    monkeypatch.setattr(fetch, 'hedge_now', lambda: clock[0])
    origin.stall_seconds = 40
    request = engine._radar_request
    def measured(*args, **kwargs):
        control = kwargs.get('attempt')
        bad = args[1].rsplit('/', 1)[-1] in ('0', '1', '2')
        if bad and not kwargs.get('retry'):
            with lock: controls.append(control)
        result = request(*args, **kwargs)
        if not bad:
            with lock:
                successes.append(args[1])
                if len(successes) == 7: ready.set()
        return result
    monkeypatch.setattr(engine, '_radar_request', measured)
    origin.behavior = lambda path, n: behavior if n == 1 and path.rsplit('/', 1)[-1] in ('0', '1', '2') else 'normal'
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(batch, engine, origin)
        assert ready.wait(5), 'healthy leases not returned'
        assert len(controls) == 3
        if behavior == 'slow':
            for control in controls:
                assert control.first_byte.wait(1), 'headers not received'
        assert all(origin.path_counts.get(f'/tile/1/{x}') == 1 for x in range(3))
        assert engine._radar_health.hedges == 0
        clock[0] = 2.01
        result, ctx = future.result(timeout=6)
    elapsed = time.monotonic()-start
    h = engine._radar_health.snapshot()
    assert len(result) == 10
    assert h['hedges'] == 3 and h['discardedHedges'] == 0
    assert h['stallHedges'] == (3 if behavior == 'slow' else 0)
    assert h['retries'] == 0  # winning hedges are not failure retries
    assert h['successRate60s'] == 1  # cancelled primaries are local, not host failures
    assert len(engine._radar_request_times) == len(origin.requests) == 13
    for x in range(3):
        ids = [ident for ident, _, path in origin.requests if path == f'/tile/1/{x}']
        assert len(ids) == len(set(ids)) == 2
    assert not engine._radar_session._busy
    print(f'{behavior}: 10/10 tiles; 3 hedges; elapsed={elapsed:.3f}s')


def test_fast_failure_retries_without_waiting_or_third_attempt(engine, origin):
    origin.behavior = lambda path, n: 'fail' if n == 1 else 'normal'
    result, _ = batch(engine, origin, count=2)
    h = engine._radar_health.snapshot()
    assert len(result) == 2 and h['hedges'] == 0 and h['retries'] == 2
    assert len(origin.requests) == len(engine._radar_request_times) == 4


def test_response_progress_without_warm_lease_does_not_hedge(engine, origin):
    # Both bodies are held by an event, not a 4 s clock: under full-suite load
    # the second request could start >2 s late, the first connection would
    # finish and go idle, and a legitimate hedge would fire (seen 2026-09-25).
    origin.behavior = lambda path, n: 'body'
    origin.stall_seconds = 30
    out = {}
    worker = threading.Thread(target=lambda: out.setdefault('batch', batch(engine, origin, count=2)))
    worker.start()
    try:
        arrived = time.monotonic() + 10
        while len(origin.requests) < 2 and time.monotonic() < arrived:
            time.sleep(.01)
        assert len(origin.requests) == 2
        time.sleep(ae.RADAR_HEDGE_SEC + .5)   # the hedge window passes with both responses in progress
        hedged = engine._radar_health.hedges
    finally:
        origin.release.set()
        worker.join(15)
    result, _ = out['batch']
    assert len(result) == 2 and hedged == 0 and engine._radar_health.hedges == 0
    assert len(origin.requests) == 2


def test_partial_and_next_pass_fetch_only_missing(engine, origin):
    origin.behavior = lambda path, n: 'fail' if path.endswith('/0') else 'normal'
    result, ctx = batch(engine, origin)
    assert len(result) == 9 and ctx['missing_tiles']
    before = len(origin.requests)
    origin.behavior = None
    result, _ = batch(engine, origin)
    assert len(result) == 10 and len(origin.requests)-before == 1


def test_deadline_and_all_busy_primary_slots(engine, origin):
    origin.behavior = lambda path, n: 'hang'
    start = time.monotonic()
    with pytest.raises((TimeoutError, ae._RadarBudget, CircuitOpen)):
        batch(engine, origin, count=12, seconds=2.3)
    assert time.monotonic()-start < 2.8
    # No idle connection means no hedge may create a rescue handshake.
    assert len(origin.requests) == 6
    assert not engine._radar_session._busy
    assert engine._radar_health.hedges == 0
    assert engine._radar_health.retries == 0  # losing hedges are not retries either


def test_hedge_cap_and_rate_gate(engine, origin, monkeypatch):
    monkeypatch.setattr(ae, 'RADAR_REQUESTS_PER_MIN', 7)
    origin.behavior = lambda path, n: 'hang' if n == 1 else 'normal'
    with pytest.raises((TimeoutError,ae._RadarBudget)):
        batch(engine, origin, count=6, seconds=2.5)
    assert len(engine._radar_request_times) <= 7
    assert len(origin.requests) <= 7
    assert engine._radar_health.hedges <= 3


def test_host_breaker_open_half_close_and_failed_probe(monkeypatch):
    health = HostHealth()
    now = [1.]
    monkeypatch.setattr('lib.radar_fetch.time.monotonic', lambda: now[0])
    url = 'https://host.invalid/meta'
    for i in range(6):
        health.admit('source', url, metadata=True)
        health.record('source', url, i < 2, error=None if i < 2 else 'hang')
    assert health.snapshot()['breaker'] == 'open'
    with pytest.raises(CircuitOpen): health.probes('source')
    now[0] += 30
    assert health.probes('source') == [(url, True)]
    assert health.admit('source', url, metadata=True)
    assert health.snapshot()['breaker'] == 'half'
    with pytest.raises(CircuitOpen): health.admit('source', url, metadata=True)
    health.record('source', url, False, 'probe failed', probe=True)
    assert health.snapshot()['breaker'] == 'open'
    now[0] += 30
    assert health.admit('source', url, metadata=True)
    health.record('source', url, True, probe=True)
    assert health.snapshot()['breaker'] == 'closed'
    now[0] += 60
    assert health.snapshot()['successRate60s'] is None


def test_health_endpoint(engine, serve_at):
    engine._radar_health.last_success = time.time()
    payload = engine._build_payload()
    assert 'health' not in payload['radar']  # diagnostics live in radar-health.json
    assert engine._radar_write_health(time.time(), force=True)
    written = json.loads((Path(engine.output_path).parent/'radar-health.json').read_text())
    _, url = serve_at(payload)
    _, response = _get(url+'/health')
    h = response['radar']
    assert {k: v for k, v in h.items() if k not in ('available', 'fileAgeSec')} == written
    assert set(('lastSuccessTs', 'successRate60s', 'hedges', 'retries', 'breaker', 'lastError')) <= h.keys()


from tests.test_radar_hybrid import hybrid  # noqa: E402,F401


def test_open_host_falls_back_in_same_pass_and_one_probe_recovers(make_emitter, hybrid, monkeypatch):
    e = make_emitter()
    source, url = 'iem-mrms-lcref', ae.RADAR_IEM_METADATA_URL
    for _ in range(6):
        e._radar_health.admit(source, url, metadata=True)
        e._radar_health.record(source, url, False, 'hang')
    e._do_radar()
    assert e._radar_result.source_id == 'rainviewer'
    assert all(c[0] == 'rainviewer' for c in hybrid.calls)
    assert e._radar_health.snapshot()['breaker'] == 'open'
    hybrid.mono += 30
    hybrid.calls.clear()
    # Probe observes the actual half state, and cannot get a second probe lease.
    observed = []
    def observe(req, _):
        if req.full_url == url:
            observed.append(e._radar_health.snapshot()['breaker'])
    hybrid.failure = observe
    e._do_radar(intent_triggered=False)
    assert e._radar_result.source_id == source
    assert observed == ['half']  # adapter consumes the probe bytes without a duplicate GET
    assert e._radar_health.snapshot()['breaker'] == 'closed'


def test_partial_publication_yields_before_history_and_repairs(make_emitter, hybrid, monkeypatch):
    import urllib.error
    e = make_emitter()
    original = e._radar_request
    failed = []
    def request(source, url, *args, **kwargs):
        if 'mrms::' in url:
            if not failed:
                failed.append(url)
            if url == failed[0]:
                raise urllib.error.HTTPError(url, 503, 'one missing tile', {}, None)
        return original(source, url, *args, **kwargs)
    monkeypatch.setattr(e, '_radar_request', request)
    hybrid.view()
    e._do_radar()
    snap = e._radar_result
    newest = next(f for f in snap.frames if f['ts'] == snap.ts_frame)
    assert snap.ts_frame == hybrid.latest and not newest['complete']
    assert e._radar_refresh['state'] == 'failed'
    assert not any('/7/' in c[2] or '/9/' in c[2] for c in hybrid.calls)
    monkeypatch.setattr(e, '_radar_request', original)
    # Stop background demand so the only new wire tile is the previous hole.
    monkeypatch.setattr(e, '_radar_is_viewed', lambda: False)
    hybrid.calls.clear()
    e._do_radar()
    newest = next(f for f in e._radar_frames if f['ts'] == snap.ts_frame)
    assert newest['complete'] and e._radar_refresh['state'] == 'idle'
    assert [c[2] for c in hybrid.calls if 'mrms::' in c[2]] == failed


def test_advertised_newest_has_no_five_minute_holdback(make_emitter, hybrid):
    hybrid.now = hybrid.latest+20
    e = make_emitter()
    e._do_radar()
    assert e._radar_result.ts_frame == hybrid.latest
    assert e._build_payload()['radar']['ageSec'] == 20


@pytest.mark.parametrize('behavior', ['hang', 'slow'])
@pytest.mark.parametrize('bad_count', [1, 3, 5])
def test_three_bad_tiles_hedged_on_warm_connections(make_emitter, monkeypatch, behavior, bad_count):
    """Deterministic batch: the first admission misses a subsequently warm lease.

    Script the attempt executor/clock only; run the real batch, race, admission,
    PNG validation/remap/cache. A healthy retry costs .1s. A primary would fail
    at 6s (hang) or 4s (slow), AFTER the 3s tile deadline. Thus waiting for primary
    failure cannot accidentally pass. All but the final bad tile get a lease at
    2s; that last tile is denied once and gets a returned warm lease at 2.05s.
    """
    import threading
    from concurrent.futures import Future
    from types import SimpleNamespace
    from lib import radar_fetch as fetch
    from tests.test_radar_hybrid import png
    local = threading.local()
    real_clock = time.monotonic
    raw = png()
    attempts, leases = [], []
    lock = threading.Lock()

    class Lease:
        def close(self): pass

    class ScriptedPool:
        def __init__(self, **kwargs):
            local.now = real_clock()
            local.start = local.now
            local.denied = False
        def submit(self, fn, control, retry):
            f = Future()
            f.control, f.retry = control, retry
            # The fake wire records and admits now; completion is driven by wait.
            f.run = lambda: fn(control, retry)
            f.bad = False
            try:
                value = f.run()
                if value is None:
                    f.bad = True
                else:
                    f.set_result(value)
            except Exception as error:
                f.set_exception(error)
            return f
        def shutdown(self, wait): pass

    def fake_wait(pending, timeout, return_when):
        done = {f for f in pending if f.done()}
        if not done:
            assert timeout > 0, 'busy polling'
            local.now += timeout
            for f in pending:
                if local.now-local.start >= (6 if behavior == 'hang' else 4):
                    f.set_exception(TimeoutError('bad primary'))
            done = {f for f in pending if f.done()}
        return done, set(pending)-done

    e = make_emitter()
    def request(source, url, deadline, attempt, retry=False, **kwargs):
        x = int(url.rsplit('/', 1)[-1])
        with lock:
            attempts.append((x, retry, attempt.hedged))
        if not retry and x < bad_count:
            if behavior == 'slow': attempt.progress()
            return None  # remains pending until its scripted first-attempt timeout
        if retry:
            local.now += .1
            assert local.now < deadline
            assert attempt.hedged and attempt.warm_lease is not None
            attempt.issued = True
            e._radar_health.issue_hedge()
        return raw
    def reserve(url):
        if int(url.rsplit('/',1)[-1]) == bad_count-1:
            if not local.denied:
                local.denied = True
                return None
            assert local.now-local.start >= 2.05-1e-6
        lease = Lease()
        with lock: leases.append(lease)
        return lease
    monkeypatch.setattr(fetch, 'time', SimpleNamespace(monotonic=lambda: getattr(local, 'now', real_clock())))
    monkeypatch.setattr(fetch, 'ThreadPoolExecutor', ScriptedPool)
    monkeypatch.setattr(fetch, 'wait', fake_wait)
    monkeypatch.setattr(e, '_radar_request', request)
    e._radar_session = SimpleNamespace(reserve_hedge=reserve)
    ctx = dict(zoom=8, tiles=[(i,1,0,0) for i in range(10)], tile_workers=6)
    end = real_clock()+3
    result = list(e._radar_tile_batch('iem-mrms-lcref', 1, ctx, end,
                  lambda x,y: 'https://fixture.invalid/tile/'+str(x), None))
    assert len(result) == 10
    assert len(attempts) == 10+bad_count and len(leases) == bad_count
    assert e._radar_health.hedges == bad_count
    assert e._radar_health.retries == e._radar_health.discarded == 0
    assert ctx['hedge_budget']['count'] == bad_count
    print(f'{behavior}: 10/10 tiles; {bad_count} healthy second attempts; denied at 2s, warm at 2.05s, complete by 2.15s < 3s')
