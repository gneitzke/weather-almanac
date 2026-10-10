"""A LAN browser on the Radar tab counts as viewing (2026-10-09), like the panel.

Viewing evidence from a LAN page = Radar tab active AND document visible (the
page sends view=radar only then), admitted like every control side effect
(controller address, never the default gateway; same-origin; rate limited),
and only while that page has had real input (touch=1) within
LAN_VIEW_ATTENDED_SEC, so a forgotten tab cannot hold acquisition at live.
Handlers run without sockets; nothing contacts any host.
"""
import ipaddress
import json
from urllib.parse import urlencode

import pytest

from lib import almanac_emit as ae
from lib import radar_engine
from lib import radar_attention
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_remote_serve import server  # noqa: F401

LAN, PHONE, PANEL = '10.20.30.40', '10.20.30.41', '127.0.0.1'
S1, S2, P = 'lan-view-session-1', 'lan-view-session-2', 'panel-view-session'


@pytest.fixture
def clock(server, hybrid, monkeypatch):
    now = [hybrid.now]
    monkeypatch.setattr(server.time, 'time', lambda: now[0])  # the one time module: engine too
    return now


def request(server, address, headers=None, **params):
    h = object.__new__(server.Handler)
    h.client_address = (address, 1234)
    h.path = '/wx.json?' + urlencode(params, doseq=True)
    h.reads, h.headers = [], headers or {}
    h.send_header = lambda k, v: None
    h.do_GET()


def view(server, address=LAN, session=S1, seq=1, radar=True, touch=False, headers=None, **extra):
    params = dict(viewSession=session, viewSeq=seq, view='radar' if radar else 'none', **extra)
    if touch:
        params['touch'] = 1
    request(server, address, headers, **params)


def viewing(tmp_path):
    marker = tmp_path / 'radar_viewing'
    return json.loads(marker.read_text()) if marker.exists() else None


def attention(e):
    a = e._build_payload()['radar']['attention']
    return a['tier'], a['frames']


