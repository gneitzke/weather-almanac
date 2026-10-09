"""Execute the production radar script with deterministic clocks and raster ownership.

DOM/canvas and scheduling use deterministic stand-ins; network acquisition is
disabled. The production window, publication and ownership functions run together.
"""
import json
import subprocess
from pathlib import Path

import pytest


def run_page(body):
    html = Path('design/almanac/console_live.html').read_text()
    radar = html[html.index('  /* V4:'):html.index('  function render(data)')]
    progress = html[html.index('  setInterval(()=>{if(radarView.active)'):html.index('  setInterval(updateFreshness')]
    script = r'''
const assert=require('node:assert/strict');
let clock=10000,wall=1000000,ordinal=0;const timers=new Map(),intervals=[];
globalThis.performance={now:()=>clock};Date.now=()=>wall;
globalThis.setTimeout=fn=>{timers.set(++ordinal,fn);return ordinal};
globalThis.clearTimeout=id=>timers.delete(id);globalThis.setInterval=fn=>intervals.push(fn);
const live=new Set();
function bitmap(w=956,h=490){const b={width:w,height:h,closes:0,close(){this.closes++;this.width=this.height=0;live.delete(this)}};live.add(b);return b;}
globalThis.OffscreenCanvas=class {constructor(w,h){this.width=w;this.height=h}getContext(){return {clearRect(){},drawImage(b){assert.ok(b.width,'drawing closed raster')},save(){},restore(){},beginPath(){},rect(){},clip(){}}}transferToImageBitmap(){return bitmap(this.width,this.height)}};
const nodes=new Map();function $(id){if(!nodes.has(id))nodes.set(id,{hidden:false,disabled:false,style:{},dataset:{},classList:{contains:()=>false},addEventListener(){},setAttribute(){},getAttribute(){},removeAttribute(){},replaceChildren(){},append(){},getContext:()=>new OffscreenCanvas(956,490).getContext()});return nodes.get(id)}
const document={hidden:false,documentElement:{dataset:{}},querySelector:$,addEventListener(){},createTextNode:s=>s,createElement:()=>$('element')};
const window={crypto:require('node:crypto').webcrypto,matchMedia:()=>({matches:false,addEventListener(){}})};
const MutationObserver=class {observe(){}};
const sessionStorage={getItem:()=>null,setItem(){},removeItem(){}};
const clamp=(v,a,b)=>Math.max(a,Math.min(b,v)),isNum=v=>typeof v==='number'&&Number.isFinite(v);
const requestAnimationFrame=()=>1,cancelAnimationFrame=()=>{};
const fetch=()=>{throw Error('unexpected fetch')},poll=()=>{},activate=()=>{};
RADAR
PROGRESS
const realQueueTiles=radarQueueTiles;
radarWake=()=>{};radarGeoRequest=()=>{};radarQueueTiles=()=>{};
radarOverlayBuild=radarOverlayPaint=radarBasePaint=radarZoomRender=radarLegendRender=radarSourceRender=radarNoteRender=()=>{};
radarView.active=true;radarCamera={lat:47,lon:-122,zoom:8};radarCameraDirty=radarBaseDirty=radarEchoDirty=false;radarIdlePrefetchAt=Infinity;
const frame=i=>({ts:100000+i*120,stamp:String(i),at:String(i),levels:{8:true},siteScans:[],complete:true});
function manifest(source='a',last=7){return {available:true,sourceId:source,sourceMode:source==='a'?'mosaic':'site',siteId:source==='a'?null:'KATX',center:{lat:47,lon:-122},zoomMin:4,zoomMax:9,staleSec:600,ageSec:0,stale:false,observedTs:frame(last).ts,frameCount:8,refresh:{state:'idle'},tiles:{revision:'123456789abc',z:8,grid:{x0:1,y0:2,w:4,h:3},camera:{...radarCamera},frames:Array.from({length:8},(_,i)=>frame(last-7+i))}}}
function decode(f){f.bitmap=bitmap();f.camera={...radarCamera};f.hasEcho=true;f.ready=true;return f}
function seed(){radarView.data=manifest();radarView.loaded=radarView.data.tiles.frames.map(f=>decode({...f,sourceId:'a',revision:'123456789abc'}));radarView.good=radarView.current=radarView.loaded.at(-1);radarView.readyFrames=radarView.loaded.slice();radarView.cycle=radarView.loaded.slice();radarView.started=true;radarView.nextAt=clock+1100;radarView.windowKey=radarWindowKey(radarView.data);}
seed();
BODY
'''.replace('RADAR', radar).replace('PROGRESS', progress).replace('BODY', body)
    result = subprocess.run(['node'], input=script, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('action', ['release', 'replacement'])
def test_abandoned_source_closes_staged_plates_and_compositor(action):
    run_page(r'''
renderRadar({radar:manifest('b'),ts:100900});
const pending=radarView.pendingSource,held=pending.frames.slice(-3).map(f=>decode(f).bitmap);
radarCompositeJob={f:pending.frames[0]};
if(ACTION==='release')radarRelease();
else renderRadar({radar:manifest('c'),ts:100902});
assert.ok(held.every(b=>b.closes===1),'staged bitmaps escaped explicit ownership');
assert.ok(!radarCompositeJob,'abandoned compositor survived');
assert.equal(radarReserved,0);
'''.replace('ACTION', json.dumps(action)))


def test_pending_source_slides_without_discarding_decoded_progress():
    run_page(r'''
renderRadar({radar:manifest('b'),ts:100900});
const held=radarView.pendingSource.frames.slice(-3).map(decode);
renderRadar({radar:manifest('b',8),ts:100902});
assert.ok(held.every(f=>radarView.pendingSource.frames.includes(f)),'stamp advance discarded staged progress');
assert.ok(held.every(f=>f.bitmap.closes===0));
''')


@pytest.mark.parametrize('change', ['camera', 'siteScans', 'truncate'])
def test_pending_source_reconciliation_checks_full_identity_and_closes_omissions(change):
    run_page(r'''
const r=manifest('b');renderRadar({radar:r,ts:100900});
const f=decode(radarView.pendingSource.frames[0]),b=f.bitmap;
const next=structuredClone(r);
if(CHANGE==='camera'){next.tiles.camera.lon+=.01;radarCamera={...next.tiles.camera};}
if(CHANGE==='siteScans')next.tiles.frames[0].siteScans=[{id:'KATX',ts:f.ts-60}];
if(CHANGE==='truncate')next.tiles.frames=next.tiles.frames.slice(1);
renderRadar({radar:next,ts:100902});
assert.equal(b.closes,1,'superseded staged plate was retained or leaked');
assert.ok(!radarView.pendingSource.frames.some(f=>f.bitmap===b));
'''.replace('CHANGE', json.dumps(change)))


def test_late_animation_frame_keeps_full_interval_and_does_not_restart_blend():
    run_page(r'''
radarView.nextAt=clock-5000;radarFrame(clock);
const blend=radarView.blend;
assert.equal(radarView.nextAt,clock+350,'late RAF schedules a catch-up before the blend finishes');
clock+=16;radarFrame(clock);assert.equal(radarView.blend,blend);
''')


@pytest.mark.parametrize('count', [1, 2, 3])
def test_available_short_loop_can_settle_automatic_buffering(count):
    run_page(r'''
radarView.loaded=radarView.loaded.slice(-COUNT);radarView.readyFrames=radarView.loaded.slice();radarView.cycle=[];
radarView.data.frameCount=COUNT;radarView.data.tiles.frames=radarView.data.tiles.frames.slice(-COUNT);
radarSwitchStart(false);radarSwitch.committed=true;
intervals[0]();radarView.current=radarView.loaded[0];intervals[0]();
assert.equal(radarSwitch,null,'all available scans still say Updating view');
intervals[0]();assert.equal(radarSwitch,null,'automatic buffering restarted on a complete short loop');
'''.replace('COUNT', str(count)))


def test_paused_geometry_adopts_a_decoded_frame_when_newest_is_pending():
    run_page(r'''
radarView.paused=true;radarView.nextAt=0;radarView.cycle=[];radarRetarget();
radarView.loaded.slice(0,4).forEach(decode);radarUpdateReady();
assert.ok(radarView.current.bitmap,'paused geometry adoption replaced playable imagery with pending newest');
''')


def test_hidden_poll_cannot_restart_acquisition_and_show_can_rebuild():
    run_page(r'''
document.hidden=true;radarRelease();
radarQueueTiles=realQueueTiles;let pumps=0;radarPumpTiles=()=>pumps++;
renderRadar({radar:manifest(),ts:100900});
assert.equal(radarView.loaded.length,0,'hidden poll repopulated released echo window');
assert.equal(pumps,0,'hidden poll admitted tile work with no decode RAF');
assert.equal(live.size,0);
document.hidden=false;radarPreload(radarView.data);
assert.equal(radarView.loaded.length,8);assert.ok(pumps>0);
''')


def test_source_acceptance_uses_decoded_subject_when_newest_is_pending():
    run_page(r'''
renderRadar({radar:manifest('b'),ts:100900});
radarView.pendingSource.frames.slice(0,4).forEach(decode);
radarView.paused=true;radarAcceptSource();
assert.equal(radarView.pendingSource,null);
assert.ok(radarView.current.bitmap,'source handoff painted an undecoded newest');
assert.equal(radarView.current.ts,frame(3).ts);
''')


def test_repeated_cancel_and_switch_accounts_for_every_live_bitmap():
    run_page(r'''
const fixed=RAD_PLATE_BYTES*3+RAD_TILE_BYTES;
for(let i=0;i<20;i++){
  renderRadar({radar:manifest('b',7+i),ts:100900+i*120});
  radarView.pendingSource.frames.slice(-3).forEach(decode);
  assert.equal(radarMemory(),fixed+live.size*RAD_PLATE_BYTES);
  radarView.active=false;radarRelease();
  assert.equal(live.size,0);assert.equal(radarMemory(),fixed);
  radarView.active=true;seed();
}
''')
