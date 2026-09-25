"""Production page logic in Node's deterministic DOM harness; no browser verifier."""
from pathlib import Path
import subprocess
from tests.test_radar_buffer_page import run_page


def page_function(name):
    html = Path('design/almanac/console_live.html').read_text()
    start = html.index('  function '+name+'(')
    end = html.index('  function ', start+12)
    return html[start:end]


def test_quiet_cold_radar_tab_survives_without_boot_state():
    run_page(r'''
radarView.everAvailable=false;
renderRadar({radar:{available:false,reason:'no data yet',starting:null,
 attention:{tier:'rest',waiting:true,tiles:false,waking:false}},ts:100900});
assert.equal(document.querySelector('.tab[data-screen="s-radar"]').hidden,false);
assert.match($('rad-src-cap').textContent,/Radar resting/);
renderRadar({radar:{available:false,reason:'no data yet',starting:null,
 attention:{tier:'live',waiting:true,tiles:true,waking:true}},ts:100902});
assert.match($('rad-src-cap').textContent,/Waking radar .* first scan/);
''')


def test_waking_note_without_frame_and_during_partial_fresh_publication():
    run_page('radarNoteRender=function'+page_function('radarNoteRender').strip().removeprefix('function radarNoteRender')+r''';
radarIntent.postedAt=0;radarPendingRetry=()=>null;radarSource.refused=false;
radarView.data.attention={waking:true};radarView.data.stale=false;
radarView.current=null;radarNoteRender();
assert.match($('rad-note').textContent,/Waking radar .* fetching the first scan/);
assert.ok(!$('rad-note').textContent.includes('showing'));
radarView.current=decode(frame(0));radarNoteRender();
assert.match($('rad-note').textContent,/Waking radar .* showing/);
radarPendingRetry=()=>({reason:'local network',when:'1:20'});radarNoteRender();
assert.match($('rad-note').textContent,/Retrying .* local network/);
''')


def test_retained_old_image_is_stale_even_when_newest_metadata_is_fresh():
    run_page(r'''
radarView.data.observedTs=101000;radarView.data.staleSec=600;
radarView.data.tiles.frames=[{ts:101000}];radarView.holdingWindow=true;
radarView.receivedAge=30;radarView.receivedAt=clock;
radarView.current=decode(frame(0));radarState();
assert.equal($('rad-plate').dataset.state,'stale');
''')


def test_normal_history_loop_does_not_flash_stale_on_its_older_frames():
    run_page(r'''
radarView.data.staleSec=600;radarView.receivedAge=30;radarView.receivedAt=clock;
radarView.holdingWindow=false;radarView.current=radarView.loaded[0];radarState();
assert.equal($('rad-plate').dataset.state,'current');
''')


def test_weather_aria_label_includes_tier_and_clears_weather():
    run_page(r'''
const tab=document.querySelector('.tab[data-screen="s-radar"]');
let label;tab.setAttribute=(name,value)=>{if(name==='aria-label')label=value;};
const r=manifest();r.attention={tier:'watch',weather:true};
renderRadar({radar:r,ts:100900});assert.match(label,/watch .* weather nearby/);
r.attention={tier:'rest',weather:false};renderRadar({radar:r,ts:100902});
assert.equal(label,'Radar: rest');assert.equal(tab.dataset.weather,'off');
''')


def test_failed_poll_retries_the_human_touch_signal():
    html = Path('design/almanac/console_live.html').read_text()
    poll = html[html.index('  function poll(viewStart)'):html.index('  /* Paint the no-data')]
    script = r'''
const assert=require('node:assert/strict');
let presenceDirty=true,pollTimer=null,pollController=null,polling=false,pollStart=0,
    FETCH_MS=4000,failCount=0,pollGen=0,reportRender=false;
const schedulePoll=()=>{},updateFreshness=()=>{};
const $=()=>({classList:{contains:()=>false}}),document={hidden:false};
const radarIntent={generation:0,ready:false,owned:false,owner:null},radarGesture={state:'idle'},
    radarSmooth={pending:null},radarBaseStyle={theme:'paper'};
let radarCamera=null,urls=[];
const fetch=url=>{urls.push(url);return Promise.reject(Error('offline'));};
POLL
(async()=>{
  poll();assert.equal(presenceDirty,false);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(presenceDirty,true,'failed poll discarded human presence');
  poll();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(urls.length,2);assert.ok(urls.every(url=>url.includes('&touch=1')));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''.replace('POLL', poll)
    result = subprocess.run(['node'], input=script, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
