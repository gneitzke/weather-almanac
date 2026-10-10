"""A local failure (no route, no resolver) never opens a host breaker, so its
retry backs off on its own instead of rerunning a doomed pass every 2 s. Every
assertion here reads the timer that was actually armed (_radar_next_retry), not
the argument some caller passed to a mocked scheduler."""
import pytest
from lib import almanac_emit as ae, radar_http as http
from lib import radar_engine
from tests.test_radar_hybrid import hybrid  # noqa: F401

SRC = 'iem-mrms-lcref'
CAP = radar_engine.RADAR_LOCAL_RETRY_MAX_SEC


def armed_delay(e):
    assert 'radar' in e._runtime.retries, 'no radar retry armed'
    return round(e.radar._next_retry - ae.time.time(), 3)


def failed_passes(e, errors, ctx=None):
    e._runtime.running = True
    e.radar._retained_refresh = lambda *a: None
    delays = []
    for error in errors:
        assert not e.radar._failed_pass(SRC, error, dict(ctx or {}))
        delays.append(armed_delay(e))
    return delays


def test_consecutive_local_failures_back_off_to_the_cap(make_emitter, hybrid):
    e = make_emitter()
    assert failed_passes(e, [http.LocalTransportError('no route')]*8) == [2, 4, 8, 16, 32, CAP, CAP, CAP]
    assert SRC not in e.radar._transport_failures  # still never advances fallback


def test_a_provider_failure_ends_the_local_backoff(make_emitter, hybrid):
    e = make_emitter()
    local = http.LocalTransportError('no route')
    assert failed_passes(e, [local]*4+[OSError('host')]+[local]) == [2, 4, 8, 16, 2, 2]


def test_ambiguous_stalls_end_the_local_backoff_and_never_advance_fallback(make_emitter, hybrid):
    # A reused socket that got no bytes may be the provider stalling: it must
    # not wait out a local-outage backoff, and it must not count as a provider
    # failure either.
    e = make_emitter()
    local = http.LocalTransportError('no route')
    stall = http.AmbiguousTransportError('reused socket, no first byte')
    assert failed_passes(e, [local]*3+[stall]*3+[local]) == [2, 4, 8, 2, 2, 2, 2]
    assert SRC not in e.radar._transport_failures


def test_a_synthetic_pass_error_is_classified_by_what_failed_underneath(make_emitter, hybrid):
    # The frame path reports "visible newest incomplete" as a TimeoutError; the
    # tile loop's per-class ctx flags say what actually went wrong.
    e = make_emitter()
    incomplete = lambda: TimeoutError('visible newest incomplete')
    assert failed_passes(e, [incomplete(), incomplete()], ctx={'local_failure': True}) == [2, 4]
    assert e.radar._local_failure_streak == 2 and SRC not in e.radar._transport_failures
    assert failed_passes(e, [incomplete()], ctx={'ambiguous_failure': True}) == [2]
    assert e.radar._local_failure_streak == 0 and SRC not in e.radar._transport_failures
    assert failed_passes(e, [incomplete()]) == [2]
    assert e.radar._transport_failures[SRC] == 1  # a plain timeout IS a provider failure


def test_health_counters_classify_when_the_ctx_flags_are_missing(make_emitter, hybrid):
    e = make_emitter()
    for _ in range(3):
        e.radar._health.record(SRC, radar_engine.RADAR_IEM_METADATA_URL, False, http.LocalTransportError('no route'))
    assert failed_passes(e, [TimeoutError('x')]*2, ctx={'local_failure_start': 2}) == [2, 4]
    e.radar._health.record(SRC, radar_engine.RADAR_IEM_METADATA_URL, False, http.AmbiguousTransportError('no first byte'))
    assert failed_passes(e, [TimeoutError('x')], ctx={'local_failure_start': 3, 'ambiguous_failure_start': 0}) == [2]
    assert e.radar._local_failure_streak == 0 and SRC not in e.radar._transport_failures


def test_the_backoff_floors_every_radar_retry_not_just_the_failed_pass(make_emitter, hybrid):
    # A partial-frame pass calls _radar_failed_pass and then re-arms its own
    # budget retry; that retry must not land 2 s later over the backoff.
    e = make_emitter()
    failed_passes(e, [http.LocalTransportError('no route')]*3)
    e.radar._budget_retry(SRC, 5)
    assert armed_delay(e) == 8
    e.radar._local_failure_streak = 0
    e.radar._budget_retry(SRC, 5)
    assert armed_delay(e) == 2


def test_a_long_outage_stays_at_the_cap(make_emitter, hybrid):
    e = make_emitter()
    e.radar._local_failure_streak = 10000
    assert failed_passes(e, [http.LocalTransportError('no route')]) == [CAP]


@pytest.mark.parametrize('dead', ['host', 'tiles'])
def test_real_passes_back_off_on_a_dead_route_and_reset_when_it_returns(make_emitter, hybrid, dead):
    def no_route(req, _):
        url = req.full_url
        if 'iastate.edu' in url and (dead == 'host' or '/mrms::lcref-' in url):
            raise http.LocalTransportError('no route to host')
    e = make_emitter()
    e._runtime.running = True
    hybrid.failure = no_route
    delays = []
    for _ in range(4):
        e.radar._acquire(intent_triggered=False)
        delays.append(armed_delay(e))
        hybrid.mono += 120; hybrid.latest += 120
    assert delays == [2, 4, 8, 16], delays
    assert e.radar._local_failure_streak == 4
    assert SRC not in e.radar._transport_failures
    hybrid.failure = None
    e.radar._acquire(intent_triggered=False)
    assert e.radar._result.available and e.radar._result.source_id == SRC
    assert e.radar._local_failure_streak == 0 and e.radar._local_backoff() == 0
