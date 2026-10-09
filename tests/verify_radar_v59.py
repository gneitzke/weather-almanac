"""Geometry handoffs: deterministic paint clock plus real loopback acquisition."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright
from tests.verify_radar_headless import AUDIT, radar_server

SCENARIO = r'''async kind=>{
  const check=(ok,msg)=>{if(!ok)throw Error(msg);};
  cancelAnimationFrame(radarRaf);radarRaf=null;radarWake=()=>{};
  radarCancelJobs();check(!radarJobs.size,'start with drained jobs');
  let clock=10000;Object.defineProperty(performance,'now',{value:()=>clock});
  radarRelease();radarGeoClear();radarView.active=true;radarView.paused=false;
  radarView.receivedAt=clock;radarView.receivedAge=0;radarView.data.stale=false;
  radarGesture.state='idle';radarIdlePrefetchAt=Infinity;
  radarGeoRequest=()=>{};radarOverlayBuild=()=>{};radarPostIntent=()=>{};
  radarQueueTiles=()=>{};radarView.pendingSource=null;
  radarCamera={...radarView.data.center,zoom:8};
  radarIntent.cameraKey=JSON.stringify(radarCamera);radarIntent.postedAt=0;
  const frames=radarView.data.tiles.frames.map(f=>({...f,sourceId:radarView.data.sourceId,revision:radarView.data.tiles.revision,levels:{'6':true,'7':true,'8':true,'9':true}}));
  radarView.data.tiles.frames=structuredClone(frames);radarView.windowKey=radarWindowKey(radarView.data);
  const cx=radarScratch.getContext('2d');
  for(const [i,f] of frames.entries()){cx.fillStyle=['#50956C','#359858','#167F34'][i%3];cx.fillRect(0,0,956,490);f.bitmap=radarScratch.transferToImageBitmap();f.ready=f.hasEcho=true;f.camera={...radarCamera};f.legend={remapped:true};}
  radarView.loaded=frames;radarView.good=frames.at(-1);radarView.current=frames[1];
  radarView.started=true;radarUpdateReady();radarView.cycle=frames.slice();radarView.nextAt=clock+350;
  radarBaseDirty=radarCameraDirty=radarEchoDirty=false;radarEchoPaint(radarView.current);radarLoopSync();
  const old=frames.map(f=>f.bitmap),start=clock,paints=[],decoded=[],frees=[];
  const oldClose=ImageBitmap.prototype.close;
  ImageBitmap.prototype.close=function(){const i=old.indexOf(this);if(i>=0&&this.width)frees.push(i);return oldClose.call(this);};
  radarMetrics.path=[];radarMetrics.peakMemoryBytes=radarMemory();audit.peak=audit.bytes();
  // Use the same public camera entry points as the stepper and pan gesture.
  if(kind==='pan'){radarBegin();radarCameraSet({...radarCamera,lon:radarCamera.lon+.2});radarSettle();}
  else radarZoomChange(kind==='double'?-2:-1);
  const warm=f=>{radarTiles.forEach(t=>t.bitmap.close());radarTiles.clear();const z=radarLevel(),tx=radarTileScratch.getContext('2d');
    for(const t of radarTileSet(radarCamera,z)){check(radarAdmit(RAD_TILE_BYTES),'tile admission');tx.fillStyle='#359858';tx.fillRect(0,0,256,256);const key=radarTileKey(f,z,t.x,t.y);radarTiles.set(key,{...t,key,bitmap:radarTileScratch.transferToImageBitmap(),hasEcho:true,sites:[],meta:{opaquePixels:65536,unmatchedPixels:0,ambiguousPixels:0}});radarUnreserve(RAD_TILE_BYTES);}
  };
  // Gate native availability on deterministic times. No faked compositor or
  // readiness: the real history worker must make each replacement bitmap.
  let nextDecode=start+900,previous=radarView.current,lastN=0,adoptAt=null,manifest=false,acquiring=false,maxBytes=0,maxAudit=0;
  const scheduled=[];
  for(clock=start+10;clock<=start+8500;clock+=10){
    if(clock>=nextDecode&&radarGesture.state==='idle'){
      const f=radarView.loaded.slice().reverse().find(f=>!f.bitmap);if(f)warm(f);nextDecode+=600;
    }
    if(!manifest&&clock>=start+300){const r=structuredClone(radarView.data);r.tiles.frames=r.tiles.frames.map(({bitmap,...f})=>f);r.tiles.z=radarLevel();r.tiles.grid={x0:0,y0:0,w:1,h:1};radarPreload(r);radarView.data=r;manifest=true;}
    const before=radarView.nextAt;
    radarFrame(clock);
    if(radarView.nextAt!==before&&before)scheduled.push({at:clock,gap:radarView.nextAt-before,holding:!!radarView.holdingWindow});
    if(clock===start+350){
      const ctx=document.getElementById('rad-echo').getContext('2d');
      check(Array.from(ctx.getImageData(478,245,1,1).data).join()==='22,127,52,255','playing pixels not retained');
      check(ctx.getImageData(kind==='pan'?955:10,245,1,1).data[3]===0,'retained window not reprojected');
      const draw=audit.smoothing.filter(d=>d.layer==='rad-echo'&&d.w===956&&d.args.length===4).at(-1);
      check(draw&&draw.args[2]===956*2**(radarCamera.zoom-8),'camera scale');
    }
    const n=frames.includes(radarView.loaded[0])?0:radarView.loaded.filter(f=>f.bitmap).length;
    if(n>lastN){decoded.push({at:clock,n,stamp:radarView.loaded.find(f=>f.bitmap&&!decoded.some(d=>d.stamp===f.stamp))?.stamp});check(n-lastN===1,'decode burst');lastN=n;}
    if(radarView.current!==previous){paints.push({at:clock,stamp:radarView.current.stamp,old:frames.includes(radarView.current),previousNewest:previous.stamp===frames.at(-1).stamp});previous=radarView.current;}
    check(!document.getElementById('rad-frame-time').textContent.includes('Buffering'),'read regressed to Buffering');
    check(radarReady().length>0&&radarView.nextAt>0,'playback lost');
    if(radarView.holdingWindow&&clock>start+610)check(document.getElementById('rad-note').textContent==='Playing previous view · sharpening '+n+' of 8','acquisition note');
    if(radarView.holdingWindow)acquiring=true;
    if(acquiring&&adoptAt===null&&!radarView.holdingWindow){adoptAt=clock;check(n>=4,'adopt before four');check(radarView.current===radarView.cycle[0],'adopt off wrap');}
    maxBytes=Math.max(maxBytes,radarMemory());maxAudit=Math.max(maxAudit,audit.bytes());check(maxBytes<=RAD_MEMORY_CAP&&maxAudit<=RAD_MEMORY_CAP,'memory bound');
  }
  check(adoptAt!==null,'never adopted');
  const oldSequence=paints.filter(p=>p.old).map(p=>p.stamp);
  check(oldSequence.join()===frames.slice(2).map(f=>f.stamp).join(),'old sequence interrupted '+oldSequence);
  check(decoded.slice(0,4).map(d=>d.stamp).join()===frames.slice(-4).reverse().map(f=>f.stamp).join(),'decode order');
  const gaps=paints.slice(1).filter((p,i)=>!p.previousNewest).map((p,i)=>p.at-paints[paints.indexOf(p)-1].at);
  check(gaps.every(g=>g>=280&&g<=420),'paint cadence '+gaps);
  check(scheduled.every(p=>[350,1100].includes(p.gap)),'deadline restarted');
  check(old.every(b=>b.width===0),'old composites leaked');
  check(frees.join()===frames.map((f,i)=>i).join(),'oldest-first release '+frees);
  check(radarMetrics.path.filter(p=>p.phase==='windowAdopted').length===1,'adoption count');
  ImageBitmap.prototype.close=oldClose;radarView.active=false;
  return {kind,adoptMs:adoptAt-start,decoded,paints,gaps,maxBytes,maxAudit,frees};
}'''


def context_page(browser, server, theme):
    context = browser.new_context(viewport=dict(width=1024, height=600))
    context.add_init_script(AUDIT)
    context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(server.url+'/') else route.abort())
    page = context.new_page()
    page.goto(server.url+'/?tabs=1&theme='+theme)
    page.locator('.tab[data-screen="s-radar"]').click()
    page.wait_for_function('radarReady().length===8 && !radarJobs.size && !radarTileQueue.length && !radarGeoQueue.length', timeout=60000)
    return context, page


def main():
    output = Path('/private/tmp/radar-v59-headless');output.mkdir(exist_ok=True)
    results = []
    with radar_server() as server, sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-gpu'])
        for theme in ('paper','night'):
            for kind in ('zoom','pan','double'):
                context,page=context_page(browser,server,theme)
                page.route('**/wx.json*',lambda route:route.abort())
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                result=page.evaluate(SCENARIO,kind);result['theme']=theme
                assert not errors,errors
                results.append(result);print(theme,kind,json.dumps(result),flush=True)
                page.screenshot(path=str(output/f'{theme}-{kind}.png'));context.close()
            context,page=context_page(browser,server,theme)
            # Real loopback fetch/decode, real RAF. Engine manifest advertises
            # fixture native levels; no remote origin or renderer substitutions.
            page.route('**/wx.json*',lambda route:route.abort())
            page.evaluate('''()=>{radarView.data.tiles.frames.forEach(f=>f.levels['6']=true);radarMetrics.path=[];window.acquisitionSamples=[];window.trace59=[];window.subjects59=[];let frame=0,subject=radarView.current;const originalFrame=radarFrame,originalTrace=radarTrace;radarTrace=(phase,extra={})=>{trace59.push({phase,at:performance.now(),frame,...extra});originalTrace(phase,extra);};radarFrame=now=>{frame++;originalFrame(now);if(subject!==radarView.current){subject=radarView.current;subjects59.push({at:now,stamp:subject.stamp,hold:subject===radarPlayback().at(-1)});}};audit.peak=audit.bytes();window.sampleTimer=setInterval(()=>{acquisitionSamples.push({at:performance.now(),read:document.getElementById('rad-frame-time').textContent,ready:radarReady().length,bytes:radarMemory(),audit:audit.bytes(),holding:!!radarView.holdingWindow});},20);}''')
            for z in (7,6,7,8):
                page.evaluate('(z)=>radarZoomChange(z-Math.round(radarCamera.zoom))',z)
                page.wait_for_timeout(3000)
            page.wait_for_function('!radarView.holdingWindow && radarReady().length===8',timeout=30000)
            result=page.evaluate('''()=>{clearInterval(sampleTimer);return {samples:acquisitionSamples,path:trace59,subjects:subjects59,peak:radarMetrics.peakMemoryBytes,auditPeak:audit.peak};}''')
            assert all(s['ready'] and 'Buffering' not in s['read'] for s in result['samples'])
            assert all(s['bytes']<=41943040 and s['audit']<=41943040 for s in result['samples'])
            assert result['auditPeak']<=41943040,result['auditPeak']
            for phase in ('decodeStart','decoded'):
                frame_ids=[x['frame'] for x in result['path'] if x['phase']==phase]
                assert len(frame_ids)==len(set(frame_ids)),(phase,frame_ids)
            paints=result['subjects']
            gaps=[b['at']-a['at'] for a,b in zip(paints,paints[1:]) if not a['hold']]
            assert gaps and all(280<=g<=420 for g in gaps),gaps
            result['paintGaps']=gaps
            result.update(theme=theme,kind='loopback');results.append(result)
            print(theme,'loopback PASS',len(result['samples']),'samples',result['peak'],'bytes',flush=True)
            context.close()
        browser.close()
    (output/'assertions.json').write_text(json.dumps(results,indent=2))
    print('RADAR V5.9 PASS: both themes, zoom/pan/two-level, loopback 8→7→6→7→8',flush=True)

if __name__=='__main__': main()
