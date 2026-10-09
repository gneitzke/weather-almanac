"""Final warning-box regressions against production JavaScript under Node."""
import itertools
import json
import re

import pytest

from tests.test_radar_warnings_page import PAGE, run, _block, _lum
from tests.test_radar_warnings_ux_page import browser, _open  # noqa: F401


def state_code():
    html = PAGE.read_text()
    state = html[html.index('  function radarState(){'):html.index('  // Announce a change of problem')]
    status = html[html.index('  function radarStatusSet('):html.index('  function radarActivate(')]
    return state.replace('function radarState()', 'radarState=function()').rstrip() + ';\n' + status


@pytest.mark.parametrize('imagery, expected', [('stale', 'Stale · 16 min old'), ('failed', "Couldn't refresh"), ('waking', 'Waking')])
def test_warning_refresh_failure_remains_visible_alongside_every_radar_status(imagery, expected):
    run(state_code() + r'''
function radarOldest(r){return r.observedTs;}
function radarRange(f,t){return [t,t];}
function radarMin(s){return Math.floor(s/60);}
const mode=MODE;
radarView.data.staleSec=900;radarView.data.observedTs=wall-60;
radarView.data.attention={waking:mode==='waking'};
radarView.receivedAt=performance.now();radarView.receivedAge=mode==='stale'?960:60;
radarView.refresh={state:mode==='failed'?'failed':'idle'};
const it=item('home',{affectsStation:true,expires:wall+7200});
radarWarnUpdate(payload([it],{staleAt:wall+150,refreshFailedAt:wall-1}));radarState();
assert.equal($('rad-status').textContent,EXPECTED);
const health=$('rad-warn-health');assert.equal(health.hidden,false);
assert.ok(health.textContent.includes('Warnings refresh failed'));
assert.ok(health.textContent.includes('Last verified boundaries'));
assert.equal($('rad-warn-tag').dataset.state,'failed');
radarWarnSelect(it);
assert.equal($('rad-warn-card').hidden,false);assert.equal($('rad-warn-card').dataset.state,'failed');
assert.equal(health.hidden,false,'opening the card keeps its freshness qualification');
radarWarnSelect(null);radarWarnToggle();
assert.equal(paths().length,0);assert.equal($('rad-warn-tag').hidden,false);
wall+=151;fire();
assert.equal($('rad-warn-tag').hidden,true,'expired freshness must not assert station coverage');
assert.equal(health.hidden,false);assert.ok(health.textContent.startsWith('Warnings unavailable'));
assert.ok(health.textContent.includes('cannot be verified'));
radarWarnUpdate(payload([]));assert.equal(health.hidden,true,'successful empty coverage clears failure');
'''.replace('MODE', json.dumps(imagery)).replace('EXPECTED', json.dumps(expected)))


def test_empty_first_fetch_failure_is_visible_with_areas_off_and_clears_without_coverage():
    run(r'''
radarWarn.on=false;
radarWarnUpdate({available:true,fetchedTs:null,staleAt:null,stale:true,refreshFailedAt:wall,items:[]});
assert.equal($('rad-warn-health').hidden,false);
assert.ok($('rad-warn-health').textContent.startsWith('Warnings unavailable'));
radarWarnUpdate({available:false,fetchedTs:wall,stale:false,items:[]});
assert.equal($('rad-warn-health').hidden,true);
''')


@pytest.mark.parametrize('zoom', [6, 8, 10])
def test_clipped_or_small_nearby_warning_names_hazard_distance_direction_and_fits(zoom):
    run(r'''
radarCamera={lat:47.61,lon:-122.33,zoom:ZOOM};
radarView.data.units='mi';
const it=item('near',{kind:'severe',label:'Severe Thunderstorm Warning',polygon:[RING(-122.33,47.36,.10)]});
radarWarnUpdate(payload([it]));
if(ZOOM===8){
  assert.equal(paths()[0].attrs['data-small'],'false');
  const b=radarWarnBounds(radarWarn.nodes.get(it.id),radarWarnScreen());
  assert.ok(b.x0>=0&&b.x1<=956&&b.y0>=0&&b.y1<=RAD_WARN_BOTTOM,'fully visible at zoom 8');
}
const cue=$('rad-warn-list');assert.equal(cue.hidden,false);
assert.ok(cue.textContent.startsWith('Severe Thunderstorm Warning\n'));
assert.match(cue.textContent,/\d+ mi S of station/);
cue.listeners.click[0]();assert.equal(radarWarn.selected,it.id);
const fit=$('rad-warn-card').children.find(c=>c.className==='rad-warn-fit');
assert.equal(fit.textContent,'Fit warning area');fit.listeners.click[0]();
assert.equal(radarZoom.auto,false);assert.equal($('rad-warn-card').hidden,true);
const h=radarWarn.nodes.get(it.id),b=radarWarnBounds(h,radarWarnScreen());
assert.ok(b.x0>=500-1e-6&&b.x1<=920+1e-6&&b.y0>=80-1e-6&&b.y1<=300+1e-6,JSON.stringify(b));
assert.ok(radarCamera.zoom>=4&&radarCamera.zoom<=10);
assert.deepEqual(radarClampCamera(radarCamera),radarCamera);
'''.replace('ZOOM', str(zoom)))


