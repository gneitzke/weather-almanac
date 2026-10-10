"""Auto-only review fixes, server and engine sides: the page build handshake that
moves an open tab onto the build its server speaks, and intent records from
before the upgrade that must not carry the retired source choice anywhere.
Temp directories and loopback sockets only."""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

from lib import almanac_emit as ae
from lib import radar_engine
from tests.test_freshness_health import SERVE, _payload, serve_at  # noqa: F401
from tests.test_radar_review_oct_serve import _raw, _status
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401
from tests.test_radar_remote_serve import A, camera, request, server  # noqa: F401

PAGE = b'<!doctype html><head><meta name="almanac-build" content=""></head><body>page</body>'
OLD_INTENT = dict(seq=7, zoom=9, center='station', camera=True, source='site', sourceAcceptedAt=1000.0)


def _fetch(url, **headers):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=5) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


# --- P1: page build handshake -----------------------------------------------------------

def test_the_server_serves_its_startup_snapshot_stamped_with_its_build(serve_at, tmp_path):
    (tmp_path/'index.html').write_bytes(PAGE)
    module, url = serve_at(_payload())
    build = module._load_page()
    assert build == hashlib.sha256(PAGE).hexdigest()[:16]
    stamped = PAGE.replace(b'content=""', b'content="' + build.encode() + b'"')
    for path in ('/index.html?theme=night&tabs=1', '/'):
        status, headers, body = _fetch(url + path)
        assert status == 200 and body == stamped, path
        assert headers['ETag'] == f'"{build}"' and headers['Cache-Control'] == 'no-cache'
        assert headers['Content-Type'] == 'text/html; charset=utf-8'
    status, headers, _ = _fetch(url + '/wx.json?_=1')
    assert status == 200 and headers['X-Almanac-Build'] == build
    # A reload revalidates and an unchanged build costs no body.
    status, headers, body = _fetch(url + '/index.html', **{'If-None-Match': f'"{build}"'})
    assert status == 304 and body == b'' and headers['ETag'] == f'"{build}"'


def test_a_page_replaced_under_a_running_server_is_not_served(serve_at, tmp_path):
    """The reverse pairing - a newer page against an older server process - cannot
    come from this server: page and server change together, at restart."""
    (tmp_path/'index.html').write_bytes(PAGE)
    module, url = serve_at(_payload())
    build = module._load_page()
    (tmp_path/'index.html').write_bytes(PAGE.replace(b'page', b'newer page'))
    _, _, body = _fetch(url + '/index.html')
    assert b'newer' not in body and build.encode() in body
    assert _fetch(url + '/wx.json')[1]['X-Almanac-Build'] == build


def test_every_viewer_gets_the_build_not_only_controllers(server, tmp_path):
    server.WEB = str(tmp_path)
    (tmp_path/'index.html').write_bytes(PAGE)
    build = server._load_page()
    for address in ('127.0.0.1', '192.168.1.20', '8.8.8.8'):
        assert request(server, address)['X-Almanac-Build'] == build, address


def test_without_a_page_at_start_there_is_no_handshake(serve_at, tmp_path):
    module, url = serve_at(_payload())
    assert module._load_page() is None
    assert 'X-Almanac-Build' not in _fetch(url + '/wx.json')[1]
    (tmp_path/'index.html').write_text('<!doctype html>page')  # served from disk, as before
    response = _raw(url, b'GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n')
    assert _status(response) == 200 and response.endswith(b'page')


def test_an_unstamped_page_is_served_whole(serve_at, tmp_path):
    """A page without the tag (or with two) is served byte for byte: it has no
    handshake of its own, and nothing else in it may be rewritten."""
    raw = b'<!doctype html><meta name="almanac-build" content=""><meta name="almanac-build" content="">'
    (tmp_path/'index.html').write_bytes(raw)
    module, url = serve_at(_payload())
    module._load_page()
    assert _fetch(url + '/index.html')[2] == raw


def test_the_server_entry_point_snapshots_the_page(tmp_path):
    (tmp_path/'wx.json').write_text('{"ts": 1}')
    (tmp_path/'index.html').write_bytes(PAGE)
    script = r'''
import os, runpy, signal, sys, threading, time
def stop():
    for _ in range(500):
        module = sys.modules.get('__main__')
        if getattr(module, '_page', None):
            with open(os.path.join(sys.argv[2], 'seen'), 'w') as f: f.write(module._page[0])
            break
        time.sleep(.01)
    os.kill(os.getpid(), signal.SIGTERM)
threading.Thread(target=stop, daemon=True).start()
runpy.run_path(sys.argv[1], run_name='__main__')
'''
    env = dict(os.environ, WFP_DATA=str(tmp_path/'wx.json'), WFP_WEB=str(tmp_path), WFP_PORT='0', WFP_BIND='127.0.0.1')
    result = subprocess.run([sys.executable, '-c', script, str(Path('design/almanac/kiosk/serve.py').resolve()), str(tmp_path)],
                            env=env, capture_output=True, text=True, timeout=30, cwd=str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert (tmp_path/'seen').read_text() == hashlib.sha256(PAGE).hexdigest()[:16]


def test_the_shipped_page_carries_exactly_one_build_tag():
    html = Path('design/almanac/console_live.html').read_bytes()
    spec = importlib.util.spec_from_file_location('serve_tag_check', SERVE)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    assert html.count(module.PAGE_BUILD_TAG) == 1


def test_the_launcher_restarts_the_server_with_the_page_it_copies():
    """The pairing the handshake relies on: every launch kills the old server,
    copies the page, then starts a server that snapshots it."""
    script = Path('design/almanac/kiosk/almanac-kiosk.sh').read_text()
    kill = script.index('pkill -f "kiosk/serve.py"')
    copy = script.index('cp -f "$APP/design/almanac/console_live.html" "$WEB/index.html"')
    start = script.index('\nlaunch_server\n')
    assert kill < copy < start


# --- should-fix: retired intent fields never reach a payload ---------------------------------

def test_the_server_strips_retired_fields_from_an_old_record(server, tmp_path):
    (tmp_path/'radar_intent').write_text(json.dumps(OLD_INTENT))
    record = server._read_radar_intent()
    assert record == {k: v for k, v in OLD_INTENT.items() if k not in ('source', 'sourceAcceptedAt')}
    acknowledged = json.loads(request(server, '127.0.0.1')['X-Radar-Intent'])
    assert 'source' not in acknowledged['intent'] and 'sourceAcceptedAt' not in acknowledged['intent']
    # A commit rewrites the record without them.
    request(server, '127.0.0.1', **camera(generation=1))
    written = json.loads((tmp_path/'radar_intent').read_text())
    assert 'source' not in written and 'sourceAcceptedAt' not in written and written['session'] == A


def test_the_engine_strips_retired_fields_before_the_payload(make_emitter, hybrid, multisite, tmp_path):
    hybrid.pin(None)
    (tmp_path/'radar_intent').write_text(json.dumps(OLD_INTENT))
    emitter = make_emitter()
    assert emitter.radar._read_intent() == {k: v for k, v in OLD_INTENT.items() if k not in ('source', 'sourceAcceptedAt')}
    emitter.radar._acquire()
    intent = emitter._build_payload()['radar']['intent']
    assert intent['seq'] == 7 and intent['zoom'] == 9
    assert 'source' not in intent and 'sourceAcceptedAt' not in intent


def test_both_readers_retire_the_same_fields(server):
    assert server.RETIRED_INTENT_FIELDS == radar_engine.RADAR_RETIRED_INTENT_FIELDS == {'source', 'sourceAcceptedAt'}
