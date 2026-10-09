"""Auto-only radar, page side: no source picker, a status that speaks only about
problems, and the loop caption as the time readout. Production console_live.html
functions run in the Node harness; no browser, no network."""
import re
import subprocess
from pathlib import Path

from tests.test_radar_auto_page import controls
from tests.test_radar_buffer_page import run_page
from tests.test_radar_review_oct_page import POLLED

HTML = Path('design/almanac/console_live.html').read_text()


def test_no_source_buttons_are_rendered():
    start = HTML.index('<main class="screen sc" id="s-radar">')
    markup = HTML[start:HTML.index('</main>', start)]
    assert 'rad-seg' not in markup and 'id="rad-src"' not in markup
    assert not re.search(r'>\s*(Auto|Region|No site)\s*</button>', markup)
    assert 'radarChooseSource' not in HTML and not re.search(r'radarSource(?![A-Za-z])', HTML)
    assert '.rad-seg' not in HTML and '.rad-src {' not in HTML and '.rad-src[' not in HTML
    # The production caption renderer neither creates nor touches a picker.
    controls(r'''
for(const mode of ['mosaic','site']){
  radarView.data.sourceMode=mode;
  if(mode==='site')Object.assign(radarView.data,{sourceId:'iem-nexrad-n0b',siteId:'KATX',sites:[{id:'KATX',contributing:true}]});
  radarSourceRender();
}
for(const id of ['rad-src','rad-src-auto','rad-src-mosaic','rad-src-site'])assert.ok(!nodes.has(id),id+' was rendered');
assert.match(caption(),/^KATX radar/);
''')


def test_the_poll_never_carries_a_source():
    poll = HTML[HTML.index('  function poll(viewStart)'):HTML.index('  /* Paint the no-data')]
    assert not re.search(r'radarSource(?![A-Za-z])', poll)
    script = r'''
const assert=require('node:assert/strict');
let presenceDirty=false,pollTimer=null,pollController=null,polling=false,pollStart=0,FETCH_MS=4000,failCount=0,pollGen=0,reportRender=false;
const schedulePoll=()=>{},updateFreshness=()=>{},$=()=>({classList:{contains:()=>true}}),document={hidden:false},clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
const radarIntent={generation:3,ready:true,owned:true,owner:null,session:'auto-only-session',heartbeat:0},radarGesture={state:'idle'},radarZoom={auto:true},radarSmooth={pending:null},radarBaseStyle={theme:'night'};
let radarCamera={lat:47,lon:-122,zoom:8},urls=[];
const fetch=url=>{urls.push(url);const chain={then:()=>chain,catch:()=>chain};return chain};
POLL
poll();
assert.ok(urls[0].includes('radarCommit=1&radarPolicy=auto'));
assert.ok(!urls[0].includes('radarSource'));
process.exit(0);
'''.replace('POLL', poll)
    result = subprocess.run(['node'], input=script, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_status_is_empty_when_current_and_names_a_stale_radar():
    run_page(POLLED + r'''
const r=manifest();r.ageSec=30;r.staleSec=600;
const d={radar:r,ts:100900};let wallMs=100930e3;
serve(structuredClone(d),wallMs);
assert.equal(status(),'');assert.equal($('rad-status').dataset.state,'current');
for(let t=0;t<300;t++){clock+=2000;wallMs+=2000;serve(structuredClone(d),wallMs);}
assert.match(status(),/^Stale · \d+ min old$/);
assert.equal($('rad-status').dataset.state,'stale','the stale accent colour keys off data-state');
const minutes=+status().match(/(\d+) min/)[1];
assert.ok(minutes>=10,'the age is the data age, not a frame offset');
''')


def test_the_caption_names_the_mosaic_without_a_mode():
    controls(r'''
radarSourceRender();assert.match(caption(),/^Region · new image every \d+ min · IEM \/ NOAA/);
radarView.data.sourceId='rainviewer';radarView.data.cadenceSec=600;
radarSourceRender();assert.match(caption(),/^Worldwide blend · new image every 10 min · RainViewer/);
assert.doesNotMatch(caption(),/Auto/);
''')


# The announcement region and the loop caption's date (with real 12- and 24-hour
# labels) are pinned in test_radar_auto_only_review_page.py.
