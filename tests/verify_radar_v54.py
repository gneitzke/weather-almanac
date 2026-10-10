"""N7–N11: real Chromium renderer, both themes; deterministic rain stills."""
import base64
import io
import json
from pathlib import Path
from PIL import Image
from playwright.sync_api import sync_playwright
from tests.verify_radar_headless import radar_server, tile_png
from tests.test_radar_v54 import CADENCES, stamps
from tests.test_radar_v32 import indexed
from lib import almanac_emit as ae, radar_palette as rp
from lib import radar_engine

OUT=Path('/private/tmp/radar-v54-headless')


def check(page,theme):
    # Exact known-dBZ N0B pixels are remapped in Python, then decoded/drawn by
    # the page's real echo painter. Every stripe spans 32px of the plate.
    mapped=rp.remap(indexed('iem-nexrad-n0b'),'iem-nexrad-n0b',((5.0,(0x7F,0x82,0x95,180)),)+rp._RADAR_LUT)
    plate=Image.new('RGBA',(956,490))
    for i,(dbz,_) in enumerate(rp._RADAR_LUT):
        plate.paste(mapped.getpixel((int((dbz+33)*2),0)),(i*32,0,(i+1)*32,490))
    stream=io.BytesIO();plate.save(stream,'PNG')
    pixels=page.evaluate('''async data=>{
      radarGestureCancel();radarView.paused=true;radarView.active=false;
      const img=await createImageBitmap(await (await fetch(data)).blob());
      const f={...radarView.good,bitmap:img,camera:{...radarCamera},ready:true,hasEcho:true};
      radarView.data.stale=false;radarEchoPaint(f);
      const c=document.getElementById('rad-echo'),x=c.getContext('2d');
      const pixels=Array.from({length:26},(_,i)=>Array.from(x.getImageData(i*32+16,245,1,1).data));
      const filter=getComputedStyle(c).filter;img.close();return {pixels,filter};
    }''','data:image/png;base64,'+base64.b64encode(stream.getvalue()).decode())
    assert pixels['filter']=='none'
    assert pixels['pixels']==[list(c) for _,c in rp._RADAR_LUT],pixels
    metrics=[]
    for site in (False,True):
        legend=rp._RADAR_DISPLAY_RAMP if site else rp._RADAR_RAMP
        page.evaluate('''({legend,site})=>{radarView.data.legend=legend;radarView.data.sourceId=site?'iem-nexrad-n0b':'iem-mrms-lcref';radarView.legendKey=null;radarLegendRender();}''',dict(legend=legend,site=site))
        bands=page.locator('#rad-ramp i').evaluate_all('es=>es.map(e=>({width:e.getBoundingClientRect().width,bg:getComputedStyle(e).backgroundImage}))')
        starts=['rgb(118, 163, 138), rgb(80, 149, 108)','rgb(67, 164, 102), rgb(53, 152, 88)','rgb(38, 172, 80), rgb(22, 127, 52)']
        for band,expected in zip(bands[int(site):],starts):assert band['bg']=='linear-gradient(90deg, '+expected+')'
        span=70 if site else 65
        assert all(abs(b['width']-372*(v['hi']-v['lo'])/span)<1 for b,v in zip(bands,legend['bands']))
        ticks=page.locator('.rad-tick').evaluate_all('es=>es.map(e=>[e.textContent,parseFloat(e.style.left)])')
        labels=[5,10,20,30,40,50,60,70] if site else [10,20,30,40,50,60,70]
        assert [t[0] for t in ticks]==[str(v) for v in labels]
        assert all(abs(t[1]-372*(v-legend['floorDbz'])/span)<1 for t,v in zip(ticks,labels))
        # Sample actual screenshot pixels inside each new gradient, not just CSS.
        screenshot=Image.open(io.BytesIO(page.locator('#rad-ramp').screenshot())).convert('RGB')
        offset=372*5/70 if site else 0
        for v in legend['bands'][int(site):int(site)+3]:
            width=372*(v['hi']-v['lo'])/span
            for fraction in (.25,.5,.75):
                x=round(offset+width*fraction)
                actual=screenshot.getpixel((x,7))
                t=(x+.5-offset)/width
                expected=[round(a+(b-a)*t) for a,b in zip(bytes.fromhex(v['start'][1:]),bytes.fromhex(v['end'][1:]))]
                assert all(abs(a-b)<=2 for a,b in zip(actual,expected)),(theme,site,x,actual,expected)
            offset+=width
        metrics.append(dict(site=site,bands=bands,ticks=ticks))
    colors=[b[k] for b in rp._RADAR_RAMP['bands'] for k in ('start','end')]
    assert page.evaluate('''colors=>{
      const pigments=colors.map(c=>{const e=document.createElement('i');e.style.color=c;document.body.append(e);const v=getComputedStyle(e).color;e.remove();return v});
      return [...document.querySelectorAll(RAD_CONTROLS+',.rad-tick,.rad-legend-unit,#rad-src-cap,#rad-note,.rad-clear-note')].every(e=>!pigments.includes(getComputedStyle(e).color));
    }''',colors)
    copies=[]
    for gaps,mode,minutes,slow in CADENCES:
        inf=radar_engine._radar_scan_cadence(stamps(gaps))
        copy=page.evaluate('''r=>{
          radarSwitch=null;radarView.current=null;
          Object.assign(radarView.data,{sourceId:'iem-nexrad-n0b',sourceMode:'site',siteId:'KATX',
            sites:[{id:'KATX',contributing:true}],nexrad:{id:'KATX',name:'Camano Island'},
            legend:{...radarView.data.legend,remapped:true}},r);
          const cap=document.getElementById('rad-src-cap');cap.style.maxWidth='2000px';cap.style.width='2000px';
          radarSourceRender();return cap.textContent;
        }''',dict(scanCadenceSec=inf['scan_cadence_sec'],scanMode=mode,scanModeSource=inf['scan_mode_source'],scanningSlowly=slow,latestOnly=not gaps))
        if mode: assert ' · '+mode+' mode · new scan every ~'+str(minutes)+' min · ' in copy,copy
        elif minutes: assert ' · new scan every ~'+str(minutes)+' min · ' in copy and ' mode' not in copy,copy
        else: assert 'new scan' not in copy and 'latest only' in copy,copy
        assert ('scanning slowly' in copy)==slow
        assert all(word not in copy for word in ('NEXRAD','MRMS','mosaic','volumes','dBZ'))
        copies.append(copy)
    chain=page.evaluate('''()=>{
      radarSiteTable=[{id:'KATX',name:'A very long primary station name for overflow'}];
      Object.assign(radarView.data,{scanCadenceSec:240,scanMode:'precipitation',scanningSlowly:false,latestOnly:false,
        sites:[{id:'KATX',contributing:true},{id:'KLGX',contributing:true},{id:'KRTX',contributing:true}]});
      const cap=document.getElementById('rad-src-cap'),trace=[],append=cap.append;
      // Force every fit check to overflow and capture each complete render.
      Object.defineProperty(cap,'scrollWidth',{configurable:true,get:()=>9999});
      cap.append=function(...args){append.apply(this,args);trace.push(this.textContent)};
      radarSourceRender();delete cap.scrollWidth;cap.append=append;return trace;
    }''')
    without=next(i for i,c in enumerate(chain) if 'precipitation mode' in c and 'every ~' not in c)
    assert '+ 2 nearby' in chain[without]
    assert '+ 2 nearby' not in chain[without+1] and 'precipitation mode' in chain[without+1]
    assert all('IEM / NOAA' in c for c in chain)
    assert 'new scan every' in chain[0] and ', high resolution' not in chain[1]
    assert 'precipitation mode · every ~4 min' in chain[2]
    return dict(theme=theme,pixels=pixels,legends=metrics,captions=copies,dropChain=chain)


