"""Real elapsed-time Region zoom-out: production page/server/engine, loopback TLS only."""
from lib import radar_engine
import argparse
import json
import shutil
import ssl
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

from tests.verify_radar_headless import radar_server, AUDIT, ae, make_config
from tests.test_radar_keepalive import origin as origin_fixture
from tests.test_emitter_lifecycle import FakeClock
from tests.conftest import loopback_only_when_offline


def verify(browser, server, origin, patch, theme, output):
    clock = FakeClock()
    patch.setattr(ae, 'Clock', clock)
    patch.setattr(radar_engine, 'RADAR_DIR', str(server.root/'radar'))
    shutil.rmtree(server.root/'radar'/'t')  # no fixture echo tiles may satisfy this run
    patch.setattr(radar_engine, 'RADAR_IEM_METADATA_URL', origin.url+'/metadata')
    patch.setattr(radar_engine, 'RADAR_IEM_ARCHIVE_TEMPLATE', origin.url+'/archive/%Y%m%d%H%M')
    patch.setattr(radar_engine, 'RADAR_IEM_TILE_TEMPLATE', origin.url+'/tile/{stamp}/{z}/{x}/{y}')
    patch.setattr(radar_engine, 'RADAR_SITE_LIST_URL', origin.url+'/listing')
    origin.response = lambda path, raw: b'{"scans":[]}' if path.startswith('/listing') else raw
    origin.delay = .04
    origin.newest_ts = int(time.time())//120*120
    (server.root/'radar_zoom').write_text('8')
    # Auto is the only source policy; this scenario measures Region.
    patch.setattr(radar_engine.RadarEngine, '_auto_source', lambda self, ctx, site_ok: 'mosaic')
    (server.root/'radar_viewed').write_text(str(time.time()))
    app = SimpleNamespace(config=make_config(Station={'Latitude':'47.61', 'Longitude':'-122.33'}),
                          obsParser=SimpleNamespace(api_data={}))
    e = ae.AlmanacEmitter(SimpleNamespace(app=app, Obs={}, Met={}, Astro={}, Sager={}),
                          output_path=str(server.root/'wx.json'))
    snapshots, infos, errors = [], [], []
    lock = threading.Lock()
    def publish(*args):
        with lock:
            data = dict(server.data, radar=e._build_payload()['radar'])
            temp = server.root/'wx.json.new'
            temp.write_text(json.dumps(data)); temp.replace(server.root/'wx.json')
            r = data['radar']
            snapshots.append(dict(at=time.monotonic(), source=r['sourceId'], zoom=e.radar._result.zoom,
                                  complete=r['completeFrameCount'], window=r['frameCount']))
    patch.setattr(e.radar, '_emit_now', publish)
    patch.setattr(e, '_emit', publish)
    patch.setattr(ae.Logger, 'info', infos.append)
    e.radar._acquire(intent_triggered=False)
    publish()
    context = browser.new_context(viewport=dict(width=1024, height=600), has_touch=True)
    context.add_init_script(AUDIT)
    context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(server.url+'/') else route.abort())
    page = context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(server.url+'/?tabs=1&theme='+theme)
        page.locator('.tab[data-screen="s-radar"]').click()
        page.wait_for_function('radarReady().length===8')
        # Establish the steady-state morning scenario: initial loop acquisition
        # has aged out of the real 60-second request window before zoom input.
        quiet = max(0, e.radar._request_times[-1]+60-time.monotonic())
        while quiet > 0:
            page.wait_for_timeout(min(quiet, 1)*1000)
            quiet = max(0, e.radar._request_times[-1]+60-time.monotonic())
        # Deliberately stale z8 durable/runtime state, cold TLS after Wi-Fi-like
        # setup stalls and sparse HTTP failures. All real sockets are loopback.
        if e.radar._session: e.radar._session.close()
        e.radar._session = None
        handshake_count = 0
        handshake = ssl.SSLSocket.do_handshake
        def flaky(sock, *args, **kwargs):
            nonlocal handshake_count
            if not sock.server_side:
                handshake_count += 1
                ordinal = handshake_count
                time.sleep(.12)
                if ordinal in (1, 4):
                    raise TimeoutError('fake Wi-Fi handshake timeout')
            return handshake(sock, *args, **kwargs)
        patch.setattr(ssl.SSLSocket, 'do_handshake', flaky)
        origin.behavior = lambda path, ordinal: 'fail' if '/tile/' in path and path.endswith('/89') and ordinal == 1 else 'normal'
        e._runtime.running = True
        started = time.monotonic()
        snapshots.clear(); infos.clear()
        page.locator('#rad-zoom-out').click()
        page.wait_for_function('radarGesture.state==="idle" && radarCamera.zoom===7')
        rows = []
        last_tick = time.monotonic()
        while time.monotonic()-started < 60:
            e.radar._check_zoom()
            now = time.monotonic()
            clock.advance(now-last_tick)
            last_tick = now
            row = page.evaluate('({zoom:radarCamera.zoom,source:radarView.data.sourceId,ready:radarReady().length,text:document.getElementById("rad-frame-time").textContent})')
            row['elapsed'] = time.monotonic()-started
            rows.append(row)
            if (e.radar._result.zoom == 7 and row['ready'] == 8
                    and sum(f['complete'] for f in e.radar._result.frames) >= 8):
                break
            page.wait_for_timeout(100)
        (output/f'{theme}-trace.json').write_text(json.dumps(dict(rows=rows,publications=snapshots,logs=infos,health=e.radar._health.snapshot(),requests=origin.requests,clock=clock.now),indent=2))
        assert rows[-1]['ready'] == 8 and e.radar._result.zoom == 7, rows[-1]
        assert all(r['source'] == 'iem-mrms-lcref' and r['zoom'] == 7 for r in rows), rows
        assert all(s['source'] == 'iem-mrms-lcref' for s in snapshots)
        assert not any('SWITCH' in m for m in infos), infos
        assert e.radar._read_intent()['zoom'] == 7
        server.module._camera_persist_timer.join(1)
        assert (server.root/'radar_zoom').read_text().strip() == '7'
        assert not errors, errors
        health = e.radar._health.snapshot()
        assert health['localFailures'] >= 2 and health['breaker'] == 'closed'
        page.screenshot(path=str(output/f'{theme}.png'))
        # Provider cap is tile resolution only; publish fallback metadata while
        # holding camera at z8, and verify the existing scaled renderer keeps it.
        page.evaluate('''()=>{radarCameraSet({...radarCamera,zoom:8});radarSettle();
            const r=JSON.parse(JSON.stringify(radarView.data));r.zoomMax=7;r.zoom=7;r.zoomCapped=true;r.zoomDesired=8;r.zoomSource='RainViewer';
            renderRadar({radar:r});radarZoomRender();}''')
        assert page.evaluate('radarCamera.zoom') == 8
        assert 'scaled to this view' in page.locator('#rad-note').text_content() or page.evaluate('radarView.zoomNote.includes("scaled to this view")')
        result = dict(theme=theme, secondsToEight=rows[-1]['elapsed'], handshakes=handshake_count,
                      health=health, rows=rows, publications=snapshots, switches=[], errors=errors)
        (output/f'{theme}.json').write_text(json.dumps(result, indent=2))
        print(json.dumps({k:result[k] for k in ('theme','secondsToEight','handshakes','health','switches','errors')}), flush=True)
    finally:
        e.stop()
        if e.radar._session: e.radar._session.close()
        context.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=Path('/private/tmp/radar-v53-harness'))
    args = parser.parse_args(); args.output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as patch:
        patch.setenv('RADAR_NET_TEST', '0')
        loopback_only_when_offline.__wrapped__(patch)
        fixture = origin_fixture.__wrapped__(Path(tmp), patch)
        origin = next(fixture)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=['--disable-gpu'])
                for theme in ('paper', 'night'):
                    with radar_server() as server, pytest.MonkeyPatch.context() as scenario:
                        verify(browser, server, origin, scenario, theme, args.output_dir)
                browser.close()
        finally:
            try: next(fixture)
            except StopIteration: pass
    print('RADAR V5.3 LOOPBACK PASS: paper + night', flush=True)


if __name__ == '__main__': main()
