"""Readiness wakeups use a deterministic clock, never the JSON emit tick."""
from datetime import datetime, timezone
from types import SimpleNamespace
import urllib.error

import pytest

from lib import almanac_emit as ae
from lib import radar_engine
from lib.radar_discovery import DiscoverySchedule
from lib.radar_fetch import HostHealth
from tests.test_emitter_lifecycle import FakeClock
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401


@pytest.fixture
def scheduled(make_emitter, hybrid, monkeypatch):
    clock = FakeClock()
    hybrid.now = hybrid.latest + 300
    monkeypatch.setattr(ae, 'Clock', clock)
    monkeypatch.setattr(ae.time, 'time', lambda: hybrid.now + clock.now)
    monkeypatch.setattr(ae.time, 'monotonic', lambda: clock.now)
    e = make_emitter()
    e._runtime.running = True
    monkeypatch.setattr(e.radar, '_spawn', lambda key, worker: worker())
    e.radar._acquire(intent_triggered=False)
    return e, clock, hybrid


def metadata_calls(h):
    return [c for c in h.calls if c[2] == radar_engine.RADAR_IEM_METADATA_URL]


def test_aligned_discovery_bounded_repoll_and_no_tiles(scheduled):
    e, clock, h = scheduled
    first = h.latest
    before = len(h.calls)
    assert e.radar._discovery.expected == first + 120 + 300
    clock.advance(119.99)
    assert len(h.calls) == before
    clock.advance(.01)
    assert len(h.calls) == before + 2  # unchanged: MRMS metadata + closest-site listing, no tiles
    assert e.radar._known('iem-mrms-lcref', dict(intent_triggered=True))['newest'] == first
    assert e.radar._discovery.last_poll == first + 420
    clock.advance(100)
    assert len(h.calls) == before + 12
    assert e.radar._discovery.polls == 6
    assert e.radar._discovery.due == first + 640
    clock.advance(119)
    assert len(h.calls) == before + 12
    e.stop()
    assert not clock.events


def test_new_stamp_slides_manifest_and_reanchors_age(scheduled):
    e, clock, h = scheduled
    old = e.radar._result
    clock.advance(120)  # provider late at predicted readiness
    h.latest += 120
    clock.advance(20)
    snap = e.radar._result
    assert snap.ts_frame == h.latest
    assert old.ts_frame in {f['ts'] for f in snap.frames}
    assert snap.frames[-1]['complete']
    assert e.radar._discovery.expected == h.latest + 420
    payload = e.radar._payload(snap, ae.time.time(), timezone.utc)
    assert payload['ageSec'] == 320 < 450
    health = e.radar._health_payload()['discovery']
    assert health['ageSec'] == 320 and health['fastPolls'] == 0
    assert health['nextPollTs'] == h.latest + 420


@pytest.mark.parametrize('spacing', [300, 600])
def test_site_expected_volume_and_thirty_second_polls(spacing):
    plan = DiscoverySchedule()
    snap = SimpleNamespace(ts_frame=3600, source_id='iem-nexrad-n0b', site_id='KATX',
                           source_mode='site', cadence=300,
                           frames=[dict(ts=t) for t in range(0, 3601, spacing)])
    plan.observe(snap, 3601, 300)
    assert plan.due == 3600 + spacing
    plan.started(plan.due)
    assert plan.due == 3630 + spacing
    plan.observe(snap, 3602, 300)
    assert plan.polls == 1  # rezooms/warming cannot restart the polling window


def test_breaker_and_budget_postpone_discovery(scheduled):
    e, clock, h = scheduled
    source, url = 'iem-mrms-lcref', radar_engine.RADAR_IEM_METADATA_URL
    for _ in range(240):
        e.radar._health.record(source, url, False, 'offline')
    assert e.radar._health.snapshot()['breaker'] == 'open'
    e.radar._arm_discovery()
    assert e.radar._discovery.due == ae.time.time() + 30
    before = len(h.calls)
    clock.advance(29)
    assert len(h.calls) == before
    e.radar._request_times = [clock.now] * 240
    e.radar._arm_discovery()
    assert e.radar._discovery.due == ae.time.time() + 60
    assert len([x for x in clock.events if x == e.radar._discovery_event]) == 1


def test_busy_lane_rearms_without_counting_a_poll(scheduled):
    e, clock, h = scheduled
    e._runtime.inflight.add('radar')
    before = len(h.calls)
    clock.advance(120)
    assert len(h.calls) == before and e.radar._discovery.polls == 0
    with pytest.raises(radar_engine._RadarBudget, match='readiness discovery'):
        e.radar._checkpoint(dict(request_reserve=radar_engine.RADAR_HISTORY_RESERVE))
    e._runtime.inflight.clear()
    clock.advance(5)
    assert len(h.calls) == before + 2
    assert not e.radar._discovery_pending


def test_discovery_can_use_last_slot_without_tile_headroom(scheduled):
    e, clock, h = scheduled
    clock.advance(119)
    e.radar._request_times = [clock.now] * 239
    before = len(h.calls)
    clock.advance(1)
    assert len(h.calls) == before + 1
    assert len(e.radar._request_times) == 240
    assert e.radar._discovery.due == h.now + 179


def test_retry_at_readiness_consumes_discovery_once(scheduled):
    e, clock, h = scheduled
    e.radar._schedule_retry('radar', e.radar._check, 120)
    e.radar._arm_discovery()  # place readiness after the coincident retry
    before = len(h.calls)
    clock.advance(120)
    assert len(h.calls) == before + 2
    assert e.radar._discovery.polls == 1
    assert e.radar._discovery.due == h.latest + 440


