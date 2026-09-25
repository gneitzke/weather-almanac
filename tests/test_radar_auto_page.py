"""Production source controls and camera posts run in Node without networking."""
import json
import re
import subprocess
from pathlib import Path

import pytest
from tests.test_radar_buffer_page import run_page


def controls(body):
    # Restore the production renderer after the common raster harness stubs it.
    html = Path('design/almanac/console_live.html').read_text()
    source = html[html.index('  function radarSourceRender()'):html.index('  function render(data)')]
    run_page('radarSourceRender=function'+source.strip().removeprefix('function radarSourceRender')+r''';
for(const id of ['rad-src-auto','rad-src-mosaic','rad-src-site']){
 const b=$(id);b.attrs={};b.setAttribute=(k,v)=>b.attrs[k]=v;
}
const cap=$('rad-src-cap');cap.parts=[];cap.replaceChildren=(...v)=>cap.parts=v;
cap.append=v=>cap.parts.push(typeof v==='string'?v:v.textContent);
cap.scrollWidth=0;cap.clientWidth=1000;
const caption=()=>cap.parts.join('');
radarSwitch=null;radarPendingRetry=()=>null;radarView.current=null;
Object.assign(radarView.data,{sourceId:'iem-mrms-lcref',sourcePref:'auto',sourceMode:'mosaic',
 sources:[{mode:'mosaic',available:true},{mode:'site',siteId:'KATX',available:true}],
 nexrad:{id:'KATX',name:'KATX'},sites:[]});
'''+body)


@pytest.mark.parametrize('mode', ['mosaic', 'site'])
def test_auto_pressed_independently_of_drawn_source_and_caption(mode):
    controls(r'''
radarView.data.sourceMode=MODE;
if(MODE==='site')Object.assign(radarView.data,{sourceId:'iem-nexrad-n0b',siteId:'KATX',native:true,sites:[{id:'KATX',contributing:true}]});
radarSourceRender();
assert.equal($('rad-src-auto').attrs['aria-pressed'],'true');
assert.equal($('rad-src-mosaic').attrs['aria-pressed'],'false');
assert.equal($('rad-src-site').attrs['aria-pressed'],'false');
assert.match(caption(),MODE==='site'?/^Auto · KATX radar/:/^Auto · Region/);
assert.equal($('rad-src-site').textContent,'KATX');
'''.replace('MODE', json.dumps(mode)))


@pytest.mark.parametrize('mode', ['mosaic', 'site'])
def test_manual_picker_and_caption(mode):
    controls(r'''
radarView.data.sourcePref=radarView.data.sourceMode=MODE;
radarSourceRender();
assert.equal($('rad-src-auto').attrs['aria-pressed'],'false');
assert.equal($('rad-src-'+MODE).attrs['aria-pressed'],'true');
assert.ok(!caption().startsWith('Auto · '));
'''.replace('MODE', json.dumps(mode)))


def test_auto_switching_copy_without_a_local_user_operation():
    controls(r'''
radarView.refresh={state:'newest',targetMode:'site'};radarSourceRender();
assert.match(caption(),/^Switching to KATX radar · showing Region/);
radarSwitchStart();radarSourceRender();
assert.match(caption(),/^Switching to KATX radar · showing Region/);
assert.equal($('rad-src-auto').attrs['aria-pressed'],'true');
''')


def test_auto_click_posts_auto_not_an_effective_source():
    run_page(r'''
let posted=[];radarPostIntent=()=>posted.push(radarIntent.preferredMode);
radarView.data.sourcePref='site';radarChooseSource('auto');
assert.deepEqual(posted,['auto']);assert.equal(radarSource.desired,'auto');
assert.equal(radarCamera.zoom,8);
radarChooseSource('auto');assert.deepEqual(posted,['auto']);
''')


def test_auto_ack_can_complete_on_either_effective_source():
    run_page(r'''
radarSource.desired='auto';const r=manifest();r.sourcePref='auto';
r.intent={session:radarIntent.session,generation:radarIntent.generation};
renderRadar({radar:r,ts:100900});
assert.equal(radarSource.desired,null);
''')


def test_budget_pause_caption_and_effective_smoothing():
    controls(r'''
radarView.data.native=false;
radarView.data.nativeBudget={ceilingState:'paused',bytesToday:250000001};
radarSourceRender();assert.match(caption(),/v2 paused · daily data limit/);
assert.equal(radarNativeActive(),false);
radarView.data.native=true;assert.equal(radarNativeActive(),true);
''')


def test_same_tokens_and_44px_target_for_auto():
    html = Path('design/almanac/console_live.html').read_text()
    assert re.search(r'class="rad-seg" id="rad-src-auto"[^>]*aria-pressed="true"', html)
    assert re.search(r'\.rad-seg \{[^}]*height:44px', html)
    assert '.rad-seg[aria-pressed="true"] { color:var(--ink)' in html


@pytest.mark.parametrize('state', ['gesturing', 'inertia'])
def test_poll_never_commits_source_mid_gesture_then_sends_auto_on_settle(state):
    html = Path('design/almanac/console_live.html').read_text()
    poll = html[html.index('  function poll(viewStart)'):html.index('  /* Paint the no-data')]
    script = r'''
const assert=require('node:assert/strict');
let presenceDirty=false,pollTimer=null,pollController=null,polling=false,pollStart=0,
 FETCH_MS=4000,failCount=0,pollGen=0,reportRender=false;
const schedulePoll=()=>{},updateFreshness=()=>{};
const $=()=>({classList:{contains:()=>true}}),document={hidden:false};
const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
const radarIntent={generation:1,ready:true,owned:true,owner:null,session:'auto-session-12345',heartbeat:0,preferredMode:'auto',sourceDirty:true},
 radarGesture={state:STATE},radarZoom={auto:false},radarSmooth={pending:null},radarBaseStyle={theme:'paper'};
let radarCamera={lat:47,lon:-122,zoom:8},urls=[];
// An unresolved thenable records the production request synchronously, without I/O.
const fetch=url=>{urls.push(url);const chain={then:()=>chain,catch:()=>chain};return chain};
POLL
poll();
assert.ok(!urls[0].includes('radarSource='));assert.ok(!urls[0].includes('radarCommit='));
assert.ok(urls[0].includes('radarMoving=1'));
polling=false;radarGesture.state='idle';poll();
assert.ok(urls[1].includes('radarSource=auto'));assert.ok(urls[1].includes('radarCommit=1'));
process.exit(0);
'''.replace('STATE', json.dumps(state)).replace('POLL', poll)
    result = subprocess.run(['node'], input=script, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
