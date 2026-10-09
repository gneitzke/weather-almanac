"""The page honours radar.loopFrames: "Refreshing · frame N of M" only while a
frame of the loop is actually on its way, with M the engine's loop target.

Runs the production radar script (as test_radar_buffer_page.py does) with the
real corner note, loop sync and preload; DOM, canvas and timers are stand-ins.
Live bug (2026-10-09): warm tier, engine idle with 4 of 8 listed frames
complete, page showed "Refreshing · frame 4 of 8" forever.
"""
import subprocess
from pathlib import Path


def run_page(body):
    html = Path('design/almanac/console_live.html').read_text()
    radar = html[html.index('  /* V4:'):html.index('  function render(data)')]
    script = r'''
const assert=require('node:assert/strict');
let clock=10000,wall=1000000,ordinal=0;const timers=new Map(),intervals=[];
globalThis.performance={now:()=>clock};Date.now=()=>wall;
globalThis.setTimeout=fn=>{timers.set(++ordinal,fn);return ordinal};
globalThis.clearTimeout=id=>timers.delete(id);globalThis.setInterval=fn=>intervals.push(fn);
function bitmap(w=956,h=490){return {width:w,height:h,closes:0,close(){this.closes++;this.width=this.height=0}};}
globalThis.OffscreenCanvas=class {constructor(w,h){this.width=w;this.height=h}getContext(){return {clearRect(){},drawImage(){},save(){},restore(){},beginPath(){},rect(){},clip(){}}}transferToImageBitmap(){return bitmap(this.width,this.height)}};
const nodes=new Map();function $(id){if(!nodes.has(id))nodes.set(id,{hidden:false,disabled:false,textContent:'',style:{},dataset:{},classList:{contains:()=>false},addEventListener(){},setAttribute(){},getAttribute(){},removeAttribute(){},replaceChildren(){},append(){},animate(){},getContext:()=>new OffscreenCanvas(956,490).getContext()});return nodes.get(id)}
const document={hidden:false,documentElement:{dataset:{}},querySelector:$,addEventListener(){},createTextNode:s=>s,createElement:()=>$('element')};
const window={crypto:require('node:crypto').webcrypto,matchMedia:()=>({matches:false,addEventListener(){}})};
const MutationObserver=class {observe(){}};
const sessionStorage={getItem:()=>null,setItem(){},removeItem(){}};
const clamp=(v,a,b)=>Math.max(a,Math.min(b,v)),isNum=v=>typeof v==='number'&&Number.isFinite(v);
const requestAnimationFrame=()=>1,cancelAnimationFrame=()=>{};
const fetch=()=>{throw Error('unexpected fetch')},poll=()=>{},activate=()=>{};
RADAR
radarWake=()=>{};radarGeoRequest=()=>{};radarQueueTiles=()=>{};
radarOverlayBuild=radarOverlayPaint=radarBasePaint=radarZoomRender=radarLegendRender=radarSourceRender=()=>{};
radarView.active=true;radarCamera={lat:47,lon:-122,zoom:8};radarCameraDirty=radarBaseDirty=radarEchoDirty=false;radarIdlePrefetchAt=Infinity;
// frames: [index, complete-at-zoom-8]
const frame=(i,ok=true)=>({ts:100000+i*120,stamp:String(i),at:String(i),levels:{7:ok,8:ok,9:ok},siteScans:[]});
function payload({frames,loop,state='idle',pending={},frameIndex,frameTotal}){
  const p={newest:false,four:false,eight:false,optional:false,...pending};
  const refresh={state,frameIndex:frameIndex??frames.filter(f=>f.levels[8]).length,frameTotal:frameTotal??frames.length,pending:p};
  const r={available:true,sourceId:'a',sourceMode:'mosaic',siteId:null,center:{lat:47,lon:-122},zoomMin:4,zoomMax:9,
    staleSec:600,ageSec:0,stale:false,observedTs:frames.at(-1).ts,frameCount:31,completeFrameCount:frames.filter(f=>f.levels[8]).length,
    pending:p,refresh,tiles:{revision:'123456789abc',z:8,grid:{x0:1,y0:2,w:4,h:3},camera:{...radarCamera},frames}};
  if(loop!==undefined){r.loopFrames=loop;refresh.loopFrames=loop;}
  return r;
}
function decode(f){f.bitmap=bitmap();f.camera={...radarCamera};f.hasEcho=true;f.ready=true;return f}
// The page as it stands after showing `r` for a while: every frame the
// server holds is decoded and the loop is running.
function seed(r){radarView.data=r;radarView.refresh=r.refresh;radarView.loaded=r.tiles.frames.map(f=>({...f,sourceId:'a',revision:'123456789abc',ready:false,hasEcho:false}));
  radarView.loaded.forEach(f=>{if(f.levels[8])decode(f)});radarView.good=radarView.loaded.at(-1);
  radarView.current=radarView.loaded.filter(f=>f.bitmap).at(-1);radarUpdateReady();radarView.windowKey=radarWindowKey(r);radarLoopSync();}
function show(r){renderRadar({radar:r,ts:100900});radarNoteRender();return $('rad-note').textContent;}
function note(){radarNoteRender();return $('rad-note').textContent;}
const range=(a,b,ok=true)=>Array.from({length:b-a},(_,i)=>frame(a+i,ok));
BODY
'''.replace('RADAR', radar).replace('BODY', body)
    result = subprocess.run(['node'], input=script, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_live_state_old_payload_idle_four_of_eight_is_not_refreshing():
    """The exact payload captured from the Pi (before loopFrames existed): eight
    listed, four complete, idle, nothing pending. The four incomplete frames are
    not on their way, so the note must not claim a refresh."""
    run_page(r'''
const r=payload({frames:[...range(0,4,false),...range(4,8)],frameIndex:4,frameTotal:8});
assert.equal(r.loopFrames,undefined);
seed(r);
assert.equal(radarView.loaded.filter(f=>f.bitmap).length,4);
assert.equal(note(),'');
assert.equal(show(r),'');
// the loop plays the four it has
assert.ok(radarCouldLoop(),'four decoded frames must loop');
// and the note stays quiet however long it sits there
for(let i=0;i<30;i++){clock+=2000;wall+=2000;assert.equal(show(r),'');}
''')


def test_live_state_with_loop_target_lists_four_and_loops_them():
    run_page(r'''
const r=payload({frames:range(4,8),loop:4,frameIndex:4,frameTotal:4});
seed(r);
assert.equal(show(r),'');
assert.equal(radarStartCount(r),4);assert.equal(radarFrameGoal(r),4);
assert.equal(radarLoopTarget(r),4);
assert.ok(radarCouldLoop());
assert.ok(!/Buffering/.test($('rad-frame-time').textContent),$('rad-frame-time').textContent);
''')


def test_warm_to_live_says_refreshing_toward_eight_then_settles():
    run_page(r'''
seed(payload({frames:range(4,8),loop:4,frameIndex:4,frameTotal:4}));
assert.equal(note(),'');
// The pass for live begins: eight listed, the four new ones incomplete.
let frames=[...range(0,4,false),...range(4,8)];
assert.equal(show(payload({frames,loop:8,state:'history',pending:{eight:true},frameIndex:4,frameTotal:8})),'Refreshing · frame 4 of 8');
// Frame 3 lands on the server and the page decodes it.
frames=[...range(0,3,false),frame(3),...range(4,8)];
assert.equal(show(payload({frames,loop:8,state:'history',pending:{eight:true},frameIndex:5,frameTotal:8})),'Refreshing · frame 4 of 8');
decode(radarView.loaded.find(f=>f.stamp==='3'));
assert.equal(note(),'Refreshing · frame 5 of 8');
// Everything complete and decoded: idle, no note.
frames=range(0,8);
show(payload({frames,loop:8,frameIndex:8,frameTotal:8}));
radarView.loaded.filter(f=>!f.bitmap).forEach(decode);
assert.equal(note(),'');
assert.equal(radarView.loaded.length,8);
''')


def test_live_to_warm_keeps_the_eight_frame_loop_without_a_flash():
    run_page(r'''
const r8=payload({frames:range(0,8),loop:8,frameIndex:8,frameTotal:8});
seed(r8);
const before=radarView.loaded.map(f=>f.bitmap),current=radarView.current;
// Warm: target four, the eight complete frames stay listed.
assert.equal(show(payload({frames:range(0,8),loop:4,frameIndex:4,frameTotal:4})),'');
assert.equal(radarView.loaded.length,8);
assert.ok(radarView.loaded.every((f,i)=>f.bitmap===before[i]&&f.bitmap.closes===0),'a decoded frame was dropped or closed');
assert.equal(radarView.current,current);
assert.ok(radarCouldLoop());
''')


def test_a_fall_mid_acquisition_drops_frames_that_will_never_come():
    run_page(r'''
// Live was still fetching the older four when the tier fell to warm.
seed(payload({frames:[...range(0,4,false),...range(4,8)],loop:8,state:'history',pending:{eight:true},frameIndex:4,frameTotal:8}));
assert.equal(note(),'Refreshing · frame 4 of 8');
// The warm publication withholds the four it will not fetch.
assert.equal(show(payload({frames:range(4,8),loop:4,frameIndex:4,frameTotal:4})),'');
assert.deepEqual(radarView.loaded.map(f=>f.stamp),['4','5','6','7'],'undecoded frames outside the target were held');
assert.equal(radarFrameGoal(),4);
''')


def test_page_decode_progress_still_reads_as_refreshing_when_the_engine_is_idle():
    """The server holds all eight; the page is still compositing. That is a real
    refresh in progress even though the engine itself is idle."""
    run_page(r'''
const r=payload({frames:range(0,8),loop:8,frameIndex:8,frameTotal:8});
seed(r);
radarView.loaded.slice(0,4).forEach(f=>{f.bitmap.close();f.bitmap=null;f.ready=false;});
assert.equal(note(),'Refreshing · frame 4 of 8');
''')


def test_a_rise_before_its_pass_starts_changes_nothing():
    """The tier rose but the engine has not yet started the live pass: the wire
    still says four, so the page stays quiet rather than promising eight."""
    run_page(r'''
const r=payload({frames:range(4,8),loop:4,frameIndex:4,frameTotal:4});
seed(r);
assert.equal(show({...r,attention:{tier:'live',frames:8,tiles:true}}),'');
''')