def test_unused_fallback_breaker_cannot_create_one_second_polls(scheduled):
    e, clock, h = scheduled
    for _ in range(6):
        e.radar._health.record('rainviewer', 'https://fallback.invalid/meta', False, 'offline')
    clock.advance(60)
    e.radar._arm_discovery()
    assert e.radar._discovery.due == h.latest + 420


def test_preferred_source_cooldown_wakes_fallback_for_recovery(scheduled):
    e, clock, h = scheduled
    e.radar._result = e.radar._result._replace(source_id='rainviewer', cadence=600)
    e.radar._cooldowns['iem-mrms-lcref'] = clock.now + 45
    e.radar._arm_discovery()
    assert e.radar._discovery.due == ae.time.time() + 45


def test_breaker_probe_recovers_without_shifting_scan_phase(scheduled):
    e, clock, h = scheduled
    e.radar._health = HostHealth()
    for _ in range(6):
        e.radar._health.admit('iem-mrms-lcref', radar_engine.RADAR_IEM_METADATA_URL, metadata=True)
        e.radar._health.record('iem-mrms-lcref', radar_engine.RADAR_IEM_METADATA_URL, False, 'offline')
    e.radar._arm_discovery()
    before = len(h.calls)
    clock.advance(30)
    assert len(h.calls) == before + 2
    assert e.radar._health.snapshot()['breaker'] == 'closed'
    assert e.radar._discovery.due == h.latest + 420
    clock.advance(89)
    assert len(h.calls) == before + 2


def test_advertised_scan_is_not_delayed_by_prediction(make_emitter, hybrid):
    hybrid.now = hybrid.latest + 10
    e = make_emitter()
    e.radar._acquire()
    assert e.radar._result.ts_frame == hybrid.latest


def test_unchanged_rainviewer_preserves_validated_manifest(make_emitter, hybrid, monkeypatch):
    monkeypatch.setattr(radar_engine, '_radar_iem_eligible', lambda *args: False)
    e = make_emitter()
    e.radar._acquire(intent_triggered=False)
    before = len(hybrid.calls)
    e.radar._acquire(intent_triggered=False, discovery=True)
    # Outside MRMS the closest site is still in range here, and its listing is
    # checked on discovery; the RainViewer manifest itself costs one request.
    assert len([c for c in hybrid.calls[before:] if c[0] == 'rainviewer']) == 1
    known = e.radar._known('rainviewer', dict(intent_triggered=True))
    assert known['newest'] == hybrid.rv and known['host'] == 'https://tiles.example'
    assert known['past'][hybrid.rv] == '/v2/' + str(hybrid.rv)


def test_early_metadata_archive_miss_does_not_mask_readiness(make_emitter, hybrid, monkeypatch):
    clock = FakeClock()
    actual = hybrid.latest
    hybrid.latest += 240  # metadata is ahead of the rendered archive
    monkeypatch.setattr(ae, 'Clock', clock)
    monkeypatch.setattr(ae.time, 'time', lambda: hybrid.now + clock.now)
    monkeypatch.setattr(ae.time, 'monotonic', lambda: clock.now)
    def fail(req, timeout):
        if req.get_method() == 'HEAD':
            stamp = datetime.strptime(req.full_url[-16:-4], '%Y%m%d%H%M').replace(tzinfo=timezone.utc).timestamp()
            if ae.time.time() < stamp + 300:
                raise urllib.error.HTTPError(req.full_url, 404, 'not ready', {}, None)
    hybrid.failure = fail
    e = make_emitter()
    e._runtime.running = True
    monkeypatch.setattr(e.radar, '_spawn', lambda key, worker: worker())
    e.radar._acquire(intent_triggered=False)
    assert e.radar._result.ts_frame == actual
    clock.advance(60)
    assert e.radar._result.ts_frame == actual + 120
    assert e.radar._health_payload()['discovery']['ageSec'] == 300


@pytest.mark.parametrize('spacing', [300, 600])
def test_site_listing_wakeup_and_slide(make_emitter, hybrid, multisite, monkeypatch, spacing):
    clock = FakeClock()
    hybrid.now = hybrid.latest + 10
    multisite.scans['KNEA'] = [hybrid.latest - i*spacing for i in range(4, -1, -1)]
    multisite.scans['KMID'] = []
    monkeypatch.setattr(ae, 'Clock', clock)
    monkeypatch.setattr(ae.time, 'time', lambda: hybrid.now + clock.now)
    monkeypatch.setattr(ae.time, 'monotonic', lambda: clock.now)
    e = make_emitter()
    e._runtime.running = True
    monkeypatch.setattr(e.radar, '_spawn', lambda key, worker: worker())
    e.radar._acquire(intent_triggered=False)
    old = e.radar._result
    multisite.calls.clear()
    clock.advance(spacing-10)
    assert len(multisite.calls) == 3 and all(c[0] == 'list' for c in multisite.calls)
    multisite.scans['KNEA'].append(hybrid.latest + spacing)
    clock.advance(30)
    assert e.radar._result.ts_frame == hybrid.latest + spacing
    assert old.ts_frame in {f['ts'] for f in e.radar._result.frames}
    assert e.radar._discovery.expected == hybrid.latest + spacing*2
