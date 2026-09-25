"""A Level III host outage must not strand Site when IEM still answers."""
import socket

import pytest

from lib import almanac_emit as ae
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
    opened = ae.RadarSession.open
    calls = []

    def fetch(self, req, timeout):
        if req.full_url.startswith(ae.RADAR_LEVEL3_BUCKET):
            calls.append(req.full_url)
            raise socket.gaierror(-3, 'Temporary failure in name resolution')
        return opened(self, req, timeout)
    monkeypatch.setattr(ae.RadarSession, 'open', fetch)
    return calls


def test_a_level3_only_outage_draws_the_site_radar_from_v1(make_emitter, hybrid, multisite, level3_dns_down):
    hybrid.view()
    emitter = make_emitter()
    for _ in range(3):
        emitter._do_radar()
    r = emitter._build_payload()['radar']
    assert level3_dns_down, 'the pass did try Level III'
    assert r['available'] and r['sourceMode'] == 'site'
    assert r['native'] is False and r['tiles']['variant'] is False     # v1: IEM's site tiles
    assert [c for c in multisite.calls if c[0] == 'tile'], 'v1 fetched IEM ridge tiles'
    fallback = r['health']['nativeFallback']
    assert fallback['active'] is True and 'name resolution' in fallback['reason']
    assert r['renderPref'] == 'v2'                                      # the choice itself is unchanged


def test_a_level3_failure_is_never_a_local_network_outage(make_emitter, hybrid, multisite, level3_dns_down):
    hybrid.view()
    emitter = make_emitter()
    emitter._do_radar()
    assert emitter._radar_local_failure_streak == 0
    assert emitter._radar_local_backoff() < ae.RADAR_LOCAL_RETRY_MAX_SEC


def test_v2_comes_back_once_the_fallback_window_passes(make_emitter, hybrid, multisite, native, monkeypatch):
    hybrid.view()
    emitter = make_emitter()
    emitter._radar_level3_fallback(ValueError('boom'))
    assert emitter._radar_level3_down()
    clock = [ae.time.monotonic() + ae.RADAR_LEVEL3_FALLBACK_SEC + 1]
    monkeypatch.setattr(ae.time, 'monotonic', lambda: clock[0])
    assert not emitter._radar_level3_down()
    emitter._do_radar()
    assert emitter._build_payload()['radar']['native'] is True


def test_fallback_expiry_wakes_the_worker_once(make_emitter, hybrid, monkeypatch):
    emitter = make_emitter()
    emitter._radar_level3_fallback(ValueError('boom'))
    emitter._running = True
    emitter._radar_zoom_stamp = emitter._radar_preference_stamp()
    spawned = []
    monkeypatch.setattr(emitter, '_spawn', lambda name, work: spawned.append(name))
    clock = [ae.time.monotonic()]
    monkeypatch.setattr(ae.time, 'monotonic', lambda: clock[0])
    emitter._check_radar_zoom()
    assert spawned == []
    clock[0] += ae.RADAR_LEVEL3_FALLBACK_SEC + 1
    emitter._check_radar_zoom()
    assert spawned == ['radar']
    emitter._radar_restart = False  # the spawned pass consumes this flag
    emitter._check_radar_zoom()
    assert spawned == ['radar']


def test_an_open_level3_breaker_draws_v1(make_emitter, hybrid, multisite, native, monkeypatch):
    hybrid.view()
    emitter = make_emitter()
    monkeypatch.setattr(emitter._radar_health, 'probe_delay',
                        lambda sources=None: 25.0 if sources and ae.RADAR_LEVEL3_TRANSPORT in sources else None)
    assert emitter._radar_level3_down()
    emitter._do_radar()
    r = emitter._build_payload()['radar']
    assert r['native'] is False and r['health']['nativeFallback']['active'] is True


def test_a_level3_failure_pass_keeps_the_fallback_chain_and_retries_soon(make_emitter, hybrid):
    emitter = make_emitter()
    emitter._radar_transport_failures['iem-nexrad-n0b'] = 2
    retries = []
    emitter._radar_budget_retry = lambda source, n, min_delay=0, reason=None: retries.append((min_delay, reason))
    assert emitter._radar_failed_pass('iem-nexrad-n0b', ValueError('no complete site scan: gaierror'),
                                      dict(level3_failed=True)) is False
    assert emitter._radar_transport_failures['iem-nexrad-n0b'] == 2     # untouched, never reset
    assert emitter._radar_local_failure_streak == 0 and retries == [(2, 'provider')]


def test_a_failed_level3_recovery_probe_also_falls_back_without_local_backoff(
        make_emitter, hybrid, multisite, level3_dns_down):
    hybrid.view()
    emitter = make_emitter()
    url = ae.RADAR_LEVEL3_BUCKET + '?list-type=2&prefix=NEA_N0B_2026_09_13_00'
    emitter._radar_health.admit(ae.RADAR_LEVEL3_TRANSPORT, url, metadata=True)
    for _ in range(6):
        emitter._radar_health.record(ae.RADAR_LEVEL3_TRANSPORT, url, False,
                                     ConnectionResetError('reset'))
    assert emitter._radar_health.probe_delay({ae.RADAR_LEVEL3_TRANSPORT}) > 0
    hybrid.mono += 31  # breaker now admits its recovery probe before acquisition
    emitter._do_radar()
    assert level3_dns_down
    assert emitter._radar_local_failure_streak == 0
    assert emitter._radar_level3_down()
    emitter._do_radar()
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
    emitter._do_radar()
    payload = emitter._build_payload()['radar']
    assert payload['available'] and payload['native']
    assert payload['health']['classification']['qcFailures'] == 0
    failures = [v for k, v in emitter._radar_level3_failed.items() if len(k) == 3]
    assert failures
    assert all(v[0] - ae.time.monotonic() <= 20 for v in failures)
    assert all('classification QC failed' not in v[1] for v in failures)


