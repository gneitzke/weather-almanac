"""October radar review, page side (B1-B4): production console_live.html
functions in the Node DOM harness. No browser, no network."""
import json
import subprocess
from pathlib import Path

from tests.test_radar_buffer_page import run_page

HTML = Path('design/almanac/console_live.html').read_text()


def _function(name):
    start = HTML.index('  function '+name+'(')
    return HTML[start:HTML.index('\n  function ', start+12)]


HM = _function('hm')

# A poll as poll() does it: the response's Date header sets the producer clock,
# then the payload renders. `serve(d, wallMs)` advances both clocks.
POLLED = r'''
function serve(d,ms){radarServerClock={ms,perf:clock};renderRadar(d);radarState();}
function status(){return $('rad-status').textContent??''}
function loopRead(){return $('rad-frame-time').textContent??''}
document.createElement=()=>({dataset:{},style:{},setAttribute(){},addEventListener(){},append(){},textContent:''});
'''


# --- B1 frozen payload ------------------------------------------------------------

def test_identical_payload_served_for_thirty_minutes_goes_stale():
    run_page(POLLED + r'''
const r=manifest();r.ageSec=60;r.staleSec=600;
const d={radar:r,ts:100900};           // written at 100900, newest scan 60 s old then
let wallMs=100960e3;                    // first receipt: the file is already 60 s old
serve(structuredClone(d),wallMs);
assert.equal(radarView.data.stale,false);assert.equal($('rad-plate').dataset.state,'current');
assert.equal(status(),'','current radar must leave the status empty');
// A dead emitter: the server keeps answering 200 with the same bytes every 2 s.
for(let t=0;t<900;t++){clock+=2000;wallMs+=2000;serve(structuredClone(d),wallMs);}
assert.equal(radarView.data.stale,true,'a frozen wx.json stayed current');
assert.equal($('rad-plate').dataset.state,'stale');
assert.equal(status(),'Stale · 32 min old');
assert.equal($('rad-status').dataset.state,'stale');
assert.equal(radarCouldLoop(),false,'a stale loop kept animating');
''')


def test_age_is_monotonic_across_a_coarse_or_stepped_server_clock():
    run_page(POLLED + r'''
const r=manifest();r.ageSec=60;const d={radar:r,ts:100900};
serve(structuredClone(d),100960e3);
clock+=1900;serve(structuredClone(d),100960e3);       // Date has 1 s resolution
const age=()=>radarView.receivedAge+(clock-radarView.receivedAt)/1000;
assert.ok(age()>=121.9,'the same observation got younger');
// A NEW observation may legitimately be younger.
const next=manifest('a',8);next.ageSec=10;clock+=100;serve({radar:next,ts:101000},101010e3);
assert.ok(Math.abs(age()-20)<1e-6);
''')


def test_poll_sets_the_producer_clock_before_rendering():
    poll = HTML[HTML.index('  function poll(viewStart)'):HTML.index('  /* Paint the no-data')]
    script = r'''
const assert=require('node:assert/strict');
let presenceDirty=false,pollTimer=null,pollController=null,polling=false,pollStart=0,FETCH_MS=4000,failCount=0,pollGen=0,reportRender=false,lastRenderMs=0,recvAgeSec=0,recvPerf=0;
let radarServerClock=null,seen=null;const isNum=v=>typeof v==='number'&&Number.isFinite(v);
const performance={now:()=>5000};
const schedulePoll=()=>{},updateFreshness=()=>{},radarIdleSync=()=>{},radarFollowIntent=()=>{},validPayload=d=>!!d&&isNum(d.ts);
const buildReload=()=>false;  // the build handshake has its own tests (test_radar_auto_only_review_page.py)
const $=()=>({classList:{contains:()=>false}}),document={hidden:false};
const radarIntent={generation:0,ready:false,owned:false,owner:null},radarGesture={state:'idle'},radarSmooth={pending:null},radarBaseStyle={theme:'paper'};
let radarCamera=null;
function render(d){seen={...radarServerClock};}
const headers={'Date':'Fri, 09 Oct 2026 12:00:00 GMT'};
const fetch=()=>Promise.resolve({ok:true,headers:{get:k=>headers[k]??null},json:()=>Promise.resolve({ts:1})});
POLL
(async()=>{
  poll();await new Promise(r=>setTimeout(r,10));
  assert.equal(seen.ms,Date.parse(headers.Date));assert.equal(seen.perf,5000);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''.replace('POLL', poll)
    result = subprocess.run(['node'], input=script, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_old_payload_without_producer_clock_still_renders():
    # Before the first poll (or in a staged source) there is no Date header.
    run_page(r'''