def main():
    OUT.mkdir(exist_ok=True)
    with radar_server() as server,sync_playwright() as p:
        # A rain-only fixture plate with the three green bands.
        # Replace the newest frame's native test shapes using real immutable tiles.
        latest=server.data['radar']['tiles']['frames'][-1]['stamp']
        newest=server.root/'radar'/'t'/radar_engine._radar_render_revision()/'iem-mrms-lcref'/'-'/latest
        for tile in newest.glob('*/*/*.png'):
            tile.write_bytes(tile_png(rp._RADAR_LUT[(int(tile.parent.name)+int(tile.stem))%10][1]))
        browser=p.chromium.launch(headless=True,args=['--disable-gpu'])
        results=[]
        for theme in ('paper','night'):
            context=browser.new_context(viewport=dict(width=1024,height=600))
            # Hermetic browser: only this local fixture origin is reachable.
            context.route('**/*',lambda route: route.continue_() if route.request.url.startswith(server.url+'/') or route.request.url.startswith('data:') else route.abort())
            page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(server.url+'/?tabs=1&theme='+theme)
            page.locator('.tab[data-screen="s-radar"]').click()
            page.wait_for_function('radarView.good && radarReady().length===8',timeout=20000)
            page.evaluate('clearTimeout(radarSwitch?.timer);radarSwitch=null;radarView.paused=true;radarView.current=radarView.good;radarView.receivedAge=0;radarView.receivedAt=performance.now();radarView.data.stale=false;radarState();radarEchoPaint(radarView.current);radarLoopSync();radarSourceRender()')
            page.screenshot(path=str(OUT/f'rain-plate-{theme}.png'))
            result=check(page,theme)
            assert not errors,errors
            results.append(result);context.close()
        browser.close()
    (OUT/'assertions.json').write_text(json.dumps(results,indent=2))
    print('V5.4 N7–N11 PASS: paper + night; rain stills: '+str(OUT))

if __name__=='__main__':main()
