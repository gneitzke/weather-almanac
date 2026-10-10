"""Accelerated five-stamp hostile HTTPS provider -> engine -> loopback Chromium."""
from lib import radar_engine
import argparse
import json
import shutil
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright
from tests.verify_radar_headless import radar_server, AUDIT, ae, make_config
from tests.test_radar_keepalive import origin as origin_fixture
from tests.conftest import loopback_only_when_offline


def verify(browser, server, origin, monkeypatch, theme, output):
    app = SimpleNamespace(config=make_config(Station={'Latitude':'47.61', 'Longitude':'-122.33'}),
                          obsParser=SimpleNamespace(api_data={}))
    emitter = ae.AlmanacEmitter(SimpleNamespace(app=app, Obs={}, Met={}, Astro={}, Sager={}),
                               output_path=str(server.root/'engine'/'wx.json'))
    monkeypatch.setattr(radar_engine, 'RADAR_DIR', str(server.root/'radar'))
    # The all-features baseline duplicates all five levels for eight site scans.
    # This mosaic scenario needs no site copies; keep real production cache caps.
    shutil.rmtree(server.root/'radar'/'t'/radar_engine._radar_render_revision()/'iem-nexrad-n0b')
    monkeypatch.setattr(radar_engine, 'RADAR_IEM_METADATA_URL', origin.url+'/metadata')
    monkeypatch.setattr(radar_engine, 'RADAR_IEM_ARCHIVE_TEMPLATE', origin.url+'/archive/%Y%m%d%H%M')
    monkeypatch.setattr(radar_engine, 'RADAR_IEM_TILE_TEMPLATE', origin.url+'/tile/{stamp}/{z}/{x}/{y}')
    # Every tenth group of wire tile requests has three hangs, including retries.
    # This deliberately allows both attempts for one tile to lose.
    outcomes = []
    fault_lock = threading.Lock()
    def behavior(path, n):
        if not path.startswith('/tile/'):
            return 'normal'
        with fault_lock:
            outcome = 'hang' if len(outcomes) % 10 in (0, 3, 6) else 'normal'
            outcomes.append(outcome)
            return outcome
    origin.behavior = behavior
    context = browser.new_context(viewport=dict(width=1024, height=600))
    context.add_init_script(AUDIT)
    context.add_init_script('(()=>{const raf=requestAnimationFrame;window.renderTicks=0;window.requestAnimationFrame=cb=>raf(t=>{renderTicks++;cb(t)})})()')
    context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(server.url+'/') else route.abort())
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(server.url+'/?tabs=1&theme='+theme)
    page.locator('.tab[data-screen="s-radar"]').click()
    page.wait_for_function('radarReady().length===8')
    page.route('**/wx.json*', lambda route: route.abort())
    rows = []
    real_time = time.time
    start_stamp = int(real_time())//120*120
    try:
        for i in range(5):
            stamp = start_stamp+i*120
            origin.newest_ts = stamp
            started = time.monotonic()
            monkeypatch.setattr(ae.time, 'time', lambda: stamp+time.monotonic()-started)
            page.evaluate('now=>{const start=performance.now();Date.now=()=>now+performance.now()-start}', stamp*1000)
            before = len(origin.requests)
            paints = page.evaluate('renderTicks')
            seen = set()
            samples = []
            first_newest = None
            pass_times = []
            for repair in range(3):
                pass_start = time.monotonic()
                worker = threading.Thread(target=emitter.radar._acquire)
                worker.start()
                while worker.is_alive():
                    payload = emitter._build_payload()['radar']
                    if payload['observedTs'] is not None:
                        page.evaluate('r=>renderRadar({radar:r})', payload)
                        seen.add(payload['observedTs'])
                        if payload['observedTs'] == stamp and first_newest is None:
                            first_newest = time.monotonic()-started
                        samples.append(payload['ageSec'])
                    page.wait_for_timeout(40)
                worker.join()
                pass_times.append(round(time.monotonic()-pass_start,3))
                latest = next((f for f in emitter.radar._frames if f['ts'] == stamp), {})
                if latest.get('complete'):
                    break
                page.wait_for_timeout(2000)  # production partial-repair retry delay
            assert latest.get('complete'), 'newest still incomplete after bounded repair passes'
            payload = emitter._build_payload()['radar']
            page.evaluate('r=>renderRadar({radar:r})', payload)
            page.wait_for_function('ts=>radarView.good&&radarView.good.ts===ts&&radarView.good.bitmap', arg=stamp)
            elapsed = time.monotonic()-started
            h = emitter.radar._health.snapshot()
            read = page.locator('#rad-frame-time').inner_text()
            assert payload['observedTs'] == stamp and payload['ageSec'] <= 120
            assert not samples or max(samples) <= 120
            assert payload['completeFrameCount'] >= 1 and payload['refresh']['state'] == 'idle'
            assert max(pass_times) < radar_engine.RADAR_BUILD_DEADLINE_SEC+.4 and payload['observedAt'] in read
            assert page.evaluate('renderTicks') > paints
            assert not page.locator('#rad-note').inner_text().startswith("Couldn't refresh")
            assert h['breaker'] == 'closed'
            rows.append(dict(stamp=stamp, elapsedSec=round(elapsed,3), passTimesSec=pass_times, asOf=read,
                             ageSec=payload['ageSec'], maxManifestAgeSec=max(samples, default=0),
                             firstNewestSec=round(first_newest,3) if first_newest is not None else None, requests=len(origin.requests)-before,
                             hedges=h['hedges'], retries=h['retries'], paintContinued=True,
                             newestPublishedDuringFetch=stamp in seen))
            print(theme, rows[-1], flush=True)
        # Failed-but-fresh must be silent; only age > two cadences gets failure copy.
        copy = page.evaluate('''()=>{
          const r=radarView.data;radarIntent.postedAt=0;radarView.zoomNote='';
          radarView.refresh={state:'failed'};
          const texts=[];for(const age of [120,240,241]){
            Date.now=()=>1000*(r.observedTs+age);radarNoteRender();texts.push(document.getElementById('rad-note').textContent);
          }return texts;
        }''')
        assert copy[0] == copy[1] == '' and copy[2].startswith("Couldn't refresh · showing ")
        page.screenshot(path=str(output/f'{theme}-five-stamps.png'))
        assert not errors, errors
        result = dict(theme=theme, stamps=rows, failureCopy=copy,
                      health=emitter.radar._health.snapshot(), consoleErrors=errors,
                      tileRequests=len(outcomes), hangingRequests=outcomes.count('hang'),
                      hangFraction=outcomes.count('hang')/len(outcomes))
        (output/f'{theme}.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)
    finally:
        monkeypatch.setattr(ae.time, 'time', real_time)
        if emitter.radar._session:
            emitter.radar._session.close()
        context.close()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=Path('/tmp/radar-v49-harness'))
    args=parser.parse_args();args.output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv('RADAR_NET_TEST', '0')
        loopback_only_when_offline.__wrapped__(monkeypatch)
        fixture=origin_fixture.__wrapped__(Path(tmp), monkeypatch)
        origin=next(fixture)
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True, args=['--disable-gpu'])
                for theme in ('paper', 'night'):
                    with radar_server() as server:
                        # A new origin path namespace each theme avoids seeding retries.
                        origin.path_counts.clear()
                        verify(browser, server, origin, monkeypatch, theme, args.output_dir)
                browser.close()
        finally:
            try: next(fixture)
            except StopIteration: pass
    print('RADAR V4.9 FIVE-STAMP LOOPBACK PASS: paper + night', flush=True)


if __name__ == '__main__':
    main()
