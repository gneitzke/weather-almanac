"""Run production Smooth events and ARIA state in the offline Node harness."""
from pathlib import Path

import pytest
from tests.test_radar_remote_page import run_remote


@pytest.mark.parametrize('theme', ['paper', 'night'])
def test_native_and_iem_press_states_and_ownership(theme):
    run_remote(r'''
const a=page();await a.poll();
a.run(`$('rad-smooth').attrs={};$('rad-smooth').setAttribute=function(k,v){this.attrs[k]=v};radarBaseStyle.theme='THEME';radarView.data={...manifest(),native:true,sourceMode:'site'};
radarSmooth.value=false;radarZoomRender();
assert.equal($('rad-smooth').attrs['aria-disabled'],'false');
assert.equal($('rad-smooth').attrs['aria-pressed'],'false');
$('rad-smooth').listeners.click();
assert.equal($('rad-smooth').attrs['aria-pressed'],'true');`);
await a.poll();assert.equal(server.smooth,'on');
a.run(`radarView.data.native=false;radarZoomRender();
assert.equal($('rad-smooth').attrs['aria-disabled'],'false');
$('rad-smooth').listeners.click();assert.equal($('rad-smooth').attrs['aria-pressed'],'false');`);
await a.poll();assert.equal(server.smooth,'off');
a.run(`radarSmooth.writable=false;radarZoomRender();
assert.equal($('rad-smooth').attrs['aria-disabled'],'true');
$('rad-smooth').listeners.click();assert.equal(radarSmooth.value,false);`);
'''.replace('THEME', theme))


def test_tooltip_and_native_colour_scaling():
    html = Path('design/almanac/console_live.html').read_text()
    assert 'Interpolates reflectivity before colouring native Level III or IEM tiles.' in html
    assert 'nothing to soften' not in html
    assert html.count('smooth:!!r.tiles.smooth,native:!!r.native,remapRevision:') == 2
    assert 'imageSmoothingEnabled=!!f.smooth&&!f.native;' in html
    assert 'imageSmoothingEnabled=!!job.f.smooth&&!job.f.native;' in html


def test_native_smooth_never_uses_canvas_colour_interpolation():
    from tests.test_radar_buffer_page import run_page
    run_page(r'''
const ctx=new OffscreenCanvas(956,490).getContext('2d');
const f=radarView.good;f.smooth=true;f.native=true;
radarEchoPaint(f,ctx);assert.equal(ctx.imageSmoothingEnabled,false);
f.native=false;radarEchoPaint(f,ctx);assert.equal(ctx.imageSmoothingEnabled,true);
f.smooth=false;radarEchoPaint(f,ctx);assert.equal(ctx.imageSmoothingEnabled,false);
''')
