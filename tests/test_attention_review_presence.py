import json

import pytest
from tests.test_radar_attention_serve import server  # noqa: F401
from tests.test_radar_hybrid import hybrid  # noqa: F401
from lib import almanac_emit as ae


def test_presence_failed_write_does_not_consume_throttle(server, monkeypatch, tmp_path):
    replace = server.os.replace
    monkeypatch.setattr(server.os, 'replace', lambda *a: (_ for _ in ()).throw(OSError('disk full')))
    server._note_presence()
    monkeypatch.setattr(server.os, 'replace', replace)
    server._note_presence()
    assert (tmp_path / 'presence').exists()
    assert not list(tmp_path.glob('presence.tmp.*'))


@pytest.mark.parametrize('ip,query,present,viewed', [
    ('127.0.0.1', 'touch=1', True, False), ('127.0.0.1', 'touch=0', False, False),
    ('127.0.0.1', 'touch', False, False), ('127.0.0.1', 'touch=1&touch=0', False, False),
    ('192.168.1.2', 'touch=1&view=radar', True, False),
    ('127.0.0.1', 'view=radar', False, True)])
def test_presence_trusts_controllers_but_unordered_lan_view_is_not_viewing(server, monkeypatch, tmp_path, ip, query, present, viewed):
    # A LAN page views only through an ordered report (viewSession/viewSeq) with
    # input behind it: tests/test_radar_lan_viewing.py. A bare view=radar does not.
    # Invoke the handler without binding a socket or contacting any address.
    handler_class = next(c for c in vars(server).values() if isinstance(c, type)
        and c.__module__ == server.__name__ and hasattr(c, 'do_GET'))
    monkeypatch.setattr(handler_class.__bases__[0], 'do_GET', lambda self: None)
    h = object.__new__(handler_class)
    h.client_address = (ip, 12345); h.path = '/wx.json?'+query
    h.do_GET()
    assert (tmp_path / 'presence').exists() is present
    assert (tmp_path / 'radar_viewing').exists() is viewed
    assert (tmp_path / 'radar_viewed').exists() is viewed
    if ip == '192.168.1.2':
        assert (tmp_path / 'last_viewer').exists()


def test_deleted_session_demotes_to_recent_attention_not_live(make_emitter, hybrid, tmp_path):
    e = make_emitter(); now = ae.time.time()
    hybrid.view()
    marker = tmp_path / 'radar_viewing'
    marker.write_text(json.dumps(dict(since=now, last=now)))
    assert e._build_payload()['radar']['attention']['tier'] == 'live'
    marker.unlink()
    assert e._build_payload()['radar']['attention']['tier'] == 'warm'
