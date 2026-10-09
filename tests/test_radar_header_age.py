"""The radar header speaks only when the data is stale, and its age measures the
data, not the frame the loop is playing."""
import json
import subprocess
from pathlib import Path

import pytest

HTML = Path('design/almanac/console_live.html').read_text()


def radar_state_source():
    # radarState and the age helpers declared just above it.
    start = HTML.index('  function radarReceivedAge(')
    return HTML[start:HTML.index('\n  function radarActivate(', start)]


# Current data leaves the header empty (2026-10-09); what the age measures is unchanged.
@pytest.mark.parametrize('frame_offset,received_age,retained,expect_old', [
    (-2700, 30, False, ''),                     # a 45-minute-old loop frame of a fresh loop
    (0, 30, False, ''),                         # the newest frame, fresh
    (0, 780, False, ''),                        # 13 min old, under the 15-minute staleSec
    (0, 960, False, 'Stale · 16 min old'),      # the newest frame itself is 16 min old
    (-900, 30, True, 'Stale · 15 min old'),     # a retained frame from an abandoned window, 15.5 min old
])
def test_header_age_follows_the_data(frame_offset, received_age, retained, expect_old):
    script = """
const nodes={};
function el(id){return nodes[id]||(nodes[id]={id,dataset:{},text:'',setAttribute(){},replaceChildren(...c){this.text=c.map(n=>n.text||'').join('')},append(...c){this.text+=c.map(n=>n.text||'').join('')},set textContent(v){this.text=v},get textContent(){return this.text}});}
const $=el;const document={createTextNode:t=>({text:t}),createElement:()=>({text:'',set textContent(v){this.text=v}})};
const performance={now:()=>1000};const isNum=v=>typeof v==='number'&&Number.isFinite(v);
function radarFrameLabel(f){return 'T'+f.ts}
const args=%s;
const newest=100000,f={ts:newest+args.offset};
var radarView={data:{staleSec:900,observedTs:newest,tiles:{frames:args.retained?[]:[{ts:f.ts}]},refresh:null},current:f,receivedAge:args.received,receivedAt:1000,holdingWindow:false,clear:false};
%s
radarState();console.log(JSON.stringify(el('rad-status').text));
""" % (json.dumps(dict(offset=frame_offset, received=received_age, retained=retained)), radar_state_source())
    out = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == expect_old, out
