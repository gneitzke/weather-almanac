"""A Level III host outage must not strand Site when IEM still answers."""
import socket

import pytest

from lib import almanac_emit as ae
from lib import radar_engine
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_level3 import native  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401


@pytest.fixture
def warnings(monkeypatch):
    messages = []
    monkeypatch.setattr(ae.Logger, 'warning', messages.append)
    return messages


@pytest.fixture
def level3_dns_down(native, monkeypatch):
    opened = radar_engine.RadarSession.open
    calls = []

    def fetch(self, req, timeout):
        if req.full_url.startswith(radar_engine.RADAR_LEVEL3_BUCKET):
            calls.append(req.full_url)
            raise socket.gaierror(-3, 'Temporary failure in name resolution')
        return opened(self, req, timeout)
    monkeypatch.setattr(radar_engine.RadarSession, 'open', fetch)
    return calls


def test_a_level3_only_outage_draws_the_site_radar_from_v1(make_emitter, hybrid, multisite, level3_dns_down):
    hybrid.view()
    emitter = make_emitter()
    for _ in range(3):
        emitter.radar._acquire()
    r = emitter._build_payload()['radar']
    assert level3_dns_down, 'the pass did try Level III'
    assert r['available'] and r['sourceMode'] == 'site'
    assert r['native'] is False and r['tiles']['variant'] is False     # v1: IEM's site tiles
    assert [c for c in multisite.calls if c[0] == 'tile'], 'v1 fetched IEM ridge tiles'
    fallback = emitter.radar._health_payload()['nativeFallback']  # radar-health.json
    assert fallback['active'] is True and 'name resolution' in fallback['reason']
    assert r['nativeFallback'] == dict(active=True, reason='level3-unreachable', recovering=False)


def test_a_level3_failure_is_never_a_local_network_outage(make_emitter, hybrid, multisite, level3_dns_down):
    hybrid.view()
    emitter = make_emitter()
    emitter.radar._acquire()
    assert emitter.radar._local_failure_streak == 0
    assert emitter.radar._local_backoff() < radar_engine.RADAR_LOCAL_RETRY_MAX_SEC


def test_v2_comes_back_once_the_fallback_window_passes(make_emitter, hybrid, multisite, native, monkeypatch):
    hybrid.view()
    emitter = make_emitter()
    emitter.radar._level3_fallback(ValueError('boom'))
    assert emitter.radar._level3_down()
    clock = [ae.time.monotonic() + radar_engine.RADAR_LEVEL3_FALLBACK_SEC + 1]
    monkeypatch.setattr(ae.time, 'monotonic', lambda: clock[0])
    assert not emitter.radar._level3_down()
    emitter.radar._acquire()
    assert emitter._build_payload()['radar']['native'] is True


def test_fallback_expiry_wakes_the_worker_once(make_emitter, hybrid, monkeypatch):
    emitter = make_emitter()
    emitter.radar._level3_fallback(ValueError('boom'))
    emitter._runtime.running = True
    emitter.radar._zoom_stamp = emitter.radar._preference_stamp()
    spawned = []
    monkeypatch.setattr(emitter.radar, '_spawn', lambda name, work: spawned.append(name))
    clock = [ae.time.monotonic()]
    monkeypatch.setattr(ae.time, 'monotonic', lambda: clock[0])
    emitter.radar._check_zoom()
    assert spawned == []
    clock[0] += radar_engine.RADAR_LEVEL3_FALLBACK_SEC + 1
    emitter.radar._check_zoom()
    assert spawned == ['radar']
    emitter.radar._restart = False  # the spawned pass consumes this flag
    emitter.radar._check_zoom()
    assert spawned == ['radar']


def test_an_open_level3_breaker_draws_v1(make_emitter, hybrid, multisite, native, monkeypatch):
    hybrid.view()
    emitter = make_emitter()
    monkeypatch.setattr(emitter.radar._health, 'probe_delay',
                        lambda sources=None: 25.0 if sources and radar_engine.RADAR_LEVEL3_TRANSPORT in sources else None)
    assert emitter.radar._level3_down()
    emitter.radar._acquire()
    r = emitter._build_payload()['radar']
    assert r['native'] is False and emitter.radar._health_payload()['nativeFallback']['active'] is True


