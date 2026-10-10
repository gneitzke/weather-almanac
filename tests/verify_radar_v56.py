"""Real loopback Smooth toggle, immutable PNG acquisition and both themes."""
import io
import json
from pathlib import Path
from PIL import Image, ImageDraw
from PIL.PngImagePlugin import PngInfo
from playwright.sync_api import sync_playwright
from tests.verify_radar_headless import radar_server, AUDIT
from lib import almanac_emit as ae, radar_palette as rp
from lib import radar_engine


def smooth_fixtures(server):
    root=server.root/'radar';revision=radar_engine._radar_render_revision(True)
    (root/'.smooth-revision').write_text(revision)
    source='iem-mrms-lcref'
    native=Image.new('P',(256,256))
    native.putpalette([v for c in rp._indexed_colors(source) for v in c],rawmode='RGBA')
    draw=ImageDraw.Draw(native);draw.ellipse((35,15,200,150),fill=94);draw.polygon([(110,90),(230,160),(200,220),(90,160)],fill=164)
    with rp.smooth_remap(native,source,rp.source_palette(source)) as mapped:
        info=PngInfo();meta={k:mapped.info[k] for k in ('remapped','unmatchedColors','opaqueColors','unmatchedPixels','opaquePixels','ambiguousPixels')}
        meta['revision']=rp.SMOOTH_REVISION
        info.add_text('radarRemap',json.dumps(meta));info.add_text('radarVisiblePixels',str(mapped.width*mapped.height-mapped.getchannel('A').histogram()[0]))
        out=io.BytesIO();mapped.save(out,'PNG',pnginfo=info)
    master=None
    old=root/'t'/radar_engine._radar_render_revision()
    for path in old.rglob('*.png'):
        target=root/'t'/revision/path.relative_to(old);target.parent.mkdir(parents=True,exist_ok=True)
        if master is None:target.write_bytes(out.getvalue());master=target
        else:target.hardlink_to(master)


def publish(server,smooth):
    # Simulate the asynchronous emitter publication AFTER the real HTTP handler
    # persists the marker. Engine/native reuse/restart is tested in pytest.
    r=server.data['radar'];r['smooth']=smooth
    r['tiles'].update(smooth=smooth,tileSize=256,
        revision=radar_engine._radar_render_revision(smooth),remapRevision=rp.SMOOTH_REVISION if smooth else rp.REMAP_REVISION)
    temp=server.root/'wx.tmp';temp.write_text(json.dumps(server.data));temp.replace(server.root/'wx.json')


def main():
    output=Path('/private/tmp/radar-v56-headless');output.mkdir(exist_ok=True)
    results=[]
    with radar_server() as server,sync_playwright() as p:
        smooth_fixtures(server)
        browser=p.chromium.launch(headless=True,args=['--disable-gpu'])
        for theme in ('paper','night'):
            publish(server,False);(server.root/'radar_smooth').write_text('off')
            context=browser.new_context(viewport=dict(width=1024,height=600),has_touch=True)
            context.add_init_script(AUDIT)
            context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(server.url+'/') else route.abort())
            page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(server.url+'/?tabs=1&theme='+theme);page.locator('.tab[data-screen="s-radar"]').click()
            ready='radarReady().length===8 && !radarView.holdingWindow && !radarJobs.size && !radarTileQueue.length && !radarGeoQueue.length'
            page.wait_for_function(ready,timeout=60000)
            geography=page.evaluate("document.getElementById('rad-base').toDataURL()")
            camera=page.evaluate('JSON.stringify(radarCamera)')
            box=page.locator('#rad-smooth').bounding_box();assert box['width']>=44 and box['height']>=44
            page.screenshot(path=str(output/f'{theme}-off.png'))
            for smooth in (True,False):
                page.locator('#rad-smooth').click()
                page.wait_for_function('(v)=>radarSmooth.pending===null&&radarSmooth.value===v',arg=smooth)
                assert (server.root/'radar_smooth').read_text().strip()==('on' if smooth else 'off')
                publish(server,smooth);page.evaluate('poll(true)')
                try:
                    page.wait_for_function('(v)=>radarView.data.tiles.smooth===v&&radarView.current?.smooth===v&&radarReady().length===8&&!radarView.holdingWindow&&!radarJobs.size&&!radarTileQueue.length&&!radarGeoQueue.length',arg=smooth,timeout=60000)
                except Exception:
                    print('DEBUG',errors,page.evaluate('({ready:radarReady().length,mem:radarMemory(),hold:radarView.holdingWindow,busy:radarTileBusy,queue:radarTileQueue.length,tiles:radarTiles.size,absent:[...radarTileAbsent],job:radarCompositeJob&&{done:radarCompositeJob.done.size,total:radarCompositeJob.tiles.length},frames:radarView.loaded.map(f=>({smooth:f.smooth,bitmap:!!f.bitmap}))})'));raise
                assert page.locator('#rad-smooth').get_attribute('aria-pressed')==str(smooth).lower()
                assert page.evaluate('JSON.stringify(radarCamera)')==camera
                if page.evaluate("document.getElementById('rad-base').toDataURL()") != geography:
                    page.screenshot(path=str(output/f'{theme}-geography-failure.png'))
                    print('GEOGRAPHY',page.evaluate('({camera:radarCamera,geo:radarGeoTiles.size,keys:[...radarGeoTiles.keys()],mem:radarMemory(),tiles:radarTiles.size})'),flush=True)
                    raise AssertionError('geography pixels changed')
                assert any('/radar/t/'+radar_engine._radar_render_revision(smooth)+'/' in path for path in server.requests)
                assert page.evaluate('radarReserved')==0
                assert page.evaluate('audit.peak')<=41943040
                # Reproject a real decoded plate at fractional zoom and inspect
                # the actual canvas draw flag, then restore camera without intent.
                flag=page.evaluate('''()=>{const old=radarCamera;radarCamera={...old,zoom:old.zoom+.25};radarEchoPaint(radarView.current);const draw=audit.smoothing.filter(d=>d.layer==='rad-echo').at(-1);radarCamera=old;radarEchoPaint(radarView.current);return draw.smooth;}''')
                assert flag is smooth
                page.screenshot(path=str(output/(f'{theme}-'+('on' if smooth else 'off-again')+'.png')))
                results.append(dict(theme=theme,smooth=smooth,peak=page.evaluate('audit.peak'),accountedPeak=page.evaluate('radarMetrics.peakMemoryBytes'),geographyUnchanged=True,target=box))
                if smooth:
                    page.reload();page.locator('.tab[data-screen="s-radar"]').click();page.wait_for_function(ready,timeout=60000)
                    assert page.locator('#rad-smooth').get_attribute('aria-pressed')=='true'
            assert not errors,errors
            context.close();print(theme,'PASS',flush=True)
        browser.close()
    (output/'assertions.json').write_text(json.dumps(results,indent=2))
    print(json.dumps(results,indent=2))

if __name__=='__main__':main()
