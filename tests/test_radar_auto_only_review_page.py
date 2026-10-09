"""Auto-only review fixes, page side, running production console_live.html code in
the Node harness (no browser, no network): the page build handshake, failure
status during an automatic handoff, the clear loop's time, the live region that
announces problems, and the loop caption with real 12- and 24-hour labels."""
import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.test_radar_buffer_page import run_page
from tests.test_radar_review_oct_page import HM, POLLED

HTML = Path('design/almanac/console_live.html').read_text()
HANDSHAKE = HTML[HTML.index('  /* Page build handshake'):HTML.index('  function poll(viewStart)')]
POLL = HTML[HTML.index('  function poll(viewStart)'):HTML.index('  /* Paint the no-data')]


def node(script):
    result = subprocess.run(['node'], input=script, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr + result.stdout


# A page "load": the handshake block evaluated with this page's stamped build, over
# a sessionStorage that, like the browser's, survives the reload.
LOADER = r'''
const assert=require('node:assert/strict');
let now=1e12,reloads=0,screen='s-radar',activated=[],storageBroken=false;
const store=new Map();
const sessionStorage={getItem:k=>{if(storageBroken)throw Error('denied');return store.has(k)?store.get(k):null},
  setItem:(k,v)=>{if(storageBroken)throw Error('denied');store.set(k,String(v))},removeItem:k=>{if(storageBroken)throw Error('denied');store.delete(k)}};
Date.now=()=>now;
const radarGesture={state:'idle'};
function $(id){return /^s-[a-z]+$/.test(id)?{id}:null}
function activate(id){activated.push(id);screen=id}
function load(stamp){
  const document={querySelector:s=>s==='meta[name="almanac-build"]'?(stamp===undefined?null:{content:stamp}):{dataset:{screen}}};
  const location={reload(){reloads++}};
  eval(HANDSHAKE_SRC);
  return {buildReload,buildResumeScreen,PAGE_BUILD};
}
'''


def handshake(body):
    node(LOADER.replace("eval(HANDSHAKE_SRC)", 'eval(' + json.dumps(HANDSHAKE) + ')') + body)


# --- P1: the page build handshake --------------------------------------------------------

def test_a_matching_build_never_reloads_and_clears_the_guard():
    handshake(r'''
const page=load('aaaa');
assert.equal(page.PAGE_BUILD,'aaaa');
store.set('almanacBuildReload',JSON.stringify({target:'aaaa',tries:1,at:now}));
assert.equal(page.buildReload('aaaa'),false);assert.equal(reloads,0);
assert.ok(!store.has('almanacBuildReload'),'the guard outlived its arrival');
''')


def test_an_old_page_reloads_once_into_the_served_build_and_keeps_its_screen():
    handshake(r'''
let page=load('old');
assert.equal(page.buildReload('new'),true);assert.equal(reloads,1);
assert.deepEqual(JSON.parse(store.get('almanacBuildReload')),{target:'new',tries:1,at:now});
assert.equal(store.get('almanacBuildScreen'),'s-radar');
// The reload lands on the new build: the open screen is restored once, the guard cleared.
screen='s-obs';page=load('new');page.buildResumeScreen();
assert.deepEqual(activated,['s-radar']);assert.ok(!store.has('almanacBuildScreen'));
assert.equal(page.buildReload('new'),false);assert.ok(!store.has('almanacBuildReload'));
page.buildResumeScreen();assert.deepEqual(activated,['s-radar'],'a later load resumed again');
''')


def test_a_reload_that_misses_its_build_backs_off_and_never_spins():
    handshake(r'''
let page=load('old');
assert.equal(page.buildReload('new'),true);
// A cache hands the old page back: no second reload inside a minute...
page=load('old');
for(let i=0;i<30;i++){now+=1000;assert.equal(page.buildReload('new'),false);}
assert.equal(reloads,1);
now+=30e3;assert.equal(page.buildReload('new'),true);assert.equal(reloads,2);
// ...then two minutes, four, eight..., capped at an hour.
const waits=[];
for(let n=0;n<10;n++){const from=now;page=load('old');while(!page.buildReload('new'))now+=1000;waits.push(Math.round((now-from)/1000));}
assert.deepEqual(waits,[120,240,480,960,1920,3600,3600,3600,3600,3600]);
// A newer deploy is a new target: immediate.
assert.equal(load('old').buildReload('newer'),true);
''')


@pytest.mark.parametrize('case', ['no header', 'unstamped page', 'no meta', 'gesture', 'storage'])
def test_no_reload_when_a_reload_could_not_help_or_is_unsafe(case):
    handshake(r'''
const CASE=%s;
const page=load(CASE==='unstamped page'?'':CASE==='no meta'?undefined:'old');
if(CASE==='gesture')radarGesture.state='pinch';
if(CASE==='storage')storageBroken=true;   // no working guard, no reload at all
assert.equal(page.buildReload(CASE==='no header'?null:'new'),false);
assert.equal(reloads,0);
''' % json.dumps(case))


def test_the_poll_reloads_before_rendering_a_newer_build():
    node(r'''
const assert=require('node:assert/strict');
let presenceDirty=false,pollTimer=null,pollController=null,polling=false,pollStart=0,FETCH_MS=4000,failCount=0,pollGen=0,reportRender=false,
    lastRenderMs=0,recvAgeSec=0,recvPerf=0,radarServerClock=null,rendered=[],reloads=0,served='new';
const store=new Map(),sessionStorage={getItem:k=>store.has(k)?store.get(k):null,setItem:(k,v)=>store.set(k,String(v)),removeItem:k=>store.delete(k)};
const document={hidden:false,querySelector:s=>s==='meta[name="almanac-build"]'?{content:'old'}:{dataset:{screen:'s-radar'}}};
const location={reload(){reloads++}},$=()=>({classList:{contains:()=>true}}),activate=()=>{};
const schedulePoll=()=>{},updateFreshness=()=>{},clamp=(v,a,b)=>Math.max(a,Math.min(b,v)),isNum=v=>typeof v==='number'&&isFinite(v);
const validPayload=d=>!!d,render=d=>rendered.push(d),radarIdleSync=()=>{},radarFollowIntent=()=>{},radarTrace=()=>{};
const radarGesture={state:'idle'},radarIntent={generation:0,ready:false,owned:false,owner:null,session:'s',heartbeat:0},radarZoom={auto:true},radarSmooth={pending:null,value:null},radarBaseStyle={theme:'night'};
let radarCamera=null;
const fetch=()=>Promise.resolve({ok:true,headers:{get:k=>({'X-Almanac-Build':served,Date:new Date().toUTCString()})[k]??null},json:()=>Promise.resolve({ts:1})});
const settle=()=>new Promise(r=>setImmediate(r));
eval(HANDSHAKE_TEXT);
POLL
(async()=>{
  poll();for(let i=0;i<5;i++)await settle();
  assert.equal(reloads,1);assert.equal(rendered.length,0,'an old page rendered a newer payload');
  served='old';
  poll();for(let i=0;i<5;i++)await settle();
  assert.equal(reloads,1);assert.equal(rendered.length,1);
  assert.ok(!store.has('almanacBuildReload'),'arrival did not clear the guard');
})().catch(e=>{console.error(e);process.exit(1)});
'''.replace('HANDSHAKE_TEXT', json.dumps(HANDSHAKE)).replace('POLL', POLL))


# --- P2: a failed automatic handoff speaks while the old picture stays ----------------------

def test_a_failed_handoff_shows_couldnt_refresh_over_the_retained_picture():
    """The reviewer's reproduction: fresh Region imagery, then a site candidate
    whose acquisition failed, before enough candidate frames decode."""
    run_page(POLLED + r'''
const candidate=manifest('b');candidate.refresh={state:'failed',frameTotal:8};
renderRadar({radar:candidate,ts:100900});
assert.ok(radarView.pendingSource,'the candidate is staged');
assert.equal(radarView.data.sourceId,'a','the old picture stays');
assert.equal(radarView.data.refresh.state,'idle');assert.equal(radarView.refresh.state,'failed');
assert.equal(status(),"Couldn't refresh",'staging spoke at once');
radarState();assert.equal(status(),"Couldn't refresh");assert.equal($('rad-status').dataset.state,'stale');
// The candidate recovers: the status goes quiet again.
const ok=manifest('b');renderRadar({radar:ok,ts:100902});radarState();
assert.equal(status(),'');
''')


def test_freshness_stays_with_the_displayed_picture_during_a_handoff():
    run_page(POLLED + r'''
radarView.receivedAge=700;radarView.receivedAt=clock;   // the picture on screen is 11+ min old
const candidate=manifest('b');candidate.ageSec=0;        // the candidate is brand new
renderRadar({radar:candidate,ts:100900});
assert.ok(radarView.pendingSource);
assert.match(status(),/^Stale · 1[12] min old$/,'the candidate\'s age stood in for the picture\'s');
''')


# --- P3 and the date fixture: the loop caption with real labels ---------------------------

def caption_case(style, day, weather, paused):
    newest = '11:55 PM' if style == '12' else '23:55'
    expected = ('Paused · ' if paused else '') + ('Thu 8 Oct, ' if day == 'previous' else '') + newest + \
        (' · No echoes above 15 dBZ' if weather == 'clear' else ' · newest')
    clock = {('12', 'previous'): '12:05 AM', ('24', 'previous'): '00:05',
             ('12', 'same'): '11:57 PM', ('24', 'same'): '23:57'}[style, day]
    return expected, clock


@pytest.mark.parametrize('style', ['12', '24'])
@pytest.mark.parametrize('day', ['previous', 'same'])
@pytest.mark.parametrize('weather', ['clear', 'echoes'])
@pytest.mark.parametrize('paused', [False, True])
def test_the_loop_caption_carries_the_frame_time_with_real_labels(style, day, weather, paused):
    expected, station_clock = caption_case(style, day, weather, paused)
    run_page(HM + POLLED + r'''
const STYLE=%s,DAY=%s,WEATHER=%s,PAUSED=%s,EXPECTED=%s,CLOCK=%s;
const r=manifest(),obs=r.observedTs;r.ageSec=60;r.staleSec=3600;
// The emitter's own station-clock labels: the newest scan is 23:55 / 11:55 PM.
function label(ts){const m=(23*60+55+Math.round((ts-obs)/60)+1440)%%1440,h=Math.floor(m/60),mm=String(m%%60).padStart(2,'0');
  return STYLE==='12'?(h%%12||12)+':'+mm+' '+(h<12?'AM':'PM'):String(h).padStart(2,'0')+':'+mm;}
r.tiles.frames.forEach(f=>f.at=label(f.ts));
const ts=obs+(DAY==='previous'?600:120);
serve({radar:r,ts,time:CLOCK,date:'Fri, 09 Oct 2026'},ts*1000);
radarView.loaded.forEach(f=>{f.at=label(f.ts);if(WEATHER==='clear')f.hasEcho=false;});
radarView.paused=PAUSED;radarLoopSync();
const read=loopRead();
assert.ok(read===EXPECTED||read===EXPECTED+' · partial coverage',JSON.stringify(read)+' != '+JSON.stringify(EXPECTED));
assert.equal(status(),'','a current radar left text in the status');
''' % tuple(json.dumps(v) for v in (style, day, weather, paused, expected, station_clock)))


def test_a_clear_loop_names_the_shown_frames_time_not_the_newest():
    run_page(HM + POLLED + r'''
const r=manifest(),obs=r.observedTs;r.ageSec=60;r.staleSec=3600;
serve({radar:r,ts:obs+60,time:'14:01',date:'Fri, 09 Oct 2026'},(obs+60)*1000);
radarView.loaded.forEach((f,i)=>{f.at='13:'+String(46+i*2).padStart(2,'0');f.hasEcho=false;});
radarView.paused=true;radarView.current=radarView.loaded[5];radarLoopSync();
assert.match(loopRead(),/^Paused · 13:56 · No echoes above 15 dBZ( · partial coverage)?$/);
''')


# --- should-fix: the live region ---------------------------------------------------------

def test_the_announcement_region_is_established_at_page_load():
    head = HTML[HTML.index('<main class="screen sc" id="s-radar">'):]
    head = head[:head.index('</div>')]
    assert re.search(r'<span class="sc-status" id="rad-status"></span>', head), 'the visible status must not be a live region'
    say = re.search(r'<span ([^>]*)id="rad-status-say"([^>]*)></span>', head)
    assert say, 'the live region is not static markup'
    attrs = say[1] + say[2]
    for attr in ('class="sr-only"', 'role="status"', 'aria-live="polite"', 'aria-atomic="true"'):
        assert attr in attrs, attr
    assert '.sr-only {' in HTML
    assert "setAttribute('aria-live'" not in HTML, 'a live region is switched on at the moment it speaks'


def test_what_the_live_region_says():
    run_page(POLLED + r'''
const say=$('rad-status-say'),heard=[];let text='';
Object.defineProperty(say,'textContent',{get:()=>text,set:v=>{text=v;heard.push(v)}});
const attrs=[];$('rad-status').setAttribute=(k,v)=>attrs.push(k);
const r=manifest();r.ageSec=30;r.staleSec=600;
let wallMs=100930e3;serve({radar:structuredClone(r),ts:100900},wallMs);
assert.deepEqual(heard.filter(Boolean),[],'a current radar was announced');
// A frozen payload goes stale: one announcement, while the visible count ticks on.
const shown=new Set();
for(let t=0;t<600;t++){clock+=2000;wallMs+=2000;serve({radar:structuredClone(r),ts:100900},wallMs);shown.add(status());}
assert.ok([...shown].filter(s=>s.startsWith('Stale')).length>=10,'the visible age stopped ticking');
assert.deepEqual(heard.filter(Boolean),['Radar: Stale · 10 min old']);
// Fresh data: silence (the region empties), then the same problem again is heard again.
const fresh=structuredClone(r);fresh.observedTs+=120;fresh.ageSec=30;
serve({radar:fresh,ts:wallMs/1000},wallMs);
assert.equal(status(),'');assert.equal(text,'');
for(let t=0;t<330;t++){clock+=2000;wallMs+=2000;serve({radar:structuredClone(fresh),ts:wallMs/1000-660-t*2},wallMs);}
radarStartingRender({available:false,reason:'no data yet',starting:{phase:'acquire'}});
const said=heard.filter(Boolean);
assert.equal(said.length,3,said.join(' | '));
assert.match(said[1],/^Radar: Stale · 1\d min old$/);assert.equal(said[2],'Radar: Starting');
assert.deepEqual(attrs,[],'the visible status was made a live region');
''')