radarServerClock=null;const r=manifest();r.ageSec=30;
renderRadar({radar:r,ts:Date.now()/1000-90});
assert.ok(Math.abs(radarView.receivedAge-120)<1e-6);
''')


# --- B2 contributor time display ------------------------------------------------

def test_a_mosaic_goes_stale_by_its_oldest_contributing_scan():
    run_page(POLLED + r'''
const r=manifest(),newest=r.observedTs;
r.observedRange=[newest-480,newest];r.ageSec=840;r.staleSec=600;  // oldest scan 14 min old at write
r.tiles.frames.at(-1).observedRange=[newest-480,newest];
serve({radar:r,ts:newest+360},(newest+360)*1000);
// The newest scan is only 6 min old; the oldest decides, and no range is shown.
assert.equal(status(),'Stale · 14 min old');assert.doesNotMatch(status(),/scans/);
''')


def test_current_data_leaves_the_status_empty_and_failure_speaks():
    run_page(POLLED + r'''
const r=manifest();r.ageSec=20;serve({radar:r,ts:r.observedTs+20},(r.observedTs+20)*1000);
assert.equal(status(),'');assert.doesNotMatch(status(),/As of/);
const f=manifest();f.ageSec=20;f.refresh={state:'failed'};serve({radar:f,ts:f.observedTs+20},(f.observedTs+20)*1000);
assert.equal(status(),"Couldn't refresh");
''')


def test_imagery_from_a_previous_day_carries_its_date_in_the_loop_caption():
    run_page(HM + POLLED + r'''
const r=manifest(),obs=r.observedTs;r.ageSec=1200;r.staleSec=3600;
// 00:10 at the station, and the newest scan is 20 minutes old: yesterday 23:50.
serve({radar:r,ts:obs+1200,time:'12:10 AM',date:'Fri, 09 Oct 2026'},(obs+1200)*1000);
assert.match(loopRead(),/^Thu 8 Oct, .* · newest$/);assert.equal(status(),'');
// Same day: no date.
serve({radar:r,ts:obs+1200,time:'2:30 PM',date:'Fri, 09 Oct 2026'},(obs+1200)*1000);
assert.match(loopRead(),/^[^,]* · newest$/);
// Across a month boundary, and an unparseable date still says it is not today.
serve({radar:r,ts:obs+1200,time:'00:05',date:'Thu, 01 Oct 2026'},(obs+1200)*1000);
assert.match(loopRead(),/^Wed 30 Sep, /);
serve({radar:r,ts:obs+1200,time:'00:05',date:'??'},(obs+1200)*1000);
assert.match(loopRead(),/^Yesterday, /);
''')


# --- B3 coverage --------------------------------------------------------------------

PNG = r'''
function png(texts){
  const chunks=[];const be=n=>[n>>>24,n>>>16&255,n>>>8&255,n&255];
  for(const [k,v] of texts){const data=[...Buffer.from(k+'\0'+v,'latin1')];chunks.push(...be(data.length),...Buffer.from('tEXt'),...data,0,0,0,0);}
  return new Uint8Array([0x89,0x50,0x4e,0x47,13,10,26,10,...chunks,0,0,0,0,...Buffer.from('IEND'),0,0,0,0]);
}
const remap=JSON.stringify({remapped:true,revision:'rv1',unmatchedColors:0,opaqueColors:0,unmatchedPixels:0,opaquePixels:0,ambiguousPixels:0});
'''


def test_png_meta_reads_uncovered_pixels_and_defaults_to_covered():
    # The count now travels with radarMeasuredGrid (adversarial review): both or neither.
    run_page(PNG + r'''
const f={remapRevision:'rv1'},some='0'+'f'.repeat(63);
assert.equal(radarPNGMeta(png([['radarRemap',remap],['radarVisiblePixels','0']]),f).uncoveredPixels,0);
assert.equal(radarPNGMeta(png([['radarRemap',remap],['radarVisiblePixels','0']]),f).measured,null);
assert.equal(radarPNGMeta(png([['radarRemap',remap],['radarVisiblePixels','0'],['radarUncoveredPixels','812'],['radarMeasuredGrid',some]]),f).uncoveredPixels,812);
for(const bad of ['-1','65537','1.5','x',''])
  assert.throws(()=>radarPNGMeta(png([['radarRemap',remap],['radarVisiblePixels','0'],['radarUncoveredPixels',bad],['radarMeasuredGrid',some]]),f),/tile coverage/);
assert.throws(()=>radarPNGMeta(png([['radarRemap',remap],['radarVisiblePixels','0'],['radarUncoveredPixels','812']]),f),/tile coverage/);
''')


def _paint(uncovered, partial='false'):
    return r'''