def test_a_level3_failure_pass_keeps_the_fallback_chain_and_retries_soon(make_emitter, hybrid):
    emitter = make_emitter()
    emitter.radar._transport_failures['iem-nexrad-n0b'] = 2
    retries = []
    emitter.radar._budget_retry = lambda source, n, min_delay=0, reason=None: retries.append((min_delay, reason))
    assert emitter.radar._failed_pass('iem-nexrad-n0b', ValueError('no complete site scan: gaierror'),
                                      dict(level3_failed=True)) is False
    assert emitter.radar._transport_failures['iem-nexrad-n0b'] == 2     # untouched, never reset
    assert emitter.radar._local_failure_streak == 0 and retries == [(2, 'provider')]


def test_a_failed_level3_recovery_probe_also_falls_back_without_local_backoff(
        make_emitter, hybrid, multisite, level3_dns_down):
    hybrid.view()
    emitter = make_emitter()
    url = radar_engine.RADAR_LEVEL3_BUCKET + '?list-type=2&prefix=NEA_N0B_2026_09_13_00'
    emitter.radar._health.admit(radar_engine.RADAR_LEVEL3_TRANSPORT, url, metadata=True)
    for _ in range(6):
        emitter.radar._health.record(radar_engine.RADAR_LEVEL3_TRANSPORT, url, False,
                                     ConnectionResetError('reset'))
    assert emitter.radar._health.probe_delay({radar_engine.RADAR_LEVEL3_TRANSPORT}) > 0
    hybrid.mono += 31  # breaker now admits its recovery probe before acquisition
    emitter.radar._acquire()
    assert level3_dns_down
    assert emitter.radar._local_failure_streak == 0
    assert emitter.radar._level3_down()
    emitter.radar._acquire()
    payload = emitter._build_payload()['radar']
    assert payload['available'] and payload['sourceMode'] == 'site'
    assert payload['native'] is False


# Classification QC and per-site Level III diagnostics.
def test_a_failed_n0h_fetch_keeps_its_retry_and_is_not_a_qc_failure(
        make_emitter, hybrid, multisite, native):
    # The native fixture serves N0B but has no N0H: these are ordinary missing
    # classifications, not QC errors that should suppress upgrades for 240 s.
    hybrid.view()
    hybrid.now = hybrid.latest + 60
    emitter = make_emitter()
    emitter.radar._acquire()
    payload = emitter._build_payload()['radar']
    assert payload['available'] and payload['native']
    assert emitter.radar._health_payload()['classification']['qcFailures'] == 0
    failures = [v for k, v in emitter.radar._level3_failed.items() if len(k) == 3]
    assert failures
    assert all(v[0] - ae.time.monotonic() <= 20 for v in failures)
    assert all('classification QC failed' not in v[1] for v in failures)


def test_a_failing_classification_qc_is_logged_once_counted_and_not_rerun(
        make_emitter, hybrid, multisite, native, monkeypatch, warnings):
    import lib.radar_mosaic as mosaic
    hybrid.view()
    emitter = make_emitter()
    real = emitter.radar._level3_scan
    def scan(site, stamp, ctx, deadline, product='N0B', volume_ts=None):
        if product == 'N0H':
            return object()                                   # a classification that arrived
        return real(site, stamp, ctx, deadline, product, volume_ts)
    monkeypatch.setattr(emitter.radar, '_level3_scan', scan)
    runs = []
    def broken(scan, classification):
        runs.append(scan.site if hasattr(scan, 'site') else 1)
        raise ValueError('N0H volume differs from N0B')
    monkeypatch.setattr(mosaic, 'quality_control', broken)
    hybrid.now = hybrid.latest + 60                            # the newest volume is inside the upgrade window
    emitter.radar._acquire()
    first = len(runs)
    assert first >= 1
    emitter.radar._acquire()
    assert len(runs) == first, 'the same failing QC ran again'
    health = emitter.radar._health_payload()['classification']
    assert health['qcFailures'] == first and 'volume differs' in health['lastQcError']['error']
    logged = [message for message in warnings if 'QC failed' in message]
    assert len(logged) == len({message.split()[2] for message in logged})   # once per site


