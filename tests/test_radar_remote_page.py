"""Two production page scripts, isolated DOMs, one in-memory server; no network."""
import json
import subprocess
from pathlib import Path

import pytest


def run_remote(body):
    html = Path('design/almanac/console_live.html').read_text()
    radar = html[html.index('  /* V4:'):html.index('  function render(data)')]
    poll = html[html.index('  var presenceDirty = false;'):html.index('  /* Paint the no-data')]
    progress = html[html.index('  setInterval(()=>{if(radarView.active)'):html.index('  setInterval(updateFreshness')]
    setup = r'''
const assert=require('node:assert/strict'),vm=require('node:vm');
const server={owner:'',epoch:0,high:new Map(),generation:0,heartbeat:0,ownerIdle:0,throttled:false,seq:1,smooth:'off',public:false,hold:false,waiting:[],requests:[],
 intent:{seq:1,zoom:8,zoomPolicy:'auto',source:'auto',center:{lat:47,lon:-122}},
 handle(q,panel=false){
   const session=q.get('radarSession'),generation=Number(q.get('radarGeneration')),heartbeat=Number(q.get('radarHeartbeat'));
   const commit=q.get('radarCommit')==='1',claim=q.get('radarClaim'),own=session===this.owner;
   const high=this.high.get(session)||{generation:0,heartbeat:0};
   if(!this.public&&!this.throttled&&commit&&generation>high.generation&&heartbeat>high.heartbeat&&(own||claim===this.owner&&q.get('radarClaimEpoch')===String(this.epoch))){
     if(!own)this.epoch++;
     this.high.set(session,{generation,heartbeat});
     this.owner=session;this.generation=generation;this.heartbeat=heartbeat;
     const center=q.get('radarGeoCenter').split(',').map(Number);
     this.intent={seq:++this.seq,session,generation,epoch:this.epoch,acceptedAt:1000,zoom:Number(q.get('radarGeoZoom')),center:{lat:center[0],lon:center[1]},zoomPolicy:q.get('radarPolicy'),source:q.get('radarSource')||this.intent.source};
   }
   if(!this.public&&!this.throttled){if(q.has('radarSmooth'))this.smooth=q.get('radarSmooth');}
   const headers=this.public?{}:{'X-Radar-Intent':JSON.stringify({session:this.owner,epoch:this.epoch,generation:this.generation,ownerIdleSec:this.ownerIdle,intent:this.intent}),
     'X-Radar-Smooth':this.smooth,'X-View-Session':'','X-Radar-Panel':panel?'1':'0','X-Radar-Throttled':this.throttled?'1':null};
   return {ok:true,headers:{get:k=>headers[k]??null},json:()=>Promise.resolve({ts:1000000,radar:{...manifest(),intent:structuredClone(this.intent),sourcePref:this.intent.source}})};
 },
 fetch(url,panel){const q=new URL(url,'http://offline.invalid/').searchParams;this.requests.push(q);
   if(this.hold){this.hold=false;return new Promise(resolve=>this.waiting.push(()=>resolve(this.handle(q,panel))));}
   return Promise.resolve(this.handle(q,panel));
 }};
function manifest(){return {available:true,center:{lat:47,lon:-122},zoomMin:4,zoomMax:10,zoomAutoLevel:8,zoomDesired:8,zoomAuto:true,
 sourceId:'iem-mrms-lcref',sourceMode:'mosaic',sourcePref:'auto',refresh:{state:'idle'},frameCount:0,
 sources:[{mode:'site',available:true,siteId:'KATX'}],nexrad:{id:'KATX',name:'Camano'},tiles:{frames:[]}};}
function page(panel=false){
 const context=vm.createContext({require,server,manifest,assert,URL,structuredClone,panel});
 vm.runInContext(`
 const timers=new Map(),delays=new Map(),intervals=[],nodes=new Map(),events={};let ordinal=0,now=1000000;
 const performance={now:()=>now},Date={now:()=>now,parse:()=>NaN};
 const setTimeout=(fn,delay)=>{timers.set(++ordinal,fn);delays.set(ordinal,delay);return ordinal},clearTimeout=id=>timers.delete(id),setInterval=fn=>intervals.push(fn);
 function $(id){if(!nodes.has(id))nodes.set(id,{hidden:false,disabled:false,style:{},dataset:{},textContent:'',children:[],listeners:{},
 classList:{contains:()=>true},addEventListener(k,fn){this.listeners[k]=fn},setAttribute(){},getAttribute(){},removeAttribute(){},
 replaceChildren(...items){this.children=items;this.textContent=items.join('')},append(item){this.children.push(item)},
 hasPointerCapture:()=>false});return nodes.get(id)}
 const document={hidden:false,documentElement:{dataset:{}},querySelector:$,addEventListener(k,fn){(events[k]??=[]).push(fn)},createTextNode:s=>s,createElement:()=>$('element')};
 const window={crypto:require('node:crypto').webcrypto,matchMedia:()=>({matches:true,addEventListener(){}})};
 const OffscreenCanvas=class {constructor(w,h){this.width=w;this.height=h}getContext(){return {clearRect(){},drawImage(){}}}};
 const MutationObserver=class {observe(){}};
 const sessionStorage={getItem:()=>null,setItem(){},removeItem(){}};
 const clamp=(v,a,b)=>Math.max(a,Math.min(b,v)),isNum=v=>typeof v==='number'&&Number.isFinite(v);
 const requestAnimationFrame=()=>1,cancelAnimationFrame=()=>{},activate=()=>{},updateFreshness=()=>{};
 const fetch=url=>server.fetch(url,panel);
 ` + RADAR + POLL + PROGRESS + `
 radarWake=radarGeoRequest=radarQueueTiles=radarOverlayBuild=radarOverlayPaint=radarMemory=()=>{};
 radarBaseStyle.theme='paper';radarView.active=true;radarView.data=manifest();radarView.refresh={state:'idle'};
 radarCamera={lat:47,lon:-122,zoom:8};radarIntent.cameraKey=JSON.stringify(radarCamera);radarIntent.policy='auto';
 function render(d){radarView.data=d.radar;radarSourceRender();radarZoomRender();radarNoteRender();}
 function caption(){radarSourceRender();return $('rad-src-cap').textContent}
 `,context);
 return {run:code=>vm.runInContext(code,context),async poll(){vm.runInContext('poll(true)',context);await new Promise(setImmediate);vm.runInContext('assert.equal(failCount,0)',context)}};
}
(async()=>{BODY})().catch(e=>{console.error(e);process.exitCode=1});
'''.replace('RADAR', json.dumps(radar)).replace('POLL', json.dumps(poll)).replace('PROGRESS', json.dumps(progress)).replace('BODY', body)
    result = subprocess.run(['node'], input=setup, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_two_pages_user_handoff_delayed_commit_and_reload():
    run_remote(r'''
const a=page(),b=page();await a.poll();await b.poll();
assert.equal(server.owner,'');assert.ok(server.requests.every(q=>!q.has('radarClaim')&&!q.has('radarCommit')));
a.run('radarZoomChange(1)');await a.poll();
const A=a.run('radarIntent.session');assert.equal(server.owner,A);assert.equal(server.intent.zoom,9);
await b.poll();b.run("assert.equal(radarCamera.zoom,9);assert.equal(radarZoom.auto,false);assert.equal(radarIntent.owned,false);assert.equal(radarIntent.ready,false);assert.doesNotMatch(caption(),/Updating view|Switching/)");
// An old owner's higher-generation request is still in flight at the handoff.
a.run('radarZoomChange(-1)');server.hold=true;await a.poll();
b.run('radarZoomChange(-1)');await b.poll();
const B=b.run('radarIntent.session');assert.equal(server.owner,B);assert.equal(server.intent.zoom,8);
server.waiting.shift()();await new Promise(setImmediate);
assert.equal(server.owner,B);assert.equal(server.intent.zoom,8);
a.run("assert.equal(radarIntent.owned,false);assert.equal(radarIntent.ready,false);assert.equal(radarIntent.sourceDirty,false);assert.equal(radarCamera.zoom,8);assert.doesNotMatch(caption(),/Updating view|Switching/)");
// Ordinary polling, including reloads, never steals or invents a commit.
const reload=page();const mark=server.requests.length;
for(let i=0;i<4;i++){await a.poll();await b.poll();await reload.poll();}
assert.equal(server.owner,B);assert.ok(server.requests.slice(mark).every(q=>!q.has('radarClaim')&&!q.has('radarCommit')));
reload.run("assert.equal(radarCamera.zoom,8);assert.equal(radarIntent.owned,false);assert.equal(radarIntent.generation,0);assert.doesNotMatch(caption(),/Updating view|Switching/)");
reload.run('radarZoomChange(-1)');await reload.poll();assert.equal(server.owner,reload.run('radarIntent.session'));assert.equal(server.intent.zoom,7);
await a.poll();await b.poll();a.run('assert.equal(radarCamera.zoom,7)');b.run('assert.equal(radarCamera.zoom,7);assert.equal(radarIntent.owned,false)');
assert.ok(server.requests.filter(q=>q.has('radarClaim')).every(q=>q.get('radarCommit')==='1'&&Number(q.get('radarGeneration'))>0));
''')


@pytest.mark.parametrize('action', [
    "radarChooseSource('site')",
    "radarBegin();radarCameraSet({lat:47.05,lon:-122.05,zoom:8});radarSettle()",
    'radarZoomChange(0)',
])
def test_nonowner_source_pan_and_auto_zoom_claim_only_on_commit(action):
    run_remote(r'''
const a=page(),b=page();await a.poll();a.run('radarZoomChange(1)');await a.poll();await b.poll();
b.run(ACTION);await b.poll();assert.equal(server.owner,b.run('radarIntent.session'));
await a.poll();a.run('assert.equal(radarIntent.owned,false);assert.equal(radarIntent.ready,false)');
assert.equal(a.run('JSON.stringify(radarCamera)'),b.run('JSON.stringify(radarCamera)'));
assert.equal(a.run('radarZoom.auto'),b.run('radarZoom.auto'));
assert.equal(a.run('radarIntent.preferredMode'),server.intent.source);
a.run('assert.doesNotMatch(caption(),/Updating view|Switching/)');
'''.replace('ACTION', json.dumps(action)))


def test_nonowner_smooth_taps_do_not_claim_and_follow_headers():
    run_remote(r'''
const a=page(),b=page();await a.poll();a.run('radarZoomChange(1)');await a.poll();await b.poll();
const owner=server.owner,mark=server.requests.length;
b.run("$('rad-smooth').listeners.click()");
await new Promise(setImmediate);await b.poll();await a.poll();
assert.equal(server.smooth,'on');assert.equal(server.owner,owner);
assert.ok(server.requests.slice(mark).every(q=>!q.has('radarClaim')&&!q.has('radarCommit')));
a.run("assert.equal(radarSmooth.value,true)");
server.public=true;const viewer=page();await viewer.poll();
viewer.run("assert.equal(radarSmooth.writable,false);radarZoomChange(1);radarChooseSource('site');assert.equal(radarIntent.ready,false);assert.doesNotMatch(caption(),/Updating view|Switching/)");
''')


def test_follower_auto_switch_reports_source_without_claiming_camera():
    run_remote(r'''
const p=page();await p.poll();
p.run("intervals[0]();radarView.refresh={state:'newest',targetMode:'site'};assert.match(caption(),/^Switching to Camano radar · showing Region/);radarView.pendingSource={data:{sourceMode:'site'},frames:[]};assert.match(caption(),/^Switching to Camano radar · showing Region/)");
await p.poll();assert.ok(server.requests.every(q=>!q.has('radarClaim')&&!q.has('radarCommit')));
''')


def test_activation_and_follower_idle_timer_do_not_claim():
    run_remote(r'''
const p=page();await p.poll();
p.run("radarSiteTable=[{id:'KATX'}];radarPreload=()=>{};radarActivate();assert.equal(radarIntent.ready,false);radarGesture.idleTimer=setTimeout(radarRecenter,90000)");
await p.poll();p.run('assert.equal(radarGesture.idleTimer,null);assert.equal(radarIntent.generation,0)');
assert.ok(server.requests.every(q=>!q.has('radarClaim')&&!q.has('radarCommit')));
''')


def test_noop_zoom_does_not_leave_an_update_caption():
    run_remote(r'''
const p=page();await p.poll();
p.run('radarZoomChange(1)');await p.poll();p.run('radarZoomChange(1)');await p.poll();
p.run("radarSwitch=null;radarZoomChange(1);assert.equal(radarIntent.ready,false);assert.equal(radarSwitch,null);assert.doesNotMatch(caption(),/Updating view|Switching/)");
''')


def test_rejected_older_request_does_not_consume_a_new_user_action():
    run_remote(r'''
const a=page(),b=page();await a.poll();a.run('radarZoomChange(1)');await a.poll();await b.poll();
a.run('radarZoomChange(-1)');server.hold=true;await a.poll();
b.run('radarZoomChange(-1)');await b.poll();
// A starts a newer gesture before its earlier rejected request returns.
a.run("radarBegin();radarCameraSet({lat:47.01,lon:-122,zoom:8})");
server.waiting.shift()();await new Promise(setImmediate);
a.run("assert.equal(radarIntent.owned,false);assert.equal(radarGesture.state,'gesturing');radarSettle()");
await a.poll();assert.equal(server.owner,a.run('radarIntent.session'));assert.equal(server.intent.center.lat,47.01);
''')