const f=radarView.loaded.at(-1);f.bitmap.close();delete f.bitmap;f.ready=false;
radarView.data.partialCoverage=PARTIAL;
// UNCOVERED unmeasured cells, centred in every tile but the first: each is on screen.
const gappy=new Uint8Array(256).fill(1);for(let i=0;i<UNCOVERED;i++)gappy[(7+(i>>4))*16+((i&15))]=0;
for(const t of radarTileSet(radarCamera,radarLevel()))radarTiles.set(radarTileKey(f,t.z,t.x,t.y),{bitmap:bitmap(256,256),meta:{opaquePixels:0,unmatchedPixels:0,ambiguousPixels:0},hasEcho:false,measured:t===radarTileSet(radarCamera,radarLevel())[0]||!UNCOVERED?null:gappy,sites:[]});
radarEchoPaint(f);
'''.replace('UNCOVERED', str(uncovered)).replace('PARTIAL', partial)


def test_transparent_tiles_with_uncovered_pixels_are_never_clear():
    run_page(_paint(0) + r'''
assert.equal(f.uncovered,0);assert.equal(radarView.clear,true,'a measured empty view is clear');
''')
    run_page(_paint(17) + r'''
assert.ok(f.uncovered>0);assert.equal(radarView.clear,false,'unmeasured pixels were called clear');
// Re-painting the finished plate (camera unchanged) keeps the verdict.
f.bitmap=bitmap();f.camera={...radarCamera};radarEchoPaint(f);assert.equal(radarView.clear,false);
''')
    run_page(_paint(0, 'true') + r'''
assert.equal(radarView.clear,false,'the engine said coverage was partial');
''')


def test_blend_subject_without_full_coverage_is_not_clear():
    run_page(r'''
const [a,b]=radarView.loaded.slice(-2);for(const f of [a,b]){f.hasEcho=false;f.legend={remapped:true};f.uncovered=0;}
b.uncovered=4;radarView.current=a;radarView.blend={from:a,to:b,start:clock-1000};radarBlendPaint(clock);
assert.equal(radarView.current,b);assert.equal(radarView.clear,false);
''')


def test_no_echo_caption_names_the_floor_and_partial_coverage():
    for setup, expected in (('', '7 · No echoes above 15 dBZ'),
                            ('frames[1].uncovered=9;', '7 · No echoes above 15 dBZ · partial coverage'),
                            ('radarView.data.partialCoverage=true;', '7 · No echoes above 15 dBZ · partial coverage'),
                            ('frames[0].uncovered=undefined;', '7 · No echoes above 15 dBZ · partial coverage')):
        run_page(r'''
const frames=radarView.loaded.slice(-4);for(const f of frames){f.hasEcho=false;f.legend={remapped:true};f.uncovered=0;}
radarView.cycle=frames.slice();radarView.current=frames.at(-1);radarView.data.sites=[];radarView.data.legend={floorDbz:15};
SETUP
radarLoopSync();
assert.equal($('rad-frame-time').textContent,EXPECTED);
'''.replace('SETUP', setup).replace('EXPECTED', json.dumps(expected)))


# --- B4 legend help -----------------------------------------------------------------

def test_legend_help_explains_dbz_floor_and_dry_caveat():
    run_page(r'''
const made=[];document.createElement=tag=>{const n={tag,dataset:{},style:{},attrs:{},listeners:{},children:[],hidden:false,textContent:'',
 setAttribute(k,v){this.attrs[k]=v},addEventListener(k,fn){this.listeners[k]=fn},append(...c){this.children.push(...c)}};made.push(n);return n;};
radarLegendRender=Function('radarView','$','document','return '+LEGEND)(radarView,$,document);
radarView.legendKey=null;radarView.data.legend={id:'v3',floorDbz:15,bands:[]};radarLegendRender();
const unit=made.find(n=>n.textContent==='dBZ'),help=made.find(n=>n.id==='rad-legend-help');
assert.equal(unit.tag,'button');assert.equal(unit.attrs['aria-controls'],'rad-legend-help');assert.equal(unit.attrs['aria-expanded'],'false');
assert.equal(help.hidden,true);
assert.match(help.textContent,/15 dBZ/);assert.match(help.textContent,/does not prove it is dry/);assert.match(help.textContent,/higher is heavier/);
unit.listeners.click();assert.equal(help.hidden,false);assert.equal(unit.attrs['aria-expanded'],'true');
// A legend rebuild (new source) keeps the reader's choice.
made.length=0;radarView.data.legend={id:'v3',floorDbz:15,bands:[],colorId:'x'};radarView.legendKey=null;radarLegendRender();
assert.equal(made.find(n=>n.id==='rad-legend-help').hidden,false);
'''.replace('LEGEND', json.dumps(_function('radarLegendRender').strip())))


def test_legend_help_is_themed_by_tokens_only():
    css = HTML[HTML.index('  button.rad-legend-unit {'):HTML.index('  .rad-legend-help[hidden]')]
    assert '#' not in css and 'rgb' not in css
    assert 'var(--ink)' in css and 'var(--rule)' in css