def test_windows_mirror_the_engine():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('lan_serve', Path('design/almanac/kiosk/serve.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.LAN_VIEW_ATTENDED_SEC == radar_attention.UNATTENDED_SEC
    assert module.RADAR_VIEWING_LAPSE_SEC == radar_engine.RADAR_VIEWING_LAPSE_SEC
    assert module.RADAR_VIEW_POLL_GAP_SEC == radar_engine.RADAR_VIEW_POLL_GAP_SEC


def test_visible_attended_lan_viewer_is_live_with_eight_frames(server, clock, make_emitter, tmp_path):
    e = make_emitter()
    view(server, touch=True)
    marker = viewing(tmp_path)
    assert marker and marker['last'] == clock[0]
    assert float((tmp_path / 'radar_viewed').read_text()) == clock[0]
    assert attention(e) == ('live', 8)
    # Polls keep it live without fresh input, inside the window.
    for seq in range(2, 6):
        clock[0] += 2
        view(server, seq=seq)
    assert viewing(tmp_path)['last'] == clock[0]
    assert attention(e) == ('live', 8)
    # Panel-only effects stay panel-only.
    assert not (tmp_path / 'radar_activity').exists() and server._renders == 0


def test_lan_view_without_any_input_is_not_viewing(server, clock, make_emitter, tmp_path):
    view(server)
    assert viewing(tmp_path) is None and not (tmp_path / 'radar_viewed').exists()
    assert attention(make_emitter())[0] != 'live'


def test_hidden_lan_tab_is_not_viewing(server, clock, make_emitter, tmp_path):
    e = make_emitter()
    view(server, touch=True)
    assert attention(e)[0] == 'live'
    clock[0] += 2
    view(server, seq=2, radar=False)          # the tab was hidden: the page reports view=none
    assert viewing(tmp_path) is None
    assert attention(e)[0] == 'warm'          # live drops at once; recent input holds warm
    # A late radar report from before the tab hid cannot resurrect the view.
    view(server, seq=1, touch=True)
    assert viewing(tmp_path) is None
    # A tab hidden from the start never views, however recently it was touched.
    view(server, PHONE, S2, seq=1, radar=False, touch=True)
    assert viewing(tmp_path) is None


def test_forgotten_lan_tab_decays_after_the_window(server, clock, hybrid, make_emitter, tmp_path):
    e = make_emitter()
    start = clock[0]
    view(server, touch=True)
    assert attention(e)[0] == 'live'
    seq = 1
    def poll(until):
        nonlocal seq
        while clock[0] < until:
            clock[0] += 60; hybrid.mono += 60; seq += 1   # monotonic too: the rate limit refills
            view(server, seq=seq)
    poll(start + radar_attention.UNATTENDED_SEC - 60)
    assert viewing(tmp_path) and attention(e)[0] == 'live'
    viewed = (tmp_path / 'radar_viewed').read_text()
    poll(start + radar_attention.UNATTENDED_SEC)
    assert viewing(tmp_path) is None
    assert (tmp_path / 'radar_viewed').read_text() == viewed   # the hint stops renewing
    assert attention(e)[0] == 'warm'                            # like any recent attention
    # ...and then it ages out: warm holds 45 min past the last counted view.
    poll(start + radar_attention.UNATTENDED_SEC + radar_attention.WARM_HOLD_SEC + radar_attention.DEMOTE_DWELL_SEC)
    assert attention(e)[0] not in ('live', 'warm')
    # A person comes back to the tab: one touch restores viewing.
    seq += 1
    view(server, seq=seq, touch=True)
    assert viewing(tmp_path) and attention(e)[0] == 'live'


def test_cross_site_request_cannot_view(server, clock, tmp_path):
    for headers in ({'Sec-Fetch-Site': 'cross-site'}, {'Origin': 'http://evil.example', 'Host': LAN + ':8137'}):
        view(server, touch=True, headers=headers)
    assert viewing(tmp_path) is None
    assert not (tmp_path / 'radar_viewed').exists() and not (tmp_path / 'presence').exists()
    view(server, touch=True, headers={'Sec-Fetch-Site': 'same-origin'})
    assert viewing(tmp_path)


def test_default_gateway_cannot_view(server, clock, monkeypatch, tmp_path):
    monkeypatch.setattr(server, '_DEFAULT_GATEWAYS', {ipaddress.ip_address('10.20.30.1')})
    view(server, '10.20.30.1', touch=True)
    view(server, '::ffff:10.20.30.1', S2, touch=True)
    assert viewing(tmp_path) is None and not (tmp_path / 'radar_viewed').exists()


def test_public_address_cannot_view(server, clock, tmp_path):
    view(server, '8.8.8.8', touch=True)
    assert viewing(tmp_path) is None


def test_panel_and_lan_viewers_do_not_clear_each_other(server, clock, make_emitter, tmp_path):
    e = make_emitter()
    view(server, PANEL, P, seq=1)
    view(server, touch=True)
    since = viewing(tmp_path)['since']
    clock[0] += 2
    view(server, PANEL, P, seq=2, radar=False)         # the panel leaves Radar
    assert viewing(tmp_path)['since'] == since and attention(e)[0] == 'live'
    clock[0] += 2
    view(server, PANEL, P, seq=3)                      # and comes back
    clock[0] += 2
    view(server, seq=2, radar=False)                   # the LAN tab leaves
    assert viewing(tmp_path) and attention(e)[0] == 'live'
    clock[0] += 2
    view(server, PANEL, P, seq=4, radar=False)
    assert viewing(tmp_path) is None


def test_closed_lan_tab_lapses_like_a_silent_panel(server, clock, make_emitter, tmp_path):
    e = make_emitter()
    view(server, touch=True)
    clock[0] += radar_engine.RADAR_VIEWING_LAPSE_SEC - 1
    assert attention(e)[0] == 'live'
    clock[0] += 1
    assert attention(e)[0] == 'warm'
    view(server, PANEL, P, seq=1, radar=False)         # any later report forgets it too
    assert viewing(tmp_path) is None


def test_two_lan_viewers_each_count_on_their_own_input(server, clock, tmp_path):
    view(server, touch=True)
    view(server, PHONE, S2, seq=1)                     # no input of its own
    clock[0] += radar_attention.UNATTENDED_SEC
    view(server, seq=2)
    view(server, PHONE, S2, seq=2)
    assert viewing(tmp_path) is None
    view(server, PHONE, S2, seq=3, touch=True)
    assert viewing(tmp_path)['last'] == clock[0]


def test_session_table_is_bounded(server, clock):
    for i in range(server._LAN_VIEW_SESSIONS + 10):
        view(server, f'192.168.1.{i + 2}', session=f'lan-view-bulk-{i:06d}', touch=True)
        clock[0] += 0.001
    assert len(server._lan_views) == server._LAN_VIEW_SESSIONS