def test_nearby_distance_uses_nearest_boundary_station_units_and_dateline_wrap():
    run(r'''
radarView.data.units='km';
const it=item('near',{polygon:[RING(-122.33,47.36,.05)]});
assert.equal(radarWarnLocation(it),'22 km S of station');
radarCamera={lat:51.9,lon:179.9,zoom:6};radarView.data.center={lat:51.9,lon:179.9};
const wrap=item('wrap',{polygon:[RING(-179.7,51.9,.05)]});
assert.match(radarWarnLocation(wrap),/^24 km E of station$/);
const target=radarWarnFitCamera(wrap);assert.ok(target.zoom>=4&&target.zoom<=10);
radarCamera=target;
const b=radarWarnBounds(radarWarnGeometry(wrap,radarView.data.center),radarWarnScreen());
assert.ok(b.x0>=500-1e-6&&b.x1<=920+1e-6,JSON.stringify(b));
''')


def test_fit_obeys_camera_ownership_and_enables_hidden_areas_when_allowed():
    run(r'''
const it=item('home',{affectsStation:true});radarWarn.on=false;
radarWarnUpdate(payload([it]));radarIntent.writable=false;radarWarnSelect(it);
const before={...radarCamera};
assert.equal($('rad-warn-card').children.find(c=>c.className==='rad-warn-fit').disabled,true);
radarWarnFit(it);assert.deepEqual(radarCamera,before);
radarIntent.writable=true;radarWarnFit(it);
assert.equal(radarWarn.on,true);assert.equal(paths().length,1);
assert.equal(localStorage.getItem('radarWarnings'),'on');
''')


def test_overflow_cue_tracks_more_below_and_above_and_all_warning_rows_remain_reachable():
    run(r'''
const all=Array.from({length:15},(_,n)=>item('w'+n,{instruction:'Official instructions. '.repeat(40)}));
radarWarnUpdate(payload(all));radarWarnSelect(all[0],all,true);
const card=$('rad-warn-card'),cue=radarWarn.card.overflow;
card.clientHeight=300;card.scrollHeight=1600;card.scrollTop=0;radarWarnOverflow();
assert.equal(cue.hidden,false);assert.equal(cue.textContent,'Scroll for more ↓ · 14 other warning areas');
assert.ok(card.children[0].children.includes(cue),'cue stays in the sticky header with Close');
card.scrollTop=1300;card.listeners.scroll[0]();
assert.equal(cue.textContent,'↑ Scroll for earlier details · 14 other warning areas');
const rows=card.children.find(c=>c.className==='rad-warn-also').children;
assert.equal(rows.length,15);rows.at(-1).listeners.click[0]();
assert.equal(radarWarn.selected,'w14');
card.scrollHeight=card.clientHeight;radarWarnOverflow();assert.equal(radarWarn.card.overflow.hidden,true);
''')


def test_complete_official_instructions_reach_the_page_from_the_parser():
    from lib import nws_warnings as nw
    from tests.fixtures import nws_warnings as fx
    from tests.test_radar_warnings_remaining import NOW
    from tests.test_radar_warnings_review import parse
    text = 'Official instruction. ' * 40 + 'Final official instruction: avoid flooded roads.'
    f = fx.feature(NOW, ring=fx.OVER_STATION)
    f['properties']['instruction'] = text
    it = nw.public(parse([f], NOW)[0])
    run('const it=' + json.dumps(it) + ';\n' + r'''
wall=NOW;radarWarnUpdate(payload([it]));radarWarnSelect(it);
assert.equal($('rad-warn-card').children.find(c=>c.className==='rad-warn-do').textContent,TEXT);
'''.replace('NOW', str(NOW)).replace('TEXT', json.dumps(text)))


