"""Five scheduled publications -> real loopback TLS -> engine -> Chromium.

Only inter-poll waiting is accelerated. Acquisition, decoding and playback use
real elapsed time; reported latency includes the simulated discovery wait.
"""
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
from tests.test_emitter_lifecycle import FakeClock
from tests.conftest import loopback_only_when_offline


def verify(browser, server, origin, patch, theme, output):
    clock = FakeClock()
    base_stamp = server.data['radar']['observedTs']
    base = base_stamp + radar_engine.RADAR_IEM_READY_LAG_SEC
    patch.setattr(ae, 'Clock', clock)
    patch.setattr(ae.time, 'time', lambda: base + clock.now)
    real_mono = time.monotonic
    patch.setattr(ae.time, 'monotonic', lambda: real_mono() + clock.now)
    patch.setattr(radar_engine, 'RADAR_DIR', str(server.root/'radar'))
    shutil.rmtree(server.root/'radar'/'t'/radar_engine._radar_render_revision()/'iem-nexrad-n0b')
    patch.setattr(radar_engine, 'RADAR_IEM_METADATA_URL', origin.url+'/metadata')
    patch.setattr(radar_engine, 'RADAR_IEM_ARCHIVE_TEMPLATE', origin.url+'/archive/%Y%m%d%H%M')
    patch.setattr(radar_engine, 'RADAR_IEM_TILE_TEMPLATE', origin.url+'/tile/{stamp}/{z}/{x}/{y}')
    patch.setattr(radar_engine, 'RADAR_SITE_LIST_URL', origin.url+'/listing')
    origin.response = lambda path, raw: b'{"scans":[]}' if path.startswith('/listing') else raw
    app = SimpleNamespace(config=make_config(Station={'Latitude':'47.61', 'Longitude':'-122.33'}),
                          obsParser=SimpleNamespace(api_data={}))
    e = ae.AlmanacEmitter(SimpleNamespace(app=app, Obs={}, Met={}, Astro={}, Sager={}),
                          output_path=str(server.root/'engine'/'wx.json'))
    Path(e.output_path).parent.mkdir()
    viewed = Path(e.output_path).with_name('radar_viewed')
    viewed.write_text(str(base))
    publications = [(base_stamp, base)] + [
        (base_stamp+i*120, base+i*120+jitter)
        for i, jitter in enumerate((0, 7, 20, 1, 12), 1)]
    metadata_polls = []
    def behavior(path, ordinal):
        if path == '/metadata':
            now = ae.time.time()
            origin.newest_ts = max(stamp for stamp, ready in publications if ready <= now)
            metadata_polls.append(dict(at=now, stamp=origin.newest_ts))
            time.sleep(.5)
        return 'normal'
    origin.behavior = behavior
    origin.delay = .01
    context = browser.new_context(viewport=dict(width=1024, height=600))
    context.add_init_script(AUDIT)
    context.add_init_script('''(()=>{
      window.radarPaints=[];
      const draw=CanvasRenderingContext2D.prototype.drawImage;
      CanvasRenderingContext2D.prototype.drawImage=function(bitmap,...args){
        const result=draw.call(this,bitmap,...args);
        if(this.canvas.id==='rad-echo'&&this.globalAlpha>.01&&typeof radarView!=='undefined'){
          const frame=radarView.loaded.concat(radarView.retired).find(f=>f.bitmap===bitmap);
          if(frame&&!radarPaints.some(p=>p.ts===frame.ts))radarPaints.push({ts:frame.ts,at:performance.now()});
        }return result;
      };
    })()''')
    context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(server.url+'/') else route.abort())
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(server.url+'/?tabs=1&theme='+theme)
    page.locator('.tab[data-screen="s-radar"]').click()
    page.wait_for_function('radarReady().length===8')
    page.route('**/wx.json*', lambda route: route.abort())
    # Warm the production tiers before starting the measured readiness clock.
    e.radar._acquire(intent_triggered=False)
    e._runtime.running = True
    e.radar._arm_discovery()
    rows = []
    try:
        for stamp, ready in publications[1:]:
            before = len(origin.requests)
            ages = []
            while e.radar._result.ts_frame != stamp:
                event = e.radar._discovery_event
                assert event is not None
                delay = max(0, event.due-clock.now)
                # Run the registered production wakeup, not a direct fetch call.
                browser_started = page.evaluate('performance.now()')
                started = time.perf_counter()
                clock.advance(delay)
                fired_at = ae.time.time()
                page.evaluate('now=>{const start=performance.now();Date.now=()=>now+performance.now()-start}', fired_at*1000)
                viewed.write_text(str(fired_at))
                while 'radar' in e._runtime.inflight:
                    r = e._build_payload()['radar']
                    ages.append(r['ageSec'] + time.perf_counter()-started)
                    page.evaluate('r=>renderRadar({radar:r})', r)
                    page.wait_for_timeout(40)
                r = e._build_payload()['radar']
                page.evaluate('r=>renderRadar({radar:r})', r)
                assert ae.time.time() < ready+150, 'discovery failed to advance'
            page.wait_for_function('ts=>radarPaints.some(p=>p.ts===ts)', arg=stamp)
            painted_at = page.evaluate('ts=>radarPaints.find(p=>p.ts===ts).at', stamp)
            elapsed = (painted_at-browser_started)/1000
            latency = fired_at-ready+elapsed
            read = page.locator('#rad-frame-time').inner_text()
            row = dict(stamp=stamp, readyTs=ready, pollTs=fired_at,
                       discoveryWaitSec=fired_at-ready, onScreenSec=round(latency, 3),
                       ageSec=round(fired_at-stamp+elapsed, 3),
                       maxAgeSec=round(max(ages), 3), asOf=read,
                       frames=r['completeFrameCount'], requests=len(origin.requests)-before)
            assert latency <= 30, row
            assert max(ages) < 450, row
            assert r['observedAt'] in read and 'min old' not in read
            assert r['completeFrameCount'] >= 8
            assert e.radar._health.snapshot()['breaker'] == 'closed'
            rows.append(row)
            print(theme, row, flush=True)
        assert not errors, errors
        page.screenshot(path=str(output/f'{theme}-five-stamps.png'))
        result = dict(theme=theme, stamps=rows, metadataPolls=metadata_polls,
                      health=e.radar._health_payload(), consoleErrors=errors,
                      clock='inter-poll waits accelerated; TLS/render/playback real time')
        (output/f'{theme}.json').write_text(json.dumps(result, indent=2))
    finally:
        e.stop()
        context.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=Path('/tmp/radar-v50-harness'))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
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
    print('RADAR V5.0 READINESS LOOPBACK PASS: paper + night', flush=True)


if __name__ == '__main__':
    main()