def test_a_failing_classification_qc_is_logged_once_counted_and_not_rerun(
        make_emitter, hybrid, multisite, native, monkeypatch, warnings):
    import lib.radar_mosaic as mosaic
    hybrid.view()
    emitter = make_emitter()
    real = emitter._radar_level3_scan
    def scan(site, stamp, ctx, deadline, product='N0B', volume_ts=None):
        if product == 'N0H':
            return object()                                   # a classification that arrived
        return real(site, stamp, ctx, deadline, product, volume_ts)
    monkeypatch.setattr(emitter, '_radar_level3_scan', scan)
    runs = []
    def broken(scan, classification):
        runs.append(scan.site if hasattr(scan, 'site') else 1)
        raise ValueError('N0H volume differs from N0B')
    monkeypatch.setattr(mosaic, 'quality_control', broken)
    hybrid.now = hybrid.latest + 60                            # the newest volume is inside the upgrade window
    emitter._do_radar()
    first = len(runs)
    assert first >= 1
    emitter._do_radar()
    assert len(runs) == first, 'the same failing QC ran again'
    health = emitter._build_payload()['radar']['health']['classification']
    assert health['qcFailures'] == first and 'volume differs' in health['lastQcError']['error']
    logged = [message for message in warnings if 'QC failed' in message]
    assert len(logged) == len({message.split()[2] for message in logged})   # once per site


def test_a_per_site_level3_failure_is_counted_and_logged_at_a_bounded_rate(
        make_emitter, hybrid, multisite, native, monkeypatch, warnings):
    opened = ae.RadarSession.open
    def fetch(self, req, timeout):
        if req.full_url.startswith(ae.RADAR_LEVEL3_BUCKET) and 'MID' in req.full_url:
            raise socket.gaierror(-3, 'Temporary failure in name resolution')
        return opened(self, req, timeout)
    monkeypatch.setattr(ae.RadarSession, 'open', fetch)
    hybrid.view()
    emitter = make_emitter()
    emitter._do_radar()
    payload = emitter._build_payload()['radar']
    assert payload['available'] and payload['native']
    mosaic_health = payload['health']['mosaic']
    count = mosaic_health['siteFailures']['KMID']['count']
    assert count >= 1
    assert 'KNEA' not in mosaic_health['siteFailures']
    emitter._radar_level3_site_failures([('KMID', socket.gaierror(-3, 'dns'))])
    assert emitter._radar_health_payload()['mosaic']['siteFailures']['KMID']['count'] == count + 1
    assert len([message for message in warnings if 'KMID Level III scan unavailable' in message]) == 1


@pytest.mark.parametrize('failed_source,url,streak', [
    (ae.RADAR_LEVEL3_TRANSPORT, ae.RADAR_LEVEL3_BUCKET, 0),
    ('iem-nexrad-n0b', ae.RADAR_SITE_LIST_URL, 1),
])
def test_local_backoff_counts_only_the_failed_sources_hosts(
        make_emitter, hybrid, failed_source, url, streak):
    emitter = make_emitter()
    source = 'iem-nexrad-n0b'
    emitter._radar_health.record(failed_source, url, False, socket.gaierror(-3, 'resolver unavailable'))
    emitter._radar_failed_pass(source, TimeoutError('visible newest incomplete'),
                               dict(local_failure_start=0, ambiguous_failure_start=0))
    assert emitter._radar_local_failure_streak == streak
    assert emitter._radar_transport_failures.get(source, 0) == (0 if streak else 1)


def test_qc_logs_each_reason_once_and_removes_cached_classification(make_emitter, hybrid, warnings):
    emitter = make_emitter()
    volume = hybrid.now
    key = ('KNEA', volume, 'N0H')
    emitter._radar_level3_scans[key] = object()
    for reason in ('volume mismatch', 'volume mismatch', 'invalid geometry'):
        emitter._radar_qc_failed('KNEA', volume, ValueError(reason))
    frame = dict(siteScans=[dict(id='KNEA', ts=volume, volumeTs=volume, filtered=False)])
    assert not emitter._radar_hca_due(frame)
    assert key not in emitter._radar_level3_scans
    assert emitter._radar_health_payload()['classification']['qcFailures'] == 3
    assert len([message for message in warnings if 'QC failed' in message]) == 2


def test_site_failure_log_reports_suppressed_count_after_the_interval(make_emitter, hybrid, warnings):
    emitter = make_emitter()
    failed = [('KNEA', TimeoutError('scan unavailable'))]
    emitter._radar_level3_site_failures(failed)
    emitter._radar_level3_site_failures(failed)
    hybrid.mono += ae.RADAR_FAILURE_LOG_SEC
    emitter._radar_level3_site_failures(failed)
    entry = emitter._radar_health_payload()['mosaic']['siteFailures']['KNEA']
    assert entry['count'] == 3 and entry['suppressed'] == 0
    assert len(warnings) == 2 and '1 not logged' in warnings[-1]


def test_quiet_region_failure_still_backs_off_without_a_nearby_site(make_emitter, hybrid, monkeypatch):
    from tests.test_radar_attention_engine import tier
    monkeypatch.setattr(ae, 'RADAR_ATTENTION_MODE', 'active')
    monkeypatch.setattr(ae, '_radar_nexrad', lambda *args: None)
    emitter = make_emitter()
    tier(emitter, 'rest')
    def fail(req, timeout):
        raise socket.gaierror(-3, 'resolver unavailable')
    hybrid.failure = fail
    emitter._do_radar()
    assert hybrid.calls
    assert emitter._radar_local_failure_streak == 1
    assert emitter._radar_local_backoff() == 2
