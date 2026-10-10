"""Retry admission and slow boot timing, with deterministic offline providers."""
import socket

import pytest

from lib import almanac_emit as ae
from lib import radar_engine
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_local_backoff import armed_delay


def test_discovery_cannot_overtake_local_retry_floor(make_emitter, hybrid):
    e = make_emitter()
    e._runtime.running = True
    e.radar._local_failure_streak = 6
    e.radar._discovery.due = ae.time.time()+1
    e.radar._budget_retry('iem-mrms-lcref', 1)
    e.radar._arm_discovery()
    assert armed_delay(e) == radar_engine.RADAR_LOCAL_RETRY_MAX_SEC
    assert e.radar._discovery.due >= e.radar._next_retry


def test_user_intent_gets_an_immediate_attempt_during_backoff(make_emitter, hybrid, monkeypatch):
    e = make_emitter()
    e._runtime.running = True
    e.radar._local_failure_streak = 6
    e.radar._budget_retry('iem-mrms-lcref', 1)
    spawned = []
    monkeypatch.setattr(e.radar, '_spawn', lambda lane, worker: spawned.append((lane, worker)))
    e.radar._zoom_stamp = None
    e.radar._check_zoom()
    assert len(spawned) == 1 and spawned[0][0] == 'radar'
    calls = []
    monkeypatch.setattr(e.radar, '_acquire', lambda **kw: calls.append(kw))
    spawned[0][1]()
    assert calls[0]['intent_triggered'] is True


def test_inventory_wait_does_not_consume_provider_deadline(make_emitter, hybrid, monkeypatch):
    e = make_emitter()
    e.radar._start_inventory()
    assert e.radar._cache_ready.wait(5)
    class SlowInventory:
        def wait(self, timeout):
            hybrid.mono += 26  # legal 30-second wait exceeds the 25-second pass
            return True
        def is_set(self):
            return True
    e.radar._cache_ready = SlowInventory()
    e.radar._acquire(intent_triggered=False)
    assert e.radar._result.ts_frame is not None, e.radar._pass


@pytest.mark.parametrize('code', [socket.EAI_NONAME, socket.EAI_FAIL, socket.EAI_AGAIN, -2, -3])
def test_every_dns_failure_is_local_whatever_the_errno(code):
    # A Pi with its network down reports EAI_NONAME (-2 on Linux) as readily as
    # EAI_AGAIN. Classifying any of them as a provider failure flapped the
    # fallback chain and tripled the pass log on Linux (test_radar_v62 Site).
    from lib import radar_http as http
    error = socket.gaierror(code, 'name resolution failed')
    assert http.failure_class(error) == 'local' and http.local_backoff_failure(error)


@pytest.mark.parametrize('scope', ['listing', 'tiles'])
def test_dns_failures_back_off_and_never_advance_fallback(make_emitter, hybrid, scope):
    e = make_emitter()
    e._runtime.running = True
    def fail(req, timeout):
        if 'iastate.edu' in req.full_url and (scope == 'listing' or '/mrms::lcref-' in req.full_url):
            raise socket.gaierror(socket.EAI_NONAME, 'Name or service not known')
    hybrid.failure = fail
    delays = []
    for _ in range(5):
        e.radar._acquire(intent_triggered=False)
        delays.append(armed_delay(e))
        hybrid.mono += 120
        hybrid.latest += 120
    assert delays == [2, 4, 8, 16, 32]
    assert e.radar._local_failure_streak == 5
    assert 'iem-mrms-lcref' not in e.radar._transport_failures   # no fallback from a resolver failure
    assert e.radar._result.source_id != 'rainviewer'
    hybrid.failure = None
    e.radar._acquire(intent_triggered=False)
    assert e.radar._result.ts_frame is not None and e.radar._local_failure_streak == 0


def test_resolver_timeout_is_local_and_backs_off(make_emitter, hybrid, monkeypatch):
    from lib import radar_http as http
    monkeypatch.setattr(http, '_shared_resolve', lambda *args: None)
    session = http.RadarSession()
    try:
        with pytest.raises(http.LocalTransportError) as raised:
            session._addresses_for(('unresolved.invalid', 443), 0)
        error = raised.value
        assert http.failure_class(error) == 'local'
        e = make_emitter()
        e._runtime.running = True
        delays = []
        for _ in range(4):
            e.radar._failed_pass('iem-mrms-lcref', error, {})
            delays.append(armed_delay(e))
        assert delays == [2, 4, 8, 16] and e.radar._local_failure_streak == 4
    finally:
        session.close()
