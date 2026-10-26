"""serve.py's side of the motion budget: the panel reports its rain frame rate
on every poll (motion=<fps>:<reason>); /health shows it beside the engine's
machine load, and a tier change leaves one line in the journal. Only the
panel is believed, and only the page's own vocabulary is accepted. Neither
ever changes the health verdict."""
import time
import urllib.request

from tests.test_freshness_health import _get, _payload, serve_at  # noqa: F401

PERF = {'cpu': 0.41, 'cores': 4, 'tempC': 61.3, 'undervolt': False, 'undervoltAgeSec': None}


def _poll(url, motion):
    with urllib.request.urlopen(f'{url}/wx.json?_=1&motion={motion}', timeout=5) as response:
        response.read()


def test_health_shows_the_machine_load_and_the_panels_motion_tier(serve_at, capsys):
    module, url = serve_at(_payload(perf=PERF))
    status, body = _get(f'{url}/health')
    assert status == 200 and body['perf'] == PERF and body['motion'] is None
    _poll(url, '30:start')
    _poll(url, '30:start')                         # repeats are not news
    _poll(url, '60:headroom')
    status, body = _get(f'{url}/health')
    assert status == 200
    assert body['motion']['fps'] == 60 and body['motion']['reason'] == 'headroom'
    assert 0 <= body['motion']['sinceSec'] <= 2
    log = [line for line in capsys.readouterr().out.splitlines() if line.startswith('motion:')]
    assert log == ['motion: - -> 30 fps (start)', 'motion: 30 -> 60 fps (headroom)']


def test_anything_outside_the_pages_vocabulary_is_ignored(serve_at):
    module, url = serve_at(_payload())
    for junk in ('45:cpu', '30:because', '30', ':cpu', '30:cpu:extra', '%0A30:cpu'):
        _poll(url, junk)
    assert _get(f'{url}/health')[1]['motion'] is None


def test_a_lan_browser_cannot_set_the_panels_tier(serve_at, monkeypatch):
    module, url = serve_at(_payload())
    monkeypatch.setattr(module, '_is_loopback', lambda address: False)
    _poll(url, '0:power')
    assert _get(f'{url}/health')[1]['motion'] is None


def test_motion_and_load_never_change_the_verdict(serve_at):
    module, url = serve_at(_payload(ts=int(time.time()) - 3600, perf=PERF))
    _poll(url, '0:power')
    status, body = _get(f'{url}/health')
    assert status == 503 and body['reason'] == 'engine stalled'    # the stale engine is still the story
    assert body['motion']['fps'] == 0 and body['perf'] == PERF


def test_an_older_engine_without_perf_reads_as_none(serve_at):
    module, url = serve_at(_payload(perf='garbage'))
    assert _get(f'{url}/health')[1]['perf'] is None
