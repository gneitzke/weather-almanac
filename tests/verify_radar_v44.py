"""Local Chromium evidence: no missing-tile pigment, delayed primary and temporal blends."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright
from tests.verify_radar_headless import AUDIT, radar_server, tile_png
from tests.verify_radar_v45 import acquisition_pixels


def verify(browser, server, theme, output):
    context = browser.new_context(viewport=dict(width=1024, height=600))
    context.add_init_script(AUDIT)
    # Fail closed if a fixture ever starts depending on an external service.
    context.route('**/*', lambda r: r.continue_() if r.request.url.startswith(server.url+'/') else r.abort())
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(server.url+'/?tabs=1&theme='+theme)
    page.locator('.tab[data-screen="s-radar"]').click()
    page.wait_for_function('radarReady().length===8 && radarReady().every(f=>f.bitmap) && radarTileBusy===0 && radarTileQueue.length===0 && radarGeoBusy===0 && radarGeoQueue.length===0')
    page.evaluate('radarView.paused=true;radarView.blend=null;radarView.current=radarView.good;radarEchoDirty=true;radarLoopSync()')
    # Finish the acquisition warm pass after history assembly released its tiles.
    page.evaluate('radarGeoRequest(true);radarQueueTiles()')
    page.wait_for_function('radarTileBusy===0 && radarTileQueue.length===0 && radarGeoBusy===0 && radarGeoQueue.length===0')
    page.wait_for_timeout(150)
    # Measure real rAF playback. Every display blend must use exactly two draws,
    # no smoothing, no tile fetches and no decode. Capture a real mid-blend still.
    page.evaluate('''()=>{
      window.v44={transitions:[],blends:[],reads:[]};const original=radarBlendPaint;
      radarBlendPaint=function(now){const b=radarView.blend;if(!b)return;
        if(!v44.transitions.some(t=>t.start===b.start))v44.transitions.push({start:b.start,stamp:b.to.stamp});
        const x=document.getElementById('rad-echo').getContext('2d'),draw=x.drawImage,alphas=[];x.drawImage=function(...args){alphas.push({alpha:x.globalAlpha,smooth:x.imageSmoothingEnabled});return draw.apply(x,args)};
        const start=performance.now();original(now);const ms=performance.now()-start;x.drawImage=draw;
        v44.blends.push({a:radarMetrics.blendAlpha,ms,alphas,stamp:radarView.current.stamp,expected:radarMetrics.blendAlpha>=.5?b.to.stamp:b.from.stamp});
      };
      audit.fetches=[];audit.decodeCount=0;radarView.paused=false;radarView.nextAt=0;radarLoopSync();
    }''')
    page.wait_for_function('radarView.blend && radarMetrics.blendAlpha>.2 && radarMetrics.blendAlpha<.8')
    # Freeze at the observed transition's exact midpoint for a reviewable still,
    # preserving the two real fixture scans already selected by the live clock.
    middle = page.evaluate('''()=>{const b=radarView.blend;radarBlendPaint(b.start+60);radarView.active=false;return {alpha:radarMetrics.blendAlpha,from:b.from.stamp,to:b.to.stamp,read:document.getElementById('rad-frame-time').textContent};}''')
    page.screenshot(path=str(output/f'mid-blend-{theme}.png'))
    page.evaluate('radarView.active=true;radarView.blend=null;radarView.nextAt=performance.now()+350;v44.transitions=[];radarWake()')
    page.wait_for_timeout(8500)
    playback = page.evaluate('''()=>{radarView.paused=true;radarView.blend=null;radarLoopSync();return {...v44,fetches:audit.fetches.filter(u=>u.includes('radar/t/')),decodes:audit.decodeCount,newest:radarView.good.stamp,memory:radarMemory(),observedMemory:audit.bytes()};}''')
    assert playback['decodes'] == 0 and playback['fetches'] == [], (playback['decodes'],playback['fetches'])
    assert all(len(b['alphas']) == 2 and not any(a['smooth'] for a in b['alphas']) and b['stamp'] == b['expected'] for b in playback['blends'])
    assert any(0 < b['a'] < 1 for b in playback['blends']) and middle['alpha'] == .5
    steps, holds = [], []
    for a, b in zip(playback['transitions'], playback['transitions'][1:]):
        (holds if a['stamp'] == playback['newest'] else steps).append(b['start']-a['start'])
    assert steps and all(abs(step-350) <= 12 for step in steps), steps
    assert holds and all(abs(h-1100) <= 10 for h in holds), holds
    assert playback['memory'] <= 40*1024*1024 and playback['observedMemory'] <= 40*1024*1024
    # Pixel oracle: two disjoint translucent echoes must each have half their
    # scan alpha at 60ms; overlapping returns retain alpha, not source-over dimming.
    pixel_blend = page.evaluate('''()=>{
      radarView.active=false;const x=document.getElementById('rad-echo').getContext('2d'),c=new OffscreenCanvas(956,490),cx=c.getContext('2d');
      const f={...radarView.good};cx.fillStyle='rgba(200,0,0,0.7058823529411765)';cx.fillRect(0,0,600,490);const from={...f,bitmap:c.transferToImageBitmap()};
      cx.fillStyle='rgba(0,0,200,0.7058823529411765)';cx.fillRect(350,0,606,490);const to={...f,bitmap:c.transferToImageBitmap()};radarView.blend={from,to,start:0};radarBlendPaint(60);
      const pixels=[100,478,850].map(px=>Array.from(x.getImageData(px,245,1,1).data));from.bitmap.close();to.bitmap.close();c.width=c.height=0;radarView.blend=null;return pixels;
    }''')
    assert [p[3] for p in pixel_blend] == [90, 180, 90], pixel_blend
    page.emulate_media(reduced_motion='reduce')
    page.evaluate('radarView.active=true;radarView.current=radarView.good;radarView.paused=true;v44.blends=[];radarEchoDirty=true;radarLoopSync()')
    page.locator('#rad-play').click()
    page.wait_for_function('radarView.singleSweep')
    page.wait_for_function('!radarView.singleSweep && radarView.paused', timeout=10000)
    rainviewer = page.evaluate("()=>{const source=radarView.data.sourceId;radarView.data.sourceId='rainviewer';const intervals=[2,4,8].map(n=>radarInterval(n));radarView.data.sourceId=source;return intervals;}")
    assert all(abs(n-180*350/110)<1e-8 for n in rainviewer), rainviewer
    reduced = page.evaluate('({blends:v44.blends.length,newest:radarView.current===radarView.good})')
    assert reduced == dict(blends=0, newest=True), reduced
    page.emulate_media(reduced_motion='no-preference')
    # Real source switch with all KATX requests delayed, KOTX arriving first.
    raw = tile_png()
    delayed = {'yes': True}
    def site_tile(route):
        if '/KATX/' in route.request.url and delayed['yes']:
            route.fulfill(status=503, body=b'fixture: nearest scan delayed')
        else:
            route.fulfill(status=200, content_type='image/png', body=raw)
    page.route('**/radar/t/*/iem-nexrad-n0b/**', site_tile)
    switch_payload={'data':server.data}
    page.route('**/wx.json*', lambda route: route.fulfill(status=200, content_type='application/json', body=json.dumps(switch_payload['data'])))
    switched = page.evaluate('''()=>{
      radarCameraSet({...radarView.data.center,zoom:7});radarSettle();radarView.paused=true;
      const r=structuredClone(radarView.data);r.sourceId='iem-nexrad-n0b';r.sourceMode='site';r.siteId='KATX';r.zoomMin=7;r.zoomMax=10;
      r.sites=['KOTX','KATX'].map(id=>({...radarSiteTable.find(s=>s.id===id),contributing:true,reporting:true}));
      r.tiles.frames.forEach(f=>{f.siteScans=r.sites.map(s=>({id:s.id,ts:f.ts}));f.levels={'7':false}});
      const tiles=radarTileSet(radarCamera,7),xs=tiles.map(t=>t.x),ys=tiles.map(t=>t.y),g={x0:Math.min(...xs),y0:Math.min(...ys),w:Math.max(...xs)-Math.min(...xs)+1,h:Math.max(...ys)-Math.min(...ys)+1};
      r.tiles.z=7;r.tiles.grid=g;r.tiles.newest.expectedMask=((1n<<BigInt(g.w*g.h))-1n).toString(16);r.tiles.newest.mask=r.tiles.newest.expectedMask;
      renderRadar({radar:r});return r;
    }''')
    switch_payload['data']={**server.data,'radar':switched}
    page.wait_for_function("radarView.data.sourceMode==='site' && radarView.current.drawnSites?.some(s=>s.id==='KOTX')")
    page.wait_for_timeout(700)
    switch = page.evaluate('({caption:document.getElementById("rad-src-cap").textContent,read:document.getElementById("rad-frame-time").textContent,sites:radarView.current.drawnSites})')
    assert switch['caption'].startswith('Camano Island radar') and 'KATX loading' in switch['caption'] and 'timeline' not in switch['caption'], switch
    assert all(s['id'] != 'KATX' for s in switch['sites']), switch
    page.screenshot(path=str(output/f'delayed-nearest-{theme}.png'))
    delayed['yes']=False
    page.wait_for_timeout(2100)
    page.evaluate('radarQueueTiles()')
    page.wait_for_function("radarView.current.drawnSites?.some(s=>s.id==='KATX') && !document.getElementById('rad-src-cap').textContent.includes('KATX loading')")
    recovery = page.locator('#rad-src-cap').inner_text()
    assert recovery.startswith('Camano Island radar'), recovery
    acquisition = acquisition_pixels(page, theme, output)
    assert not errors, errors
    times = sorted(b['ms'] for b in playback['blends'])
    result = dict(theme=theme, intervalMs=sum(steps)/len(steps), steps=steps, holds=holds,
                  blendDraws=2, blendCostMs=dict(median=times[len(times)//2], p95=times[int(len(times)*.95)], max=max(times)),
                  middle=middle, pixelBlend=pixel_blend, reduced=reduced, rainviewer=rainviewer, switch=switch, recovery=recovery, acquisition=acquisition,
                  playbackFetches=0, playbackDecodes=0, memory=playback['memory'], observedMemory=playback['observedMemory'])
    (output/f'v44-{theme}.json').write_text(json.dumps(result, indent=2))
    print('V4.4', json.dumps(result), flush=True)
    context.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=Path('/tmp/radar-v44'))
    args = parser.parse_args();args.output_dir.mkdir(parents=True, exist_ok=True)
    with radar_server() as server, sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--disable-gpu'])
        for theme in ('paper', 'night'):
            verify(browser, server, theme, args.output_dir)
        browser.close()
    print('RADAR V4.4 HEADLESS PASS: paper + night', flush=True)


if __name__ == '__main__':
    main()
