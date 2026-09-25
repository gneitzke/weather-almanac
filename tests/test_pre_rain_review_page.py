"""Execute production poll/visibility paths with no browser or network."""
import subprocess
from pathlib import Path


def test_all_poll_paths_explicitly_report_visibility_and_order():
    html = Path('design/almanac/console_live.html').read_text()
    poll = html[html.index('  var presenceDirty = false;'):html.index('  /* Paint the no-data')]
    activate = html[html.index('  function activate(id)'):html.index('\n  document.querySelectorAll(".tab[data-screen]")')]
    visibility = next(line for line in html.splitlines() if "document.addEventListener('visibilitychange'" in line)
    script = r'''
const assert=require('node:assert/strict'), listeners={},urls=[];
globalThis.setTimeout=()=>1;globalThis.clearTimeout=()=>{};
let active=false;
const screen={id:'s-radar',classList:{contains:()=>active,toggle:(key,on)=>active=on}};
const $=id=>id==='s-radar'?screen:{style:{}};
const document={hidden:false,addEventListener:(name,fn)=>listeners[name]=fn,querySelector:()=>({dataset:{}}),querySelectorAll:selector=>selector==='.screen'?[screen]:[]};
const radarView={active:false},radarIntent={session:'page-session-123456',generation:0,heartbeat:0,ready:false,owned:false,owner:null},radarGesture={state:'idle'},radarSource={fastUntil:0};
const radarSmooth={pending:null},radarBaseStyle={theme:'paper'};
const radarGestureCancel=()=>{},radarRelease=()=>{},radarActivate=()=>{},radarIdleSync=()=>{},radarLoopSync=()=>{};
const updateFreshness=()=>{},isNum=Number.isFinite,clamp=(v,lo,hi)=>Math.max(lo,Math.min(v,hi));
let radarCamera=null;
const fetch=url=>{urls.push(new URL(url,'http://example.invalid/').searchParams);return new Promise(()=>{});};
POLL
ACTIVATE
VISIBILITY
const last=()=>urls.at(-1);
poll();assert.equal(last().get('view'),'none','initial observations screen');
activate('s-radar');assert.equal(last().get('view'),'radar','entry before first camera exists');
radarCamera={lat:47,lon:-122,zoom:8};reportRender=true;
listeners.pointerdown();poll(true);
assert.equal(last().get('view'),'radar');assert.equal(last().get('r'),'1');assert.equal(last().get('touch'),'1');
poll(true);assert.equal(last().get('view'),'radar','smooth/camera immediate poll');assert.equal(last().has('touch'),false);
document.hidden=true;listeners.visibilitychange();assert.equal(last().get('view'),'none');
document.hidden=false;listeners.visibilitychange();assert.equal(last().get('view'),'radar');
activate('s-obs');assert.equal(last().get('view'),'none','exit reported immediately');
assert.ok(urls.every((q,i)=>q.get('viewSession')===radarIntent.session&&Number(q.get('viewSeq'))===i+1));
'''.replace('POLL', poll).replace('ACTIVATE', activate).replace('VISIBILITY', visibility)
    result = subprocess.run(['node'], input=script, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
