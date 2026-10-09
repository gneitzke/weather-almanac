"""Deterministic local Chromium oracle for sliding radar windows, both themes.

Native tiles for incoming scans are resident fixture data, so stamp advances
exercise the real preload, compositor, queue and playback with zero HTTP reads.
Fresh native acquisition is separately covered by the existing headless runner.
"""
import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright
from tests.verify_radar_headless import AUDIT, radar_server


SCENARIO = r'''async()=>{
  const check=(ok,msg)=>{if(!ok)throw Error(msg);};
  cancelAnimationFrame(radarRaf);radarRaf=null;radarWake=()=>{};
  radarView.active=false;radarRelease();radarGeoClear();radarGestureCancel();
  // Explicit monotonic clock: every call below drives the actual renderer.
  let clock=10000;Object.defineProperty(performance,'now',{value:()=>clock});
  radarView.active=true;radarView.paused=false;radarView.singleSweep=false;
  radarCamera={...radarView.data.center,zoom:8};radarGesture.state='idle';
  radarBaseDirty=radarCameraDirty=radarEchoDirty=false;radarIdlePrefetchAt=Infinity;
  radarGeoRequest=()=>{};radarOverlayBuild=()=>{};
  const baseline=structuredClone(radarView.data),baseTs=baseline.tiles.frames[0].ts;
  const scan=i=>({ts:baseTs+i*120,stamp:radarStamp(baseTs+i*120),at:'12:'+String(i*2).padStart(2,'0'),levels:{'8':true},siteScans:[]});
  // Include older listed history: selection of eight differs from an omission.
  const manifest=(first,last)=>{const r=structuredClone(baseline);r.stale=false;r.tiles.frames=Array.from({length:last+1},(_,i)=>scan(i));r.observedTs=r.tiles.frames.at(-1).ts;r.observedAt=r.tiles.frames.at(-1).at;r.tiles.newest={...r.tiles.newest,stamp:r.tiles.frames.at(-1).stamp,mask:'ffffffffffffffff',expectedMask:'ffffffffffffffff'};return r;};
  const plate=new OffscreenCanvas(956,490),px=plate.getContext('2d');
  const decode=f=>{px.fillStyle='#8aa3c6';px.fillRect(0,0,956,490);f.bitmap=plate.transferToImageBitmap();f.camera={...radarCamera};f.ready=true;f.hasEcho=true;f.legend={remapped:true};};
  const reset=(missing=[])=>{radarRelease();radarView.paused=false;radarView.singleSweep=false;radarView.data=manifest(0,7);radarView.windowKey=radarWindowKey(radarView.data);radarView.loaded=radarView.data.tiles.frames.map(f=>({...f,revision:baseline.tiles.revision,sourceId:baseline.sourceId}));for(const [i,f] of radarView.loaded.entries())if(!missing.includes(i))decode(f);else f.levels={'8':false};radarView.current=radarView.good=radarView.loaded.at(-1);radarUpdateReady();radarLoopSync();radarEchoDirty=false;radarEchoPaint(radarView.current);};
  const id=f=>Math.round((f.ts-baseTs)/120),ids=fs=>fs.map(id);
  const read=()=>document.getElementById('rad-frame-time').textContent;
  const tick=()=>parseFloat(document.getElementById('rad-tick').style.left);
  const drawAt=at=>{clock=at;radarFrame(clock);};
  const step=()=>{const at=radarView.nextAt;check(at>0,'no scheduled step');drawAt(at-1);check(radarView.nextAt===at,'advanced before deadline');drawAt(at);const b=radarView.blend;const target=b?b.to:radarView.current,window=radarPlayback();if(!radarView.paused)check(radarView.nextAt-at===(target===window.at(-1)?1100:350),'frame interval');if(b){check(b.start===at,'blend start');drawAt(at+59);check(radarView.current===b.from,'subject before midpoint');drawAt(at+60);check(radarView.current===b.to,'subject at midpoint');drawAt(at+120);check(!radarView.blend,'blend completes at 120ms');}return id(radarView.current);};
  // Seed actual native inputs needed by unchanged newest-first acquisition.
  // Drop fixture native inputs between advances; composites remain untouched.
  const tile=new OffscreenCanvas(256,256),tx=tile.getContext('2d');
  const warm=async f=>{radarTiles.forEach(t=>t.bitmap.close());radarTiles.clear();const z=radarLevel(),all=radarTileSet(radarCamera,z,1);
    for(const level of [z-1,z+1])if(level>=radarView.data.zoomMin&&level<=radarView.data.zoomMax){const p=radarWorldPoint(radarCamera.lat,radarCamera.lon,level);for(let y=Math.floor(p[1]/256)-1;y<=Math.floor(p[1]/256);y++)for(let x=Math.floor(p[0]/256)-1;x<=Math.floor(p[0]/256);x++)all.push({z:level,x,y});}
    for(const t of all){const key=radarTileKey(f,t.z,t.x,t.y);tx.fillStyle='#8aa3c6';tx.fillRect(0,0,256,256);radarTiles.set(key,{...t,key,bitmap:tile.transferToImageBitmap(),hasEcho:true,meta:{opaquePixels:65536,unmatchedPixels:0,ambiguousPixels:0},sites:[]});}
  };
  const advance=async(first,last)=>{const r=manifest(first,last);await warm({...r.tiles.frames.at(-1),revision:r.tiles.revision,sourceId:r.sourceId});const subject=radarView.current,blend=radarView.blend,deadline=radarView.nextAt;renderRadar({radar:r});check(radarView.current===subject&&radarView.blend===blend&&radarView.nextAt===deadline,'undecoded manifest changed display');check(read().includes(radarFrameLabel(subject)),'loop read lost displayed subject');radarHistoryWork();check(radarView.good.bitmap,'new native scan composited');};
  reset();check(radarView.cycle.length===8,'initial cycle');
  check(step()===0&&step()===1,'initial scan sequence');
  const old=radarView.loaded[0],oldBitmap=old.bitmap,retained=new Map(radarView.loaded.slice(1).map(f=>[f.stamp,f.bitmap])),key=radarFrameKey(radarView.loaded[1]);
  const at=radarView.nextAt;drawAt(at);drawAt(at+30);const blend=radarView.blend,deadline=radarView.nextAt,subject=radarView.current;
  audit.fetches=[];audit.decodeCount=0;await advance(1,8);
  check(radarView.current===subject&&radarView.blend===blend&&radarView.nextAt===deadline,'manifest cut/restart');
  check(radarFrameKey(radarView.loaded[0])===key,'frame key changed with newest stamp');
  check([...retained].every(([stamp,b])=>radarView.loaded.find(f=>f.stamp===stamp).bitmap===b),'retained bitmap replaced');
  check(read().includes(radarFrameLabel(subject)),'loop read displayed measurement');
  check(!read().includes('Buffering'),'running read rebuffered');
  drawAt(at+60);drawAt(at+120);const sequence=[id(radarView.current)];
  while(id(radarView.current)!==7)sequence.push(step());
  const hold=radarView.nextAt;check(hold-(clock-120)===1100,'old newest hold');drawAt(hold-1);check(id(radarView.current)===7,'old hold cut short');
  sequence.push(step());check(sequence.join(',')==='2,3,4,5,6,7,1','slid wrap sequence '+sequence);
  check(oldBitmap.width===0&&!audit.live.has(oldBitmap),'aged bitmap not closed');check(tick()===0,'wrap tick');
  while(id(radarView.current)!==8)sequence.push(step());check(read()==='12:16 · newest'&&tick()===218,'newest read/tick');
  check(radarView.nextAt-(clock-120)===1100,'new newest hold');
  const advances=[{newest:8,fetches:audit.fetches.filter(u=>u.includes('radar/t/')).length}];
  for(let newest=9;newest<=11;newest++){const subject=radarView.current,deadline=radarView.nextAt;await advance(newest-7,newest);check(radarView.current===subject&&radarView.nextAt===deadline,'repeated advance changed cycle');const first=step();check(first===newest-7,'repeated wrap oldest');while(id(radarView.current)!==newest)step();advances.push({newest,fetches:audit.fetches.filter(u=>u.includes('radar/t/')).length});}
  check(advances.every(a=>a.fetches===0)&&audit.decodeCount===0,'stamp advance refetch/decode');
  const memory=radarMemory();check(memory<=RAD_MEMORY_CAP&&audit.bytes()<=RAD_MEMORY_CAP,'memory cap');
  // A decoded island counts, and a late middle frame joins only at wrap.
  reset([3]);check(ids(radarReady()).join(',')==='0,1,2,4,5,6,7','decoded island excluded');
  check(step()===0,'late fixture wrap');decode(radarView.loaded[3]);radarUpdateReady();radarLoopSync();check(ids(radarReady()).includes(3)&&!ids(radarPlayback()).includes(3),'late frame changed cycle');
  const late=[];for(let i=0;i<7;i++)late.push(step());check(late.join(',')==='1,2,4,5,6,7,0','late frame not skipped');
  check(step()===1&&step()===2&&step()===3,'late frame missing at next wrap');check(Math.abs(tick()-3/7*218)<.001,'ready tick position');
  // Paused oldest ages out silently, stays drawable until resume leaves it.
  reset();step();document.getElementById('rad-play').click();radarEchoDirty=false;
  const paused=radarView.current,pausedBitmap=paused.bitmap;await advance(1,8);
  check(radarView.current===paused&&radarView.nextAt===0&&pausedBitmap.width===956,'paused subject changed');check(read()==='Paused · 12:00 · −16 min','paused read '+read());
  document.getElementById('rad-play').click();check(radarView.current===paused,'resume cut to newest');radarEchoDirty=false;check(step()===1,'paused resume slid oldest');check(pausedBitmap.width===0,'paused bitmap leak');
  // Cold start: newest decoded immediately; four decoded scans starts playback.
  reset([0,1,2,3,4,5,6,7]);check(read()==='Buffering · 0 of 8'&&!radarView.nextAt,'cold zero');
  for(const i of [7,5,2]){decode(radarView.loaded[i]);radarUpdateReady();radarLoopSync();check(!radarView.nextAt,'started before four');}
  check(read()==='12:14 · newest'&&id(radarView.current)===7,'cold ready read');
  decode(radarView.loaded[0]);radarUpdateReady();radarLoopSync();check(radarView.nextAt&&read()==='12:14 · newest','four start');
  reset([0,1,2,3,4,5,6]);await advance(1,8);check(id(radarView.current)===8&&!radarView.nextAt&&read()==='12:16 · newest','cold newest immediate');
  // Identity captures render/source revision; changing mutable manifest fallback
  // cannot turn an old composite into a scan rendered by a different revision.
  const f=radarView.good,k=radarFrameKey(f);radarView.data.tiles.revision='000000000000';check(radarFrameKey(f)===k,'mutable render identity');
  radarView.data.tiles.revision=baseline.tiles.revision;
  window.v46Harness={reset,step,advance,id,read,drawAt,manifest,warm,decode};radarView.active=false;
  return {sequence,advances,late,memory,read:read(),zeroTileFetches:true,retainedComposites:retained.size,agedBitmapClosed:oldBitmap.width===0,frameMs:RAD_FRAME_MS,holdMs:RAD_HOLD_MS,blendMs:RAD_BLEND_MS};
}'''


