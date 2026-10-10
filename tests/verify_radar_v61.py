"""Local-only Smooth handoffs, memory ledger, and event-driven admission.

--baseline uses the supplied starting commit's renderer and 512px field fixtures.
No sleeps: acquisition predicates and RAF counts bound every observation.
"""
import argparse
import json
import subprocess
import types
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from tests.verify_radar_headless import AUDIT, radar_server
from lib import almanac_emit as ae, radar_palette as rp
from tests import verify_radar_v56 as smooth

MEASURE = r'''()=>({zoom:radarCamera.zoom,mem:radarMemory(),reserved:radarReserved,tiles:radarTiles.size,
 geo:radarGeoTiles.size,queue:radarTileQueue.length,busy:radarTileBusy,geoBusy:radarGeoBusy,
 loaded:radarView.loaded.filter(f=>f.bitmap).length,retired:radarView.retired.filter(f=>f.bitmap).length,
 cycle:radarView.cycle.filter(f=>f.bitmap).length,holding:!!radarView.holdingWindow,
 completed:performance.getEntriesByType('resource').filter(r=>r.name.includes('/radar/t/')).length})'''

SCHEDULER = r'''()=>{
 const check=(x,m)=>{if(!x)throw Error(m)};
 cancelAnimationFrame(radarRaf);radarRaf=null;radarWake=()=>{};
 radarView.paused=true;radarView.nextAt=0;radarView.blend=null;
 radarBaseDirty=radarCameraDirty=radarEchoDirty=false;radarIdlePrefetchAt=Infinity;
 const f=radarView.good,t=radarTileSet(radarCamera,radarLevel())[0];
 radarTileQueue=[{f,...t,key:'refused'}];radarGeoQueue=[];
 const admit=radarAdmit;radarAdmit=()=>false;radarPumpBlocked=null;
 radarPumpTiles();const before=radarMetrics.pumpIterations;
 for(let n=0;n<600;n++)radarFrame(performance.now()+n*17);
 check(radarMetrics.pumpIterations===before,'RAF retried refused admission');
 // A release changes the ledger; the very next frame can retry once.
 radarReserved+=1;radarPumpTiles();const held=radarMetrics.pumpIterations;
 radarUnreserve(1);radarFrame(performance.now());
 check(radarMetrics.pumpIterations===held+1,'release did not rearm pump');
 const release=radarMetrics.pumpIterations;
 renderRadar({radar:radarView.data,ts:Date.now()/1000,alerts:[]});
 check(radarMetrics.pumpIterations===release+1,'poll did not rearm pump');
 radarAdmit=admit;radarTileQueue=[];radarGeoQueue=[];radarPumpBlocked=null;
 return {animationFrames:600,refusedPumpIterations:0,releaseRetries:1,pollRetries:1};
}'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-o', type=Path, default=Path('/tmp/radar-v61-headless'))
    parser.add_argument('--baseline', action='store_true')
    args = parser.parse_args(); args.o.mkdir(parents=True, exist_ok=True)
    palette = rp
    if args.baseline:
        palette = types.ModuleType('baseline_palette'); palette.__file__ = rp.__file__
        exec(compile(subprocess.check_output(['git','show','7afbb3f:lib/radar_palette.py']), 'baseline_palette', 'exec'), palette.__dict__)
    results = []
    with patch.object(smooth, 'rp', palette), patch.object(radar_engine, 'SMOOTH_REVISION', palette.SMOOTH_REVISION), radar_server() as server, sync_playwright() as p:
        if args.baseline:
            (server.root/'index.html').write_bytes(subprocess.check_output(['git','show','7afbb3f:design/almanac/console_live.html']))
        smooth.smooth_fixtures(server)
        # Fixture metadata counts the actual output dimensions in either revision.
        def publish(on):
            smooth.publish(server, on)
            if args.baseline and on:
                server.data['radar']['tiles']['tileSize'] = 512
                (server.root/'wx.json').write_text(json.dumps(server.data))
        browser = p.chromium.launch(headless=True, args=['--disable-gpu'])
        for theme in ('paper', 'night'):
            server.data['radar'].update(zoomDesired=7,zoomAuto=False,zoomAutoLevel=7)
            server.data['radar']['tiles'].update(z=7,grid=None)
            publish(False); (server.root/'radar_smooth').write_text('off')
            context = browser.new_context(viewport=dict(width=1024,height=600),has_touch=True)
            context.add_init_script(AUDIT)
            context.route('**/*',lambda r:r.continue_() if r.request.url.startswith(server.url+'/') else r.abort())
            page = context.new_page(); errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(server.url+'/?tabs=1&theme='+theme);page.locator('.tab[data-screen="s-radar"]').click()
            drained = 'radarReady().length===8&&!radarView.holdingWindow&&!radarJobs.size&&!radarTileQueue.length&&!radarGeoQueue.length'
            page.wait_for_function(drained,timeout=60000)
            page.wait_for_function(drained+'&&radarView.cycle.length===8',timeout=60000)
            assert page.evaluate('radarCamera.zoom')==7
            page.evaluate('performance.setResourceTimingBufferSize(10000)')
            page.evaluate('window.measure='+MEASURE)
            rows=[dict(state='off',**page.evaluate('measure()'))]
            for on in (True,False):
                page.evaluate('''()=>{window.outgoing=radarView.cycle.slice();window.samples=[];window.violations=[];
                  window.take=()=>{const s=measure();samples.push(s);
                    if(s.holding&&s.loaded<4&&outgoing.some(f=>!f.bitmap||!f.bitmap.width))violations.push('lost outgoing before four');
                    if(s.loaded+s.retired<s.cycle)violations.push('owned plates below playing window');
                    if(radarPlayback().some(f=>!f.bitmap||!f.bitmap.width))violations.push('playing frame lost bitmap');
                    if(samples.length<600)requestAnimationFrame(take);};requestAnimationFrame(take);}''')
                page.locator('#rad-smooth').click()
                page.wait_for_function('(v)=>radarSmooth.pending===null&&radarSmooth.value===v',arg=on)
                publish(on);page.evaluate('poll(true)')
                if not args.baseline:
                    page.wait_for_function('radarView.holdingWindow')
                    page.wait_for_function("document.getElementById('rad-note').textContent.includes('sharpening')")
                    page.wait_for_function("getComputedStyle(document.getElementById('rad-note')).opacity==='1'")
                    page.screenshot(path=str(args.o/f'{theme}-{"on" if on else "off"}-acquiring.png'))
                page.wait_for_function('samples.length===600',timeout=30000)
                rows.append(dict(state='on' if on else 'off again',**page.evaluate('measure()'),
                    minOwned=page.evaluate('Math.min(...samples.map(s=>s.loaded+s.retired))'),
                    peak=page.evaluate('Math.max(...samples.map(s=>s.mem))')))
                if not args.baseline:
                    assert page.evaluate('violations')==[],page.evaluate('violations')
                    assert page.evaluate('samples.every(s=>s.zoom===7)')
                    page.wait_for_function(drained,timeout=60000)
                    assert page.evaluate('radarView.current.smooth') is on
                    assert page.evaluate('radarReserved')==0
                    assert page.evaluate('radarTileScratch.width')==256
                    assert page.evaluate('audit.peak')<=41943040
                    page.wait_for_function('radarView.cycle.length===8')
            result=dict(theme=theme,rows=rows,peak=page.evaluate('radarMetrics.peakMemoryBytes'),auditPeak=page.evaluate('audit.peak'),errors=errors)
            if not args.baseline:
                result['scheduler']=page.evaluate(SCHEDULER)
                # Real browser fetches held at a local route barrier. Abort must
                # settle all four owners without receiving headers or decoding.
                held=[];page.route('**/radar/t/**',lambda route:held.append(route))
                page.evaluate("""()=>{const f=radarView.good;radarTileQueue=radarTileSet(radarCamera,radarLevel()).slice(0,4).map(t=>({...t,f,key:radarTileKey(f,t.z,t.x,t.y),source:f.sourceId,epoch:radarEchoEpoch}));radarPumpTiles();}""")
                page.wait_for_function('radarJobs.size===4&&radarTileBusy===4')
                assert page.evaluate('radarReserved')==4*786432
                page.evaluate('radarCancelJobs()')
                page.wait_for_function('!radarJobs.size&&!radarTileBusy&&!radarReserved')
                result['cancelledFour']=dict(reserved=page.evaluate('radarReserved'),busy=page.evaluate('radarTileBusy'))
                for route in held:route.abort()
                assert not errors,errors
            results.append(result);context.close();print(theme,'PASS' if not args.baseline else 'BASELINE',json.dumps(result),flush=True)
        browser.close()
    (args.o/'assertions.json').write_text(json.dumps(results,indent=2))


if __name__=='__main__':main()
