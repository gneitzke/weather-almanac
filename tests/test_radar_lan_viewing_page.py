"""Page side of LAN viewing: the production poll() and presence listeners.

A LAN page counts as viewing only while it reports view=radar (Radar tab active
AND document visible) and has had input; pointer, keyboard and wheel input set
touch=1 on the next poll. The same page code runs on the panel and on LAN
browsers, so this is the evidence serve.py judges.
"""
import json
import subprocess
from pathlib import Path


def run(body):
    html = Path('design/almanac/console_live.html').read_text()
    presence = html[html.index('  var presenceDirty = false;'):html.index('  var POLL_MS')]
    start = html.index('  function poll(viewStart) {')
    poll = html[start:html.index('\n  }\n', start) + 4]
    script = r'''
const assert=require('node:assert/strict');
const listeners={};const document={hidden:false,addEventListener(type,fn){(listeners[type]=listeners[type]||[]).push(fn)}};
let active=true;const $=id=>({classList:{contains:c=>id==='s-radar'&&c==='active'&&active}});
const urls=[];const fetch=url=>{urls.push(url);return new Promise(()=>{})};
const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
const radarIntent={session:'page-session-0001',viewOwner:null,writable:true,ready:false,owned:false,owner:null,generation:0,heartbeat:0};
const radarGesture={state:'idle'},radarSmooth={pending:null},radarBaseStyle={theme:'paper'},radarZoom={auto:true};
let radarCamera={lat:47,lon:-122,zoom:8};
var POLL_MS=2000,FETCH_MS=4000,failCount=0,polling=false,pollStart=0,pollGen=0,pollController=null,reportRender=false,pollTimer=null;
function schedulePoll(){}function updateFreshness(){}
globalThis.setTimeout=()=>0;globalThis.clearTimeout=()=>{};
@@PRESENCE@@
@@POLL@@
function last(){const q=new URLSearchParams(urls.at(-1).split('?')[1]);polling=false;return q;}
@@BODY@@
'''.replace('@@PRESENCE@@', presence).replace('@@POLL@@', poll).replace('@@BODY@@', body)
    result = subprocess.run(['node'], input=script, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_visible_radar_tab_reports_view_radar_with_its_session():
    run(r'''
poll();let q=last();
assert.equal(q.get('view'),'radar');assert.equal(q.get('viewSession'),'page-session-0001');assert.ok(Number(q.get('viewSeq'))>0);
assert.equal(q.get('touch'),null,'no input yet: no presence');
''')


def test_hidden_document_or_other_tab_reports_view_none():
    run(r'''
document.hidden=true;poll();assert.equal(last().get('view'),'none');
document.hidden=false;active=false;poll();assert.equal(last().get('view'),'none');
active=true;poll();assert.equal(last().get('view'),'radar');
''')


def test_pointer_key_and_wheel_input_each_send_touch_once():
    run(r'''
for(const type of ['pointerdown','keydown','wheel']){
  assert.ok(listeners[type]&&listeners[type].length,type+' is not presence');
  listeners[type].forEach(fn=>fn({}));
  poll();assert.equal(last().get('touch'),'1',type);
  poll();assert.equal(last().get('touch'),null,type+' presence repeated without new input');
}
''')


def test_sequence_increases_so_a_late_report_cannot_win():
    out = run(r'''
poll();const a=Number(last().get('viewSeq'));poll();const b=Number(last().get('viewSeq'));
console.log(JSON.stringify([a,b]));
''')
    a, b = json.loads(out)
    assert b > a
