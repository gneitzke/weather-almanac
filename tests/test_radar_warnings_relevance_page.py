"""View relevance against the production warning JavaScript under Node."""
import pytest

from tests.test_radar_warnings_page import run
from tests.test_radar_warnings_ux_page import browser, _open  # noqa: F401


@pytest.mark.parametrize('zoom', [6, 8, 10])
@pytest.mark.parametrize('on', [True, False])
def test_quiet_day_national_warnings_leave_no_cue_or_count(zoom, on):
    run(r'''
radarCamera.zoom=ZOOM;radarWarn.on=ON;
const distant=Array.from({length:13},(_,n)=>item('east'+n,{
  kind:'flood',label:'Flash Flood Warning',polygon:[RING(-85+n*.1,37)]}));
radarWarnUpdate(payload(distant));
assert.equal(radarWarnLive().length,13,'national data stays available');
assert.equal(paths().length,ON?13:0,'outline retention is unchanged');
assert.equal($('rad-warn-list').hidden,true);
assert.equal($('rad-warn-tag').hidden,true);
assert.equal($('rad-warnings').hidden,false);
assert.equal(count(),'');assert.equal($('rad-warnings-count').hidden,true);
assert.equal($('rad-warnings').getAttribute('aria-label'),'Warning areas');
assert.equal($('rad-warnings').dataset.hiding,'false');
assert.deepEqual(radarWarnListItems(radarWarnLive()),[]);
'''.replace('ZOOM', str(zoom)).replace('ON', str(on).lower()))


def test_nearby_offscreen_warning_and_list_exclude_distant_hazards():
    run(r'''
radarCamera.zoom=10;
const far=item('far',{threat:'emergency',polygon:[RING(-85,37)]});
const near=item('near',{kind:'severe',label:'Severe Thunderstorm Warning',polygon:[RING(-122.33,46.5)]});
radarWarnUpdate(payload([far,near]));
assert.equal(count(),'Off map · 1');
assert.equal($('rad-warn-list').hidden,false);
assert.match($('rad-warn-list').textContent,/^Severe Thunderstorm Warning\n.*mi S of station · 1 warning area · list · 1 off map$/);
assert.ok($('rad-warnings').getAttribute('aria-label').endsWith('1 off map'));
$('rad-warn-list').listeners.click[0]();
assert.equal(radarWarn.selected,'near');assert.equal(radarWarn.also.length,0);
radarWarnToggle();assert.equal(count(),'Off map · 1');
assert.equal($('rad-warn-list').hidden,true);
''')


def test_station_coverage_counts_when_camera_is_distant_even_with_areas_hidden():
    run(r'''
radarCamera={lat:37,lon:-85,zoom:10};
const home=item('home',{affectsStation:true});
radarWarnUpdate(payload([home]));
assert.equal(count(),'Off map · 1');assert.equal($('rad-warn-tag').hidden,false);
assert.equal($('rad-warn-tag-name').textContent,'Tornado Warning');
radarWarnToggle();assert.equal(paths().length,0);
assert.equal(count(),'Off map · 1');assert.equal($('rad-warn-tag').hidden,false);
$('rad-warn-tag').listeners.click[0]();assert.equal(radarWarn.selected,'home');
assert.ok($('rad-warn-card').text().includes('At this station'));
''')


def test_camera_pan_and_zoom_reveal_retained_warnings_without_new_payload():
    run(r'''
const east=item('east',{kind:'flood',label:'Flash Flood Warning',polygon:[RING(-85,37)]});
radarWarnUpdate(payload([east]));const node=paths()[0];
assert.equal(count(),'');assert.equal($('rad-warn-list').hidden,true);
radarCamera={lat:37,lon:-85,zoom:4};radarWarnPlace();
assert.equal(count(),'Shown · 1');assert.equal($('rad-warn-list').hidden,false);
assert.ok($('rad-warn-list').textContent.startsWith('Flash Flood Warning'));
assert.equal(paths()[0],node,'camera movement retains the outline');
radarCamera={lat:37,lon:-94,zoom:4};radarWarnPlace();
assert.ok(radarWarnDistance(east,radarCamera).meters>RAD_WARN_NEAR_METERS);
assert.equal(count(),'Shown · 1','on-screen areas count beyond the radius');
radarCamera.zoom=10;radarWarnPlace();
assert.equal(count(),'');assert.equal($('rad-warn-list').hidden,true);
radarCamera={lat:37,lon:-88,zoom:10};radarWarnPlace();
assert.equal(count(),'Off map · 1','radius follows camera rather than station');
''')


def test_radius_uses_nearest_boundary_and_wraps_at_dateline():
    run(r'''
radarCamera={lat:0,lon:0,zoom:10};radarView.data.center={lat:0,lon:0};
const deg=RAD_WARN_NEAR_METERS/6371000*180/Math.PI;
const near=item('near',{polygon:[RING(0,deg+.049,.05)]});
const far=item('far',{polygon:[RING(0,deg+.051,.05)]});
radarWarnUpdate(payload([near,far]));
assert.deepEqual(radarWarnListItems(radarWarnLive()).map(i=>i.id),['near']);
radarView.data.units='km';radarWarnPlace();assert.equal(count(),'Off map · 1');
radarCamera={lat:51.9,lon:179.9,zoom:10};radarView.data.center={lat:51.9,lon:179.9};
radarWarnUpdate(payload([item('wrap',{polygon:[RING(-178.9,51.9,.05)]})]));
assert.equal(count(),'Off map · 1');assert.equal($('rad-warn-list').hidden,false);
''')


@pytest.mark.parametrize('theme', ['paper', 'night'])
def test_quiet_toggle_is_neutral_and_keeps_layout_in_both_themes(browser, theme):
    ctx, page = _open(browser, theme)
    try:
        result = page.evaluate('''()=>{
          activate('s-radar');document.getElementById('rad-zoom').hidden=false;
          radarView.data={center:{lat:47.61,lon:-122.33},zoomMin:4};
          radarCamera={lat:47.61,lon:-122.33,zoom:8};radarOverlayBuild();
          const now=radarServerNow(),b=document.getElementById('rad-warnings');
          const it={id:'east',kind:'flood',label:'Flash Flood Warning',expires:now+900,
            polygon:[[[-85.1,36.9],[-84.9,36.9],[-84.9,37.1],[-85.1,37.1],[-85.1,36.9]]]};
          radarWarnUpdate({available:true,fetchedTs:now,staleAt:now+900,items:[it]});
          const quiet={text:b.innerText,label:b.getAttribute('aria-label'),
            cueHidden:document.getElementById('rad-warn-list').hidden,
            countHidden:document.getElementById('rad-warnings-count').hidden,
            width:b.getBoundingClientRect().width,height:b.getBoundingClientRect().height};
          radarCamera={lat:37,lon:-85,zoom:4};radarOverlayPaint();
          return {quiet,shown:b.innerText,width:b.getBoundingClientRect().width,
            height:b.getBoundingClientRect().height,cueHidden:document.getElementById('rad-warn-list').hidden};
        }''')
        assert result['quiet']['text'].strip().casefold() == 'warning areas'
        assert result['quiet']['label'] == 'Warning areas'
        assert result['quiet']['cueHidden'] and result['quiet']['countHidden']
        assert result['width'] == result['quiet']['width'] == 112
        assert result['height'] == result['quiet']['height'] == 44
        assert 'Shown · 1' in result['shown'] and not result['cueHidden']
    finally:
        ctx.close()