def verify(browser, server, theme, output):
    context = browser.new_context(viewport=dict(width=1024, height=600))
    context.add_init_script(AUDIT)
    context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(server.url+'/') else route.abort())
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(server.url+'/?tabs=1&theme='+theme)
    page.locator('.tab[data-screen="s-radar"]').click()
    page.wait_for_function('radarReady().length===8 && radarTileBusy===0 && radarTileQueue.length===0 && radarGeoBusy===0 && radarGeoQueue.length===0')
    page.route('**/wx.json*', lambda route: route.abort())
    result = page.evaluate(SCENARIO)
    page.emulate_media(reduced_motion='reduce')
    result['reduced'] = page.evaluate(r'''async()=>{
      const {reset,step,advance,id,read}=v46Harness,check=(ok,msg)=>{if(!ok)throw Error(msg);};
      radarView.active=true;reset();check(!radarView.nextAt,'reduced idle animated');
      document.getElementById('rad-play').click();radarEchoDirty=false;
      check(step()===0&&step()===1,'reduced sweep start');
      await advance(1,8);check(id(radarView.current)===1,'reduced manifest cut');
      const sequence=[];while(!radarView.paused){sequence.push(step());check(!radarView.blend,'reduced blend');}
      check(sequence.join(',')==='2,3,4,5,6,7','single sweep changed');
      check(read()==='Paused · 12:14 · −2 min','reduced old newest read');
      document.getElementById('rad-play').click();radarEchoDirty=false;check(step()===1,'reduced slid wrap');
      while(!radarView.paused)step();check(id(radarView.current)===8,'reduced new newest');
      radarView.active=false;return {sequence,read:read(),blends:0};
    }''')
    assert not errors, errors
    page.screenshot(path=str(output/f'v46-{theme}.png'))
    (output/f'v46-{theme}.json').write_text(json.dumps(result, indent=2))
    print(theme, json.dumps(result), flush=True)
    context.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=Path('/tmp/radar-v46'))
    args = parser.parse_args();args.output_dir.mkdir(parents=True, exist_ok=True)
    with radar_server() as server, sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--disable-gpu'])
        for theme in ('paper', 'night'):
            verify(browser, server, theme, args.output_dir)
        browser.close()
    print('RADAR V4.6 HEADLESS PASS: paper + night', flush=True)


if __name__ == '__main__':
    main()