def test_a_per_site_level3_failure_is_counted_and_logged_at_a_bounded_rate(
        make_emitter, hybrid, multisite, native, monkeypatch, warnings):
    opened = radar_engine.RadarSession.open
    def fetch(self, req, timeout):
        if req.full_url.startswith(radar_engine.RADAR_LEVEL3_BUCKET) and 'MID' in req.full_url:
            raise socket.gaierror(-3, 'Temporary failure in name resolution')
        return opened(self, req, timeout)
    monkeypatch.setattr(radar_engine.RadarSession, 'open', fetch)
    hybrid.view()
    emitter = make_emitter()
    emitter.radar._acquire()
    payload = emitter._build_payload()['radar']
    assert payload['available'] and payload['native']
    mosaic_health = emitter.radar._health_payload()['mosaic']
    count = mosaic_health['siteFailures']['KMID']['count']
    assert count >= 1
    assert 'KNEA' not in mosaic_health['siteFailures']
    emitter.radar._level3_site_failures([('KMID', socket.gaierror(-3, 'dns'))])
    assert emitter.radar._health_payload()['mosaic']['siteFailures']['KMID']['count'] == count + 1
    assert len([message for message in warnings if 'KMID Level III scan unavailable' in message]) == 1


@pytest.mark.parametrize('failed_source,url,streak', [
    (radar_engine.RADAR_LEVEL3_TRANSPORT, radar_engine.RADAR_LEVEL3_BUCKET, 0),
    ('iem-nexrad-n0b', radar_engine.RADAR_SITE_LIST_URL, 1),
])
def test_local_backoff_counts_only_the_failed_sources_hosts(
        make_emitter, hybrid, failed_source, url, streak):
    emitter = make_emitter()
    source = 'iem-nexrad-n0b'
    emitter.radar._health.record(failed_source, url, False, socket.gaierror(-3, 'resolver unavailable'))
    emitter.radar._failed_pass(source, TimeoutError('visible newest incomplete'),
                               dict(local_failure_start=0, ambiguous_failure_start=0))
    assert emitter.radar._local_failure_streak == streak
    assert emitter.radar._transport_failures.get(source, 0) == (0 if streak else 1)


def test_qc_logs_each_reason_once_and_removes_cached_classification(make_emitter, hybrid, warnings):
    emitter = make_emitter()
    volume = hybrid.now
    key = ('KNEA', volume, 'N0H')
    emitter.radar._level3_scans[key] = object()
    for reason in ('volume mismatch', 'volume mismatch', 'invalid geometry'):
        emitter.radar._qc_failed('KNEA', volume, ValueError(reason))
    frame = dict(siteScans=[dict(id='KNEA', ts=volume, volumeTs=volume, filtered=False)])
    assert not emitter.radar._hca_due(frame)
    assert key not in emitter.radar._level3_scans
    assert emitter.radar._health_payload()['classification']['qcFailures'] == 3
    assert len([message for message in warnings if 'QC failed' in message]) == 2


def test_site_failure_log_reports_suppressed_count_after_the_interval(make_emitter, hybrid, warnings):
    emitter = make_emitter()
    failed = [('KNEA', TimeoutError('scan unavailable'))]
    emitter.radar._level3_site_failures(failed)
    emitter.radar._level3_site_failures(failed)
    hybrid.mono += radar_engine.RADAR_FAILURE_LOG_SEC
    emitter.radar._level3_site_failures(failed)
    entry = emitter.radar._health_payload()['mosaic']['siteFailures']['KNEA']
    assert entry['count'] == 3 and entry['suppressed'] == 0
    assert len(warnings) == 2 and '1 not logged' in warnings[-1]


def test_quiet_region_failure_still_backs_off_without_a_nearby_site(make_emitter, hybrid, monkeypatch):
    from tests.test_radar_attention_engine import tier
    monkeypatch.setattr(radar_engine, 'RADAR_ATTENTION_MODE', 'active')
    monkeypatch.setattr(radar_engine, '_radar_nexrad', lambda *args: None)
    emitter = make_emitter()
    tier(emitter, 'rest')
    def fail(req, timeout):
        raise socket.gaierror(-3, 'resolver unavailable')
    hybrid.failure = fail
    emitter.radar._acquire()
    assert hybrid.calls
    assert emitter.radar._local_failure_streak == 1
    assert emitter.radar._local_backoff() == 2
