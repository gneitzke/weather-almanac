"""Compact pass accounting, failure episodes and preserved on-demand evidence."""
import io
import json
import socket
import urllib.error

import pytest

from lib import almanac_emit as ae
from tests.test_freshness_health import serve_at, _get  # noqa: F401
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tools.benchmark_radar_logging import simulate


@pytest.fixture
def logs(monkeypatch):
    info, warnings = [], []
    monkeypatch.setattr(ae.Logger, 'info', info.append)
    monkeypatch.setattr(ae.Logger, 'warning', warnings.append)
    return info, warnings


def test_saturated_history_compact_pass_and_health(make_emitter, logs, serve_at, monkeypatch):
    e = make_emitter()
    e._radar_request_metrics = [dict(at=i, failureClass='local', detail='x'*300) for i in range(128)]
    e._radar_phase_metrics = [dict(at=i, phase='newest', intent={'seq': i}) for i in range(128)]
    e._radar_pass.update(source='iem-nexrad-n0b', site='KATX', outcome='failed', error='\n雪\\"'*10000)
    e._radar_pass['counts'].update(dict(ok=200, local=10, host=10, http=10, ambiguous=5, cancelled=5))
    e._radar_health.hedges = 3
    # No health snapshot creation on the logging path, even at DEBUG log level.
    with monkeypatch.context() as m:
        m.setattr(e, '_radar_health_payload', lambda: pytest.fail('history serialized'))
        e._radar_log_pass(ae.time.monotonic())
    line = logs[0][-1]
    assert len((line+'\n').encode()) < 768
    assert 'requests=240 ok=200 failed=40' in line
    assert 'hedges=3 breaker=closed' in line
    assert 'source=iem-nexrad-n0b site=KATX' in line
    assert '\n' not in line and 'phases' not in line and 'detail' not in line
    payload = e._build_payload()
    e._radar_write_health(ae.time.time(), force=True)  # /health reads radar-health.json
    _, url = serve_at(payload)
    _, health = _get(url+'/health')
    assert health['radar']['requests'] == e._radar_request_metrics
    assert health['radar']['phases'] == e._radar_phase_metrics


def test_failure_reminders_changes_and_recovery(make_emitter, logs, monkeypatch):
    e = make_emitter()
    now = [1000.]
    monkeypatch.setattr(ae.time, 'monotonic', lambda: now[0])
    source = 'iem-mrms-lcref'
    error = socket.gaierror('offline')
    def fail(error=error, source=source):
        e._radar_begin_log_pass()
        e._radar_log_failure(source, error)
        e._radar_log_pass(now[0])
    fail()
    for _ in range(3):
        now[0] += 2
        fail()
    assert len(logs[1]) == 1
    now[0] = 1000+ae.RADAR_FAILURE_LOG_SEC
    fail()
    assert len(logs[1]) == 2 and 'suppressed=3' in logs[1][-1]
    fail(socket.gaierror('different'))
    fail(ValueError('different'))
    fail(source='rainviewer')
    assert len(logs[1]) == 5  # message, error class and source changes log immediately
    # A successful fallback and a deferred pass do not recover the primary.
    e._radar_begin_log_pass()
    e._radar_pass['recovered'].add(('rainviewer', 'pass'))
    e._radar_log_pass(now[0])
    assert any('rainviewer recovered' in s for s in logs[0])
    e._radar_begin_log_pass()
    e._radar_budget_retry(source, 1)
    e._radar_log_pass(now[0])
    assert not any(source+' recovered' in s for s in logs[0])
    fail(ValueError('different'))
    e._radar_begin_log_pass()
    e._radar_pass['recovered'].add((source, 'pass'))
    e._radar_log_pass(now[0])
    e._radar_log_pass(now[0])
    recovery = [s for s in logs[0] if source+' recovered' in s]
    assert len(recovery) == 1 and 'suppressed=1' in recovery[0]
    fail(ValueError('different'))
    assert 'suppressed=0' in logs[1][-1]


