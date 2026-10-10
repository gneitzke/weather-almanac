"""Fixes made before the first rain of the season (2026-09-23).

1. Live flapped to warm about twice an hour on a Radar tab nobody left: the
   server cleared radar_viewing only on camera-accepted polls, so after a radar
   session owned the camera the "tab closed" signal never fired and the engine
   inferred it from a 10 s silence.
2. A Radar tab left open all day counted as someone studying it (21.6 MB by
   3 PM): after 30 min without a touch it keeps its loop but stops prefetching.
3. The Tempest's instant rain-start event (evt_precip) was discarded; rain onset
   waited up to a minute for the next obs_st.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib import almanac_emit as ae, properties
from lib import radar_engine
from lib.radar_attention import Attention, Signals, UNATTENDED_SEC
from tests.fixtures.config import make_config
from tests.test_radar_attention_serve import server  # noqa: F401
from tests.test_radar_buffer_page import run_page
from tests.test_radar_hybrid import hybrid  # noqa: F401

T0 = 1_800_000_000.0


def poll(server, monkeypatch, query, ip='127.0.0.1'):
    handler_class = next(c for c in vars(server).values() if isinstance(c, type)
        and c.__module__ == server.__name__ and hasattr(c, 'do_GET'))
    monkeypatch.setattr(handler_class.__bases__[0], 'do_GET', lambda self: None)
    h = object.__new__(handler_class)
    h.client_address = (ip, 12345); h.path = '/wx.json?' + query
    h.do_GET()


# ---- 1. viewing ------------------------------------------------------------
def test_a_plain_kiosk_poll_clears_viewing_even_while_a_radar_session_owns_the_camera(server, monkeypatch, tmp_path):
    poll(server, monkeypatch, 'view=radar')
    assert (tmp_path / 'radar_viewing').exists()
    server._radar_owner = dict(session='owner-session-123456', generation=3)   # a radar session owns the camera
    poll(server, monkeypatch, '_=1')                                            # the page left the Radar tab
    assert not (tmp_path / 'radar_viewing').exists()


def test_a_lan_browser_never_touches_the_kiosk_viewing_marker(server, monkeypatch, tmp_path):
    poll(server, monkeypatch, 'view=radar')
    poll(server, monkeypatch, '_=1', ip='192.168.1.2')
    assert (tmp_path / 'radar_viewing').exists()


def test_live_survives_a_slow_poll_and_ends_on_the_explicit_clear(make_emitter, hybrid, tmp_path):
    e = make_emitter(); now = ae.time.time()
    marker = tmp_path / 'radar_viewing'
    marker.write_text(json.dumps(dict(since=now - 100, last=now - 30)))     # 30 s since the last radar poll
    assert e.radar._viewing_now(now)
    marker.write_text(json.dumps(dict(since=now - 100, last=now - radar_engine.RADAR_VIEWING_LAPSE_SEC - 1)))
    assert not e.radar._viewing_now(now)
    marker.unlink()
    assert not e.radar._viewing_now(now)


# ---- 2. unattended ---------------------------------------------------------
def sig(now, **over):
    values = dict(local_hour=14.0, obs_age=30, rain_rate_mm=0.0, precip_pct=0, conditions='Clear',
                  echo=False, echo_age=60, viewing=True, viewed_age=0, touch_age=60)
    values.update(over)
    return Signals(now, **values)


def test_an_open_tab_without_a_touch_for_30_min_stays_live_but_stops_prefetching():
    a = Attention(T0, 'live')
    assert a.decide(sig(T0, touch_age=60)) == 'live' and a.knobs(14.0)['prefetch'] is True
    assert a.decide(sig(T0 + 1, touch_age=UNATTENDED_SEC + 1)) == 'live'
    k = a.knobs(14.0)
    assert a.unattended and k['prefetch'] is False and k['frames'] == 8 and k['tiles'] is True
    assert a.reason == 'radar tab open, unattended'
    assert a.decide(sig(T0 + 2, touch_age=0)) == 'live' and a.knobs(14.0)['prefetch'] is True   # a touch restores it
    assert a.decide(sig(T0 + 3, touch_age=None)) == 'live' and a.knobs(2.0)['frames'] == 8      # no touch ever: loop kept at night too
    assert a.knobs(2.0)['prefetch'] is False


def test_unattended_is_published(make_emitter, hybrid, tmp_path, monkeypatch):
    monkeypatch.setattr(radar_engine, 'RADAR_ATTENTION_MODE', 'active')
    e = make_emitter(); now = ae.time.time()
    (tmp_path / 'radar_viewing').write_text(json.dumps(dict(since=now, last=now)))
    a = e._build_payload()['radar']['attention']
    assert a['tier'] == 'live' and a['unattended'] is True                   # nobody has touched this fresh panel
    (tmp_path / 'presence').write_text(str(now))
    assert e._build_payload()['radar']['attention']['unattended'] is False


# ---- 3. rain start ---------------------------------------------------------
def parser_app():
    cc = SimpleNamespace(Obs=properties.Obs(), switchPanel=lambda *a, **k: None, button_list=[])
    cfg = make_config(System={'nc_rain': '0', 'Timeout': '5', 'stats_endpoint': '0'})
    return SimpleNamespace(config=cfg, CurrentConditions=cc), cc


def test_evt_precip_reaches_the_display_once(make_parser):
    app, cc = parser_app()
    parser = make_parser(app)
    calls = []
    original = parser.update_display
    parser.update_display = lambda kind: (calls.append(kind), original.__wrapped__(parser, kind) if hasattr(original, '__wrapped__') else None)
    parser.display_obs['precipStartTs'] = None
    parser.parse_evt_precip({'type': 'evt_precip', 'device_id': 111, 'evt': [1790000000]}, app.config)
    parser.parse_evt_precip({'type': 'evt_precip', 'device_id': 111, 'evt': [1790000000]}, app.config)   # websocket repeat
    parser.parse_evt_precip({'type': 'evt_precip', 'device_id': 111}, app.config)                         # malformed
    assert parser.display_obs['precipStartTs'] == 1790000000 and calls == ['evt_precip']


def test_both_transports_route_evt_precip_instead_of_discarding_it():
    for path, ignore in (('service/websocket.py', "['connection_opened', 'ack']"), ('service/udp.py', "['hub_status', 'device_status']")):
        src = Path(path).read_text()
        assert ignore in src and "'evt_precip']" not in src.split(ignore)[0][-80:] + ignore
        assert 'parse_evt_precip(self.message, self.config)' in src


@pytest.mark.parametrize('status,start,obs,now,expect', [
    ('Currently Dry', 1000, 990, 1010, 'Rain Starting'),      # event after the last observation
    ('Currently Dry', 1000, 1030, 1040, 'Currently Dry'),     # the next obs_st governs, even if dry
    ('Light Rain', 1000, 990, 1010, 'Light Rain'),            # measured rain always wins
    ('Currently Dry', 1000, 990, 1000 + ae.RAIN_START_HOLD_SEC + 1, 'Currently Dry'),   # observations stopped: bounded
    ('Currently Dry', None, 990, 1010, 'Currently Dry'),
    ('Snow Likely', 1000, 990, 1010, 'Snow Likely'),
])
def test_rain_starting_status(status, start, obs, now, expect):
    assert ae.AlmanacEmitter._rain_starting(status, start, obs, now) == expect


def test_rain_starting_reaches_the_payload_and_warms_the_radar(make_emitter, monkeypatch):
    from tests.fixtures import obs_scenarios as scn
    scenario = scn.clear_day()
    obs_ts = scenario['Obs'].get('obsTs') or (ae.time.time() - 30)
    scenario['Obs'] = dict(scenario['Obs'], obsTs=obs_ts, precipStartTs=obs_ts + 5,
                           RainRate=['0.00', 'in/hr', 'Currently Dry'])
    monkeypatch.setattr(ae.time, 'time', lambda: obs_ts + 20)
    e = make_emitter(scenario)
    p = e._build_payload()
    assert p['rainStatus'] == 'Rain Starting', p['rainStatus']
    assert p['radar']['attention']['weather'] is True


# ---- 4. copy ---------------------------------------------------------------
def test_history_note_counts_the_loop_and_budget_pacing_stays_silent_on_a_full_loop():
    run_page(r'''
const r=manifest();renderRadar({radar:r,ts:100900});
radarView.refresh={state:'history',frameIndex:25,frameTotal:31,nextRetry:100930,retryReason:'budget'};radarView.payloadTs=100900;
assert.equal(radarPendingRetry(),null,'a budget retry over a full current loop must not be announced');
radarView.loaded[0].bitmap=null;radarUpdateReady();
assert.notEqual(radarPendingRetry(),null,'a budget retry with frames missing is real waiting');
radarView.refresh.retryReason='local';radarView.loaded[0].bitmap=bitmap();radarUpdateReady();
assert.notEqual(radarPendingRetry(),null,'other retry reasons are still reported');
''')
    html = Path('design/almanac/console_live.html').read_text()
    assert "copy='Refreshing · frame '+f.frameIndex+' of '+f.frameTotal" not in html


def test_legend_ticks_never_fall_below_the_display_floor():
    # 2026-09-24: with the scale starting at 15 dBZ the fixed "10" tick hung off the left end
    html = Path('design/almanac/console_live.html').read_text()
    assert '[floorDbz,10,20,30,40,50,60,70].filter((v,i,a)=>v>=floorDbz&&a.indexOf(v)===i)' in html
    ticks = [v for i, v in enumerate([15, 10, 20, 30, 40, 50, 60, 70]) if v >= 15 and [15, 10, 20, 30, 40, 50, 60, 70].index(v) == i]
    assert ticks == [15, 20, 30, 40, 50, 60, 70]
