"""Fast state/identity checks; real canvas and clock oracle: verify_radar_v46."""
import json
import subprocess
from pathlib import Path

import pytest

HTML = Path('design/almanac/console_live.html').read_text()


def function(name):
    start = HTML.index('  function '+name+'(')
    line = HTML[start:HTML.index('\n', start)]
    return line if line.rstrip().endswith('}') else HTML[start:HTML.index('\n  }', start)+4]


def run(body):
    script = '\n'.join(function(name) for name in (
        'radarWindowKey', 'radarFrameKey', 'radarPruneFrames', 'radarReady', 'radarPlayback',
        'radarUpdateReady', 'radarCouldLoop', 'radarPreload', 'isNum', 'radarListed')) + '''
let reduced=false;const radarReduced=()=>reduced,document={hidden:false},radarGesture={state:'idle'};
const radarQueueTiles=()=>{},radarWake=()=>{};let radarEchoDirty=false,radarCompositeJob=null;
const radarView={active:true,paused:false,started:false,cycle:[],retired:[],loaded:[],data:{sourceId:'a',tiles:{revision:'r1'},frameCount:8}};
const frame=i=>({stamp:String(i),ts:i,sourceId:'a',revision:'r1',siteScans:[],bitmap:{closed:0,close(){this.closed++}}});
''' + body
    result = subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize('count', range(9))
def test_four_decoded_scans_start_and_sparse_inventory_counts(count):
    result = run('''
radarView.loaded=Array.from({length:8},(_,i)=>({...frame(i),bitmap:i<COUNT?{}:null,hasEcho:true,ready:true}));
radarView.good=radarView.loaded[7];radarUpdateReady();
console.log(JSON.stringify({count:radarReady().length,loop:radarCouldLoop()}));
'''.replace('COUNT', str(count)))
    assert result == dict(count=count, loop=count >= 4)


def test_ready_is_sorted_decoded_inventory_and_cycle_is_frozen():
    result = run('''
const a=frame(1),b=frame(2),c=frame(3);b.bitmap=null;
radarView.loaded=[c,b,a];radarView.cycle=[a,c];radarView.started=true;
radarUpdateReady();const before=radarReady().map(f=>f.ts);b.bitmap={};radarUpdateReady();
console.log(JSON.stringify({before,after:radarReady().map(f=>f.ts),cycle:radarPlayback().map(f=>f.ts)}));
''')
    assert result == dict(before=[1, 3], after=[1, 2, 3], cycle=[1, 3])


def test_stamp_advance_retains_identity_and_does_not_cut_or_close_blend():
    result = run('''
const a=frame(-3600),b=frame(2),old=a.bitmap,key=radarFrameKey(b);
radarView.loaded=[a,b];radarView.current=a;radarView.good=b;radarView.started=true;
radarView.cycle=[a,b];radarView.blend={from:a,to:b,start:123};radarView.nextAt=456;
radarPreload({sourceId:'a',tiles:{revision:'r1',frames:[{stamp:'2',ts:2},{stamp:'3',ts:3}]}});
const staged={current:radarView.current.ts,deadline:radarView.nextAt,blend:radarView.blend.start,closed:old.closed,retained:radarView.loaded[0]===b,key:radarFrameKey(b)===key,dirty:radarEchoDirty};
radarView.data.tiles.revision='r2';staged.pinned=radarFrameKey(b)===key;
radarView.cycle=radarView.loaded;radarView.current=b;radarPruneFrames();staged.blendRetained=old.closed===0;
radarView.blend=null;radarPruneFrames();staged.closedAfterBlend=old.closed;
console.log(JSON.stringify(staged));
''')
    assert result == dict(current=-3600, deadline=456, blend=123, closed=0, retained=True,
                          key=True, dirty=False, pinned=True, blendRetained=True, closedAfterBlend=1)