@pytest.mark.parametrize('order', list(itertools.permutations(range(3))))
def test_split_cancellation_clears_station_coverage_through_tracker_and_page(order):
    from lib import nws_warnings as nw
    from tests.test_radar_warnings_remaining import NOW, split_predecessor
    from tests.test_radar_warnings_review import parse
    features = split_predecessor()
    tracker = nw.Tracker()
    tracker.succeeded(NOW - 60, parse(features[:1], NOW - 60), 'fixture')
    before = tracker.payload(NOW - 60)
    tracker.succeeded(NOW, parse([features[k] for k in order], NOW), 'fixture')
    after = tracker.payload(NOW)
    run('const before=' + json.dumps(before) + ';const after=' + json.dumps(after) + ';\n' + r'''
wall=NOW-60;radarWarnUpdate(before,wall);
assert.equal($('rad-warn-tag').hidden,false);
assert.ok($('rad-warn-tag-when').textContent.includes('At this station'));
wall=NOW;radarWarnUpdate(after,wall);
assert.equal(paths().length,1);
assert.equal(radarWarn.paths[0].item.id,CONTINUATION);
assert.equal(paths()[0].attrs['data-covers'],'false');
assert.equal($('rad-warn-tag').hidden,true);
assert.equal($('rad-warnings-count').textContent,'Shown · 1');
assert.equal($('rad-warn-health').hidden,true);
assert.equal(radarWarnProblem(),'');
'''.replace('NOW', str(NOW)).replace('CONTINUATION', json.dumps(features[2]['properties']['id'])))


@pytest.mark.parametrize('theme', ['paper', 'night'])
def test_warning_health_and_scroll_cues_use_opaque_readable_theme_surfaces(theme):
    # DOM/geometry tests run in Node; colours are the same contrast-checked
    # ink/paper tokens as the cards in both production theme definitions.
    html = PAGE.read_text()
    selector=':root {\n    --paper' if theme=='paper' else ':root[data-theme="night"] {'
    tokens=_block(html, selector)
    ink=re.search(r'--ink:\s*(#[0-9A-Fa-f]{6})', tokens).group(1)
    paper=re.search(r'--paper:\s*(#[0-9A-Fa-f]{6})', tokens).group(1)
    a,b=sorted((_lum(ink),_lum(paper)),reverse=True)
    assert (a+.05)/(b+.05)>=4.5
    for cls in ('.rad-warn-health {', '.rad-warn-fit {'):
        rule = html[html.index(cls):].split('}', 1)[0]
        assert 'background:var(--paper)' in rule and 'color:var(--ink)' in rule
    assert '.rad-warn-health:not([hidden]) ~ .rad-warn-tag' in html
    assert '.rad-warn-health:not([hidden]) ~ .rad-warn-tag:not([hidden]) ~ .rad-warn-card' in html
    assert 'position:sticky' in html[html.index('.rad-warn-head {'):].split('}', 1)[0]


@pytest.mark.parametrize('theme', ['paper', 'night'])
def test_browser_failure_dock_and_overflow_cue_do_not_overlap(browser, theme):
    ctx, page = _open(browser, theme)
    try:
        result = page.evaluate('''()=>{
          activate('s-radar');const now=radarServerNow();
          radarView.data={center:{lat:47.61,lon:-122.33},zoomMin:4};radarCamera={lat:47.61,lon:-122.33,zoom:6};radarOverlayBuild();
          const items=Array.from({length:12},(_,n)=>({id:String(n),kind:n?'severe':'tornado',affectsStation:!n,
            label:n?'Severe Thunderstorm Warning':'Tornado Warning',expires:now+900,
            instruction:'Official instruction. '.repeat(40),polygon:[[[-122.4,47.5],[-122.2,47.5],[-122.2,47.7],[-122.4,47.5]]]}));
          radarWarnUpdate({available:true,fetchedTs:now,staleAt:now+900,refreshFailedAt:now-1,items});
          radarWarnSelect(items[1],items,true);
          const card=document.getElementById('rad-warn-card'),tag=document.getElementById('rad-warn-tag'),health=document.getElementById('rad-warn-health');
          const box=n=>{const b=n.getBoundingClientRect();return {top:b.top,bottom:b.bottom}};
          const cue=card.querySelector('.rad-warn-scroll');
          const initial={card:box(card),tag:box(tag),health:box(health),cue:box(cue),hidden:cue.hidden,text:cue.textContent};
          card.scrollTop=card.scrollHeight;radarWarnOverflow();
          return {...initial,scrolledCue:box(cue),scrolledText:cue.textContent};
        }''')
        assert not result['hidden'] and 'Scroll for more' in result['text']
        assert result['card']['bottom'] < result['tag']['top']
        assert result['tag']['bottom'] < result['health']['top']
        for key in ('cue', 'scrolledCue'):
            assert result['card']['top'] <= result[key]['top'] < result[key]['bottom'] <= result['card']['bottom']
        assert 'earlier details' in result['scrolledText']
    finally:
        ctx.close()
