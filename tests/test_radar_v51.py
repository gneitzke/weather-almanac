"""Closest-site control identity is independent of the drawn caption subject."""
import json
import subprocess

import pytest

from tests.test_radar_v46 import function


def render(dark=False, nearest=True, width=1000, current=False):
    script = '\n'.join(function(n) for n in ('isNum', 'radarPendingRetry', 'radarSourceSubject', 'radarUnfiltered', 'radarSourceRender')) + '''
const nodes=new Map(),$=id=>{if(!nodes.has(id))nodes.set(id,{
  dataset:{},attrs:{},textContent:'',clientWidth:WIDTH,
  setAttribute(k,v){this.attrs[k]=v},removeAttribute(){},
  replaceChildren(s){this.textContent=s},append(s){this.textContent+=typeof s==='string'?s:s.textContent},
  get scrollWidth(){return this.textContent.length}
});return nodes.get(id)};
const document={createTextNode:s=>s,createElement:()=>$('credit')};
const radarNoteRender=()=>{},radarSource={desired:null},radarIntent={postedAt:0},radarSwitch=null;
const radarSiteTable=[{id:'KATX',name:'Camano Island'},{id:'KLGX',name:'Langley Hill'}];
const sites=['KATX','KLGX','KRTX','KOTX'].map(id=>({id,contributing:true}));
if(DARK)Object.assign(sites[0],{contributing:false,reason:'not reporting'});
const radarView={data:{sourceId:'iem-nexrad-n0b',sourceMode:'site',native:true,sourcePref:'site',siteId:DARK?'KLGX':'KATX',
  scanCadenceSec:240,scanMode:null,sources:[{mode:'site',siteId:'KLGX',available:true}],sites,
  nexrad:NEAREST?{id:'KATX',name:'Fallback',distanceDisp:'39 mi',bearing:'NE'}:null}};
if(CURRENT)radarView.current={drawnSites:[{id:'KLGX'},{id:'KRTX'}]};
radarSourceRender();
console.log(JSON.stringify({button:$('rad-src-site').textContent,
  aria:$('rad-src-site').attrs['aria-label'],caption:$('rad-src-cap').textContent}));
'''
    for key, value in dict(WIDTH=width, DARK=dark, NEAREST=nearest, CURRENT=current).items():
        script = script.replace(key, json.dumps(value))
    return json.loads(subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True).stdout)


@pytest.mark.parametrize('dark', [False, True])
def test_closest_button_and_drawn_caption(dark):
    r = render(dark=dark)
    assert r['button'] == ('KATX +2' if dark else 'KATX +3')
    assert r['aria'] == f"KATX and {2 if dark else 3} nearby: Camano Island radar, high resolution, 39 mi NE"
    assert r['caption'] == (
        'Langley Hill radar, high resolution + 2 nearby · new scan every ~4 min · NOAA Level III · KATX not reporting'
        if dark else 'Camano Island radar, high resolution + 3 nearby · new scan every ~4 min · NOAA Level III')
    assert '39 mi' not in r['caption']


def test_missing_nearest_falls_back_to_drawn_primary():
    r = render(dark=True, nearest=False)
    assert r['button'] == 'KLGX +2'
    assert r['aria'] == 'KLGX and 2 nearby: Langley Hill radar, high resolution'


def test_additional_count_uses_displayed_frame():
    r = render(dark=True, current=True)
    assert r['button'] == 'KATX +1'
    assert r['caption'].startswith('Langley Hill radar, high resolution + 1 nearby')


def test_resolution_yields_before_existing_drop_order():
    full = 'Langley Hill radar + 2 nearby · new scan every ~4 min · NOAA Level III · KATX not reporting'
    short = full.replace('new scan every', 'every')
    bare = short.replace(' + 2 nearby', '')
    for expected in (full, short, bare):
        r = render(dark=True, width=len(expected))
        assert r['caption'] == expected
        assert 'high resolution' in r['aria']


from tests.test_radar_v49 import batch, engine  # noqa: E402,F401
from tests.test_radar_keepalive import origin  # noqa: E402,F401


def test_health_separates_hedge_and_failure_retry_in_same_batch(engine, origin, monkeypatch):
    # No idle warm lease: both failure modes must use their second attempt.
    monkeypatch.setattr(engine._radar_session, "reserve_hedge", lambda url: None)
    def behavior(path, ordinal):
        if ordinal == 1:
            if path.endswith('/0'):
                return 'hang'
            if path.endswith('/1'):
                return 'fail'
        return 'normal'
    origin.behavior = behavior
    result, _ = batch(engine, origin, count=3)
    health = engine._radar_health.snapshot()
    assert len(result) == 3
    assert health['hedges'] == 0 and health['retries'] == 2
    assert health['discardedHedges'] == 0  # no hedge was admitted
    assert len(origin.requests) == len(engine._radar_request_times) == 5