def test_real_pass_failure_then_recovery(make_emitter, hybrid, monkeypatch, logs):
    e = make_emitter()
    with monkeypatch.context() as m:
        def offline(*a, **kw):
            raise socket.gaierror('offline')
        m.setattr(ae.RadarSession, 'open', offline)
        e._do_radar()
        e._do_radar()
    assert len(logs[1]) == 1
    assert 'outcome=failed' in logs[0][-1]
    e._do_radar()
    e._do_radar()
    recovered = [line for line in logs[0] if ' recovered;' in line]
    assert len(recovered) == 1 and 'suppressed=1' in recovered[0]
    assert 'error=None' in logs[0][-1]  # /health lastError remains historical
    assert e._radar_health_payload()['lastError'] == 'offline'


def test_request_counts_survive_history_eviction_and_inner_retry(make_emitter, monkeypatch, logs):
    e = make_emitter()
    e._radar_session = ae.RadarSession()
    e._radar_request_gate = lambda *a: None
    mode = ['ok']
    def transport(session, req, **kw):
        if mode[0] == 'retry':
            req.radar_retry_failure(socket.gaierror('offline'))
        if mode[0] == 'denied':
            req.radar_retry_failure(socket.gaierror('offline'))
            req.radar_gate_failed = True
            raise ae._RadarBudget('no capacity')
        if mode[0] == '304':
            raise urllib.error.HTTPError(req.full_url, 304, 'unchanged', {}, None)
        if mode[0] == '404':
            raise urllib.error.HTTPError(req.full_url, 404, 'missing', {}, None)
        return io.BytesIO(b'{}')
    monkeypatch.setattr(ae.RadarSession, 'open', transport)
    url = ae.RADAR_IEM_METADATA_URL
    for _ in range(140):
        e._radar_request('iem-mrms-lcref', url, ae.time.monotonic()+10, metadata=True)
    for name in ('retry', '304', '404', 'denied'):
        mode[0] = name
        try:
            e._radar_request('iem-mrms-lcref', url, ae.time.monotonic()+10, metadata=True)
        except (urllib.error.HTTPError, ae._RadarBudget):
            pass
    e._radar_log_pass(ae.time.monotonic())
    assert len(e._radar_request_metrics) == 128
    assert 'requests=145 ok=142 failed=3 classes={"http":1,"local":2}' in logs[0][-1]
    e._radar_begin_log_pass()
    e._radar_log_pass(ae.time.monotonic())
    assert 'requests=0 ok=0 failed=0' in logs[0][-1]


@pytest.mark.parametrize('mode', ['Region', 'Site'])
def test_simulated_outage_hour(mode):
    stats = simulate(ae, mode)
    assert stats['passLines'] == 1800
    assert stats['requestHistory'] == stats['phaseHistory'] == 128
    assert stats['maxPassBytes'] < 768
    assert stats['bytes'] < 1_000_000
    assert stats['lines'] < 1850


def test_listing_success_does_not_recover_failed_tiles(make_emitter, logs, monkeypatch):
    e = make_emitter()
    source = 'iem-nexrad-n0b'
    e._radar_log_failure(source, TimeoutError('tiles unavailable'))
    e._radar_log_pass(ae.time.monotonic())
    e._radar_begin_log_pass()
    e._radar_session = ae.RadarSession()
    monkeypatch.setattr(ae.RadarSession, 'open', lambda *a, **kw: io.BytesIO(b'{"scans":[]}'))
    e._radar_site_listing(dict(deadline=ae.time.monotonic()+10), dict(id='KATX'))
    e._radar_log_pass(ae.time.monotonic())
    assert not any(' recovered;' in line for line in logs[0])
    e._radar_begin_log_pass()
    e._radar_log_failure(source, socket.gaierror('listing offline'), scope='KATX')
    e._radar_log_pass(ae.time.monotonic())
    e._radar_begin_log_pass()
    e._radar_site_listing(dict(deadline=ae.time.monotonic()+10), dict(id='KATX'))
    e._radar_log_pass(ae.time.monotonic())
    assert len([line for line in logs[0] if ' recovered;' in line]) == 1
