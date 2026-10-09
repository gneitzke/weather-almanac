"""Adversarial regressions, entirely offline (handlers are called without sockets)."""
import json

import pytest

from lib import almanac_emit as ae
from tests.test_pre_rain_fixes import poll
from tests.test_radar_attention_serve import server  # noqa: F401
from tests.test_radar_buffer_page import run_page
from tests.test_radar_hybrid import hybrid  # noqa: F401


def report(seq, view='radar', session='page-session-123456', claim=None):
    return (f'view={view}&viewSession={session}&viewSeq={seq}'
            + (f'&viewClaim={claim}' if claim is not None else ''))


def test_auxiliary_and_delayed_polls_do_not_change_current_view(server, monkeypatch, tmp_path):
    marker = tmp_path / 'radar_viewing'
    poll(server, monkeypatch, report(2))
    original = marker.read_text()
    poll(server, monkeypatch, 'r=1')  # diagnostics/ack without a view report
    assert marker.read_text() == original
    poll(server, monkeypatch, report(1, 'none'))  # aborted request reached server late
    assert marker.read_text() == original
    poll(server, monkeypatch, report(3, 'none'))
    assert not marker.exists()
    poll(server, monkeypatch, report(2))
    assert not marker.exists()


def test_reloaded_page_fences_old_session_without_owning_camera(server, monkeypatch, tmp_path):
    poll(server, monkeypatch, report(7))
    server._radar_owner = dict(session='camera-owner-123456', generation=9)
    poll(server, monkeypatch, report(1, 'none', 'new-page-session-1234', 'page-session-123456'))
    assert not (tmp_path / 'radar_viewing').exists()
    poll(server, monkeypatch, report(8))
    assert not (tmp_path / 'radar_viewing').exists()
    poll(server, monkeypatch, report(2, session='new-page-session-1234'))
    assert (tmp_path / 'radar_viewing').exists()
    assert server._radar_owner == dict(session='camera-owner-123456', generation=9)


def test_rejected_reports_cannot_extend_since(server, monkeypatch, tmp_path):
    now = [1000.]
    monkeypatch.setattr(server.time, 'time', lambda: now[0])
    poll(server, monkeypatch, report(2))
    now[0] += 2
    poll(server, monkeypatch, report(1))
    assert json.loads((tmp_path / 'radar_viewing').read_text()) == dict(since=1000., last=1000.)
    now[0] += server.RADAR_VIEW_POLL_GAP_SEC
    poll(server, monkeypatch, report(3))
    assert json.loads((tmp_path / 'radar_viewing').read_text())['since'] == now[0]


@pytest.mark.parametrize('unattended', [True, False])
def test_prefetch_flip_does_not_supersede_foreground(make_emitter, monkeypatch, unattended):
    monkeypatch.setattr(ae, 'RADAR_ATTENTION_MODE', 'active')
    e = make_emitter()
    e._radar_attention.tier = 'live'
    e._radar_attention.unattended = not unattended
    before = e._radar_attention_knobs()
    e._radar_attention.unattended = unattended
    e._radar_checkpoint(dict(attention_knobs=before))
    arms = []
    monkeypatch.setattr(e, '_radar_arm_discovery', lambda **kw: arms.append(kw))
    e._radar_attention_changed(before, ae.time.time())
    assert not arms, 'prefetch-only flip must not rearm foreground discovery'


def test_prefetch_work_yields_when_unattended(make_emitter, monkeypatch):
    monkeypatch.setattr(ae, 'RADAR_ATTENTION_MODE', 'active')
    e = make_emitter(); e._radar_attention.tier = 'live'
    before = e._radar_attention_knobs()
    e._radar_attention.unattended = True
    with pytest.raises(ae._RadarBudget):
        e._radar_checkpoint(dict(attention_knobs=before, prefetch=True))


@pytest.mark.parametrize('state', ['retarget', 'source', 'partial', 'short', 'failed'])
def test_budget_retry_does_not_hide_real_waiting(state):
    run_page(r'''
renderRadar({radar:manifest(),ts:100900});
radarView.refresh={state:'history',nextRetry:100930,retryReason:'budget'};
radarView.payloadTs=100900;
if(STATE==='retarget'){radarCamera.lon+=1;radarRetarget();radarUpdateReady();}
if(STATE==='source'){radarView.pendingSource={frames:[],data:manifest('b')};}
if(STATE==='partial'){radarView.loaded[0].ready=false;}
if(STATE==='short'){radarView.loaded=radarView.loaded.slice(-4);radarUpdateReady();}
if(STATE==='failed'){radarView.refresh.state='failed';}
assert.notEqual(radarPendingRetry(),null,'budget silence concealed '+STATE);
'''.replace('STATE', json.dumps(state)))


@pytest.mark.parametrize('unattended', [True, False])
def test_prefetch_flip_mid_acquisition_finishes_eight_frames(make_emitter, hybrid, monkeypatch, unattended):
    monkeypatch.setattr(ae, 'RADAR_ATTENTION_MODE', 'active')
    e = make_emitter(); e._radar_attention.tier = 'live'
    e._radar_attention.unattended = not unattended
    original = e._radar_fill_frame
    def flip(*args, **kwargs):
        frame = original(*args, **kwargs)
        e._radar_attention.unattended = unattended
        return frame
    monkeypatch.setattr(e, '_radar_fill_frame', flip)
    e._do_radar(intent_triggered=False)
    assert sum(f['complete'] for f in e._radar_result.frames) == 8
    assert e._radar_pass['outcome'] != 'superseded'
    assert not any(e._radar_pending.get(k) for k in ('newest', 'four', 'eight'))


def test_unattended_flip_during_history_stops_optional_retry(make_emitter, hybrid, monkeypatch):
    monkeypatch.setattr(ae, 'RADAR_ATTENTION_MODE', 'active')
    e = make_emitter(); e._radar_attention.tier = 'live'; hybrid.view()
    original = e._radar_fill_frame
    def flip(*args, **kwargs):
        frame = original(*args, **kwargs)
        if args[1] != hybrid.latest:
            e._radar_attention.unattended = True
        return frame
    monkeypatch.setattr(e, '_radar_fill_frame', flip)
    e._do_radar(intent_triggered=False)
    assert sum(f['complete'] for f in e._radar_result.frames) == 8
    assert not e._radar_pending.get('optional')
    assert 'radar' not in e._retries


def test_concurrent_reports_have_one_ordered_marker_writer(server, monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    monkeypatch.setattr(server.Handler.__bases__[0], 'do_GET', lambda self: None)
    poll(server, monkeypatch, report(0))
    barrier = threading.Barrier(8)
    def send(seq):
        h = object.__new__(server.Handler)
        h.client_address = ('127.0.0.1', 12345)
        h.path = '/wx.json?' + report(seq, 'none' if seq == 8 else 'radar')
        barrier.wait()
        h.do_GET()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(send, range(1, 9)))
    assert not (tmp_path / 'radar_viewing').exists()
    assert not list(tmp_path.glob('radar_viewing.tmp.*'))
