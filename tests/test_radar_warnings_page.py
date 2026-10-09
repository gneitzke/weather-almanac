""" Page side of radar.warnings: the production NWS-WARNINGS block of
console_live.html run under node with a minimal SVG/DOM stand-in.

Covers projection onto the overlay (one cased group per warning),
station-covering emphasis, client-side expiry, stale handling, tap and keyboard
details, the per-viewer Warning areas toggle (persistence, storage that throws,
the shown/hidden count, no toggle without NWS coverage), and theme tokens with
contrast in both themes. The UX pass (priority, tag, card, small targets,
failed refresh) is in test_radar_warnings_ux_page.
"""
import json
import re
import subprocess
from pathlib import Path

import pytest

PAGE = Path('design/almanac/console_live.html')


def run(body, storage='memory'):
    html = PAGE.read_text()
    block = html[html.index('  /* NWS-WARNINGS:BEGIN'):html.index('  /* NWS-WARNINGS:END */')]
    world = html[html.index('  function radarWorldPoint('):html.index('  function radarWorldInverse(') + len(html[html.index('  function radarWorldInverse('):].splitlines()[0])]
    script = r'''
const assert=require('node:assert/strict');
let wall=1000000;const timers=new Map();let ordinal=0;
globalThis.setTimeout=(fn,ms)=>{timers.set(++ordinal,{fn,ms});return ordinal};globalThis.clearTimeout=id=>timers.delete(id);
function fire(){const due=[...timers.entries()];timers.clear();for(const [,t] of due)t.fn();}
class Node{constructor(tag){this.tagName=tag;this.attrs={};this.children=[];this.parent=null;this.dataset={};this.hidden=false;this.listeners={};this.textContent='';this.className='';this.style={};
  this.classList={contains:c=>(this.attrs.class||'').split(' ').includes(c)};}
  setAttribute(k,v){this.attrs[k]=String(v)}getAttribute(k){return this.attrs[k]??null}
  _detach(){if(this.parent){const i=this.parent.children.indexOf(this);if(i>=0)this.parent.children.splice(i,1);this.parent=null;
    // a node leaving the document takes keyboard focus with it, as in a browser
    if(document.activeElement===this)document.activeElement=null;}}
  remove(){this._detach()}
  appendChild(c){if(c instanceof Node){c._detach();c.parent=this;}this.children.push(c);return c}
  insertBefore(c,ref){if(ref==null)return this.appendChild(c);c._detach();c.parent=this;this.children.splice(this.children.indexOf(ref),0,c);return c}
  append(...c){for(const x of c)this.appendChild(typeof x==='string'?{textContent:x}:x)}
  replaceChildren(...c){for(const x of [...this.children])x instanceof Node?x._detach():null;this.children=[];this.append(...c)}
  addEventListener(t,fn,cap){(this.listeners[t]=this.listeners[t]||[]).push(fn)}
  focus(){document.activeElement=this;focusCalls++}
  text(){return this.textContent+this.children.map(c=>c.text?c.text():c.textContent).join('')}}
let focusCalls=0;
const nodes=new Map();function $(id){if(!nodes.has(id))nodes.set(id,new Node(id));return nodes.get(id)}
$('rad-warnings').hidden=true;$('rad-warn-card').hidden=true;$('rad-warn-tag').hidden=true;
const document={activeElement:null,createElement:t=>new Node(t),createTextNode:s=>({textContent:s})};
function el(name,attrs,parent){const e=new Node(name);for(const k in attrs)e.setAttribute(k,attrs[k]);if(parent)parent.appendChild(e);return e;}
const clamp=(v,a,b)=>Math.max(a,Math.min(b,v)),isNum=v=>typeof v==='number'&&Number.isFinite(v);
const RAD_MAX_LAT=85.05112878,RAD_CONTROLS='.rad-zoom';
function radarServerNow(){return wall}
function radarPointer(e){return {x:e.x,y:e.y}}
let radarState=()=>{states++};let states=0;
const STORE=@@STORAGE@@;
const localStorage=STORE==='throws'?{getItem(){throw Error('denied')},setItem(){throw Error('denied')}}:
  {m:new Map(STORE==='off'?[['radarWarnings','off']]:[]),getItem(k){return this.m.has(k)?this.m.get(k):null},setItem(k,v){this.m.set(k,String(v))}};
const radarView={active:true,data:{center:{lat:47.61,lon:-122.33},zoomMin:4}};
const radarIntent={writable:true},radarZoom={auto:true};
function radarSwitchStart(){}function radarSourceRender(){}
function radarEase(c){radarCamera=c;radarWarnPlace();}
@@CLAMP@@
let radarCamera={lat:47.61,lon:-122.33,zoom:8};
let radarOverlay={warn:new Node('g'),geo:new Node('g'),chrome:new Node('g'),labels:[]};
@@WORLD@@
@@BLOCK@@
radarWarnWire();
const RING=(cx,cy,r=.06)=>[[cx-r,cy-r],[cx+r,cy-r],[cx+r,cy+r],[cx-r,cy+r],[cx-r,cy-r]];
function item(id,o={}){return Object.assign({id,event:'Tornado Warning',label:'Tornado Warning',kind:'tornado',threat:null,level:'warning',color:'#FF0000',
  onset:wall-60,expires:wall+600,until:'1:45 PM',headline:'Tornado Warning issued October 9 at 1:18PM PDT until 1:45PM PDT by NWS Seattle WA',
  detail:'Radar indicated',affectsStation:false,polygon:[RING(-122.33,47.61)]},o);}
function payload(items,o={}){return Object.assign({available:true,fetchedTs:wall-10,stale:false,items},o);}
const paths=()=>radarOverlay.warn.children;   // one <g> per warning: case, halo, core, rail
const count=()=>$('rad-warnings-count').textContent;
function screen(lon,lat){const c=radarCamera,p=radarWorldPoint(c.lat,c.lon,c.zoom),s=2**(c.zoom-9),q=radarWorldPoint(lat,lon,9);return {x:q[0]*s+478-p[0],y:q[1]*s+245-p[1]}}
function tap(pt){const plate=$('rad-plate');plate.listeners.pointerdown[0]({pointerId:1,target:{closest:()=>null},...pt});plate.listeners.pointerup[0]({pointerId:1,...pt});}
@@BODY@@
'''.replace('@@STORAGE@@', json.dumps(storage)).replace('@@WORLD@@', world).replace('@@BLOCK@@', block).replace('@@BODY@@', body).replace('@@CLAMP@@', html[html.index('  function radarClampCamera('):html.index('  function radarFocal(')])
    result = subprocess.run(['node'], input=script, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_polygons_are_projected_and_drawn_beneath_the_station_marker():
    run(r'''
radarWarnUpdate(payload([item('a',{affectsStation:true,polygon:[RING(-122.33,47.61),RING(-122.0,47.9)]})]));
assert.equal(paths().length,1);
const g=paths()[0],d=g.children[2].attrs.d,q=radarWorldPoint(47.61-.06,-122.33-.06,9);
assert.deepEqual(g.children.map(c=>c.attrs.class),['rad-warn-case','rad-warn-halo','rad-warn-core','rad-warn-rail']);
assert.ok(g.children.every(c=>c.attrs.d===d&&c.attrs['vector-effect']==='non-scaling-stroke'),'every stroke shares the outline');
assert.ok(d.startsWith('M'+q[0].toFixed(1)+','+q[1].toFixed(1)),d);
assert.equal((d.match(/Z/g)||[]).length,2,'both rings of a multipolygon');
assert.equal(g.attrs['data-kind'],'tornado');
''')
    html = PAGE.read_text()
    # the warnings group is created first in #rad-over: beneath rings and the station
    build = html[html.index('  function radarOverlayBuild(){'):]
    assert build.index("warn:el('g'") < build.index("geo:el('g'") < build.index("chrome:el('g'")
    # and it follows the camera exactly like the geography group
    assert "if(o.warn)o.warn.setAttribute('transform','translate('+tx+','+ty+') scale('+s+')')" in html


def test_a_warning_covering_the_station_is_emphasised():
    run(r'''
radarWarnUpdate(payload([item('home',{affectsStation:true}),item('near',{polygon:[RING(-122.0,47.9)]})]));
const byId=Object.fromEntries(paths().map(p=>[p.attrs['aria-label'],p.attrs['data-covers']]));
assert.equal(byId['Tornado Warning until 1:45 PM, covers this station'],'true');
assert.equal(byId['Tornado Warning until 1:45 PM'],'false');
// the most important (first in the payload) is drawn last, on top
assert.equal(paths().at(-1).attrs['data-covers'],'true');
''')
    css = PAGE.read_text()
    assert re.search(r'\.rad-warn\[data-covers="true"\]\s*\{\s*--core:\s*2\.5px', css)
    assert re.search(r'\.rad-warn\[data-kind="tornado"\]\[data-covers="true"\]\s*\{\s*--core:\s*4px', css)


def test_client_side_expiry_removes_the_polygon_without_a_payload():
    run(r'''
radarWarnUpdate(payload([item('soon',{expires:wall+120}),item('later',{expires:wall+900,polygon:[RING(-122.0,47.9)]})]));
assert.equal(paths().length,2);
const t=[...timers.values()].at(-1);assert.ok(t.ms>=120000&&t.ms<121000,String(t.ms));
wall+=121;fire();
assert.deepEqual(paths().map(p=>p.attrs['aria-label']),['Tornado Warning until 1:45 PM']);
wall+=800;fire();assert.equal(paths().length,0);
''')


def test_stale_data_draws_nothing_and_reports_unknown_coverage():
    run(r'''
// Unknown coverage is visible even on first load.
radarWarnUpdate(payload([item('a')],{stale:true}));
assert.equal(paths().length,0);assert.equal(radarWarnProblem(),'Warnings unavailable');
// fresh warning shown, then the feed goes stale: drop it and say so
radarWarnUpdate(payload([item('a')]));assert.equal(paths().length,1);
radarWarnUpdate(payload([item('a')],{stale:true}));
assert.equal(paths().length,0);assert.equal(radarWarnProblem(),'Warnings unavailable');
radarWarnUpdate(payload([item('a')],{stale:true}));assert.equal(radarWarnProblem(),'Warnings unavailable','sticky while stale');
radarWarnUpdate(payload([]));assert.equal(radarWarnProblem(),'','fresh data clears it');
''')
    html = PAGE.read_text()
    assert "r.attention?.waking?'Waking':radarWarnProblem());" in html   # the existing problem-only slot


def test_tap_shows_event_headline_and_until():
    run(r'''
radarWarnUpdate(payload([item('a',{affectsStation:true})]));
tap(screen(-122.33,47.61));
const card=$('rad-warn-card');assert.equal(card.hidden,false);
const text=card.text();
assert.ok(text.includes('Tornado Warning'),text);assert.ok(text.includes('until 1:45 PM'),text);
assert.ok(text.includes('issued October 9'),text);assert.ok(text.includes('At this station'),text);
assert.equal(paths()[0].attrs['data-selected'],'true');
// a tap on open map closes it; a drag is not a tap
tap(screen(-121.0,47.0));assert.equal(card.hidden,true);
const plate=$('rad-plate'),a=screen(-122.33,47.61);
plate.listeners.pointerdown[0]({pointerId:2,target:{closest:()=>null},...a});plate.listeners.pointerup[0]({pointerId:2,x:a.x+30,y:a.y});
assert.equal(card.hidden,true,'a pan selected a warning');
// a second finger makes it a pinch, not a tap
plate.listeners.pointerdown[0]({pointerId:3,target:{closest:()=>null},...a});plate.listeners.pointerdown[0]({pointerId:4,target:{closest:()=>null},...a});
plate.listeners.pointerup[0]({pointerId:4,...a});plate.listeners.pointerup[0]({pointerId:3,...a});
assert.equal(card.hidden,true,'a pinch selected a warning');
''')


def test_keyboard_enter_opens_details_and_escape_closes():
    run(r'''
radarWarnUpdate(payload([item('a')]));
const p=paths()[0];assert.equal(p.attrs.tabindex,'0');assert.equal(p.attrs.role,'button');
const over=$('rad-over');let prevented=false;
over.listeners.keydown[0]({key:'Enter',target:p,preventDefault(){prevented=true}});
assert.ok(prevented);assert.equal($('rad-warn-card').hidden,false);
over.listeners.keydown[0]({key:'Escape',target:p,preventDefault(){}});
assert.equal($('rad-warn-card').hidden,true);
''')


def test_selection_closes_when_the_selected_warning_expires():
    run(r'''
radarWarnUpdate(payload([item('a',{expires:wall+60})]));tap(screen(-122.33,47.61));
assert.equal($('rad-warn-card').hidden,false);wall+=61;fire();assert.equal($('rad-warn-card').hidden,true);
''')


# ----------------------------------------------------------------- toggle
def test_toggle_hides_and_shows_only_the_polygons_and_persists():
    run(r'''
radarWarnUpdate(payload([item('a'),item('b',{polygon:[RING(-122.0,47.9)]})]));
const b=$('rad-warnings');assert.equal(b.hidden,false);assert.equal(b.getAttribute('aria-pressed'),'true');
assert.equal(count(),'Shown · 2');
tap(screen(-122.33,47.61));
b.listeners.click[0]();
assert.equal(paths().length,0);assert.equal(b.getAttribute('aria-pressed'),'false');
assert.equal($('rad-warn-card').hidden,true,'hiding the outlines closes their details');
assert.equal(localStorage.getItem('radarWarnings'),'off');
assert.equal(count(),'Hidden · 2');assert.ok(b.getAttribute('aria-label').endsWith('2 hidden'));assert.equal(b.dataset.hiding,'true');
tap(screen(-122.33,47.61));assert.equal($('rad-warn-card').hidden,true,'hidden outlines cannot be tapped');
b.listeners.click[0]();
assert.equal(paths().length,2);assert.equal(localStorage.getItem('radarWarnings'),'on');assert.equal(count(),'Shown · 2');
''')


def test_a_stored_off_choice_is_restored():
    run(r'''
assert.equal(radarWarn.on,false);
radarWarnUpdate(payload([item('a')]));assert.equal(paths().length,0);
assert.equal(count(),'Hidden · 1');
''', storage='off')


def test_storage_that_throws_defaults_on_and_still_toggles():
    run(r'''
assert.equal(radarWarn.on,true);
radarWarnUpdate(payload([item('a')]));assert.equal(paths().length,1);
$('rad-warnings').listeners.click[0]();assert.equal(paths().length,0);
$('rad-warnings').listeners.click[0]();assert.equal(paths().length,1);
''', storage='throws')


def test_hidden_count_ignores_expired_and_stale_warnings():
    run(r'''
radarWarn.on=false;
radarWarnUpdate(payload([item('a'),item('gone',{expires:wall-1})]));assert.equal(count(),'Hidden · 1');
radarWarnUpdate(payload([item('a')],{stale:true}));assert.equal(count(),'Unavailable');assert.equal($('rad-warnings').dataset.hiding,'false');
assert.equal(radarWarnProblem(),'Warnings unavailable','coverage is independent of area visibility');
''')


def test_no_toggle_without_nws_coverage():
    run(r'''
radarWarnUpdate({available:false,fetchedTs:wall,stale:false,items:[]});
assert.equal($('rad-warnings').hidden,true);
radarWarnUpdate(undefined);assert.equal($('rad-warnings').hidden,true,'an older engine without warnings');
radarWarnUpdate(payload([]));assert.equal($('rad-warnings').hidden,false,'coverage, no warnings: the toggle stays available');
assert.equal(paths().length,0);assert.equal($('rad-warn-card').hidden,true);
''')


def test_toggle_markup_matches_the_rail():
    html = PAGE.read_text()
    button = re.search(r'<button[^>]*id="rad-warnings"[^>]*>', html).group(0)
    assert 'aria-pressed="true"' in button and 'hidden' in button and 'rad-reset' in button
    rail = html[html.index('<div class="rad-zoom" id="rad-zoom"'):html.index('<div id="rad-note"')]
    assert rail.index('id="rad-smooth"') < rail.index('id="rad-warnings"')
    # .rad-reset gives the 44 px target
    assert re.search(r'\.rad-reset \{ margin-left: auto; width: 44px; height: 44px;', html)


# ----------------------------------------------------------------- themes
KINDS = ('tornado', 'extremewind', 'flashflood', 'severe', 'marine', 'snowsquall', 'dust')


def _block(css, selector):
    start = css.index(selector)
    return css[start:css.index('}', start)]


def _lum(hex_colour):
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


@pytest.mark.parametrize('selector', [':root {\n    --paper', ':root[data-theme="night"] {',
                                      ':root:not([data-theme="paper"]):not([data-theme="light"]) {'])
def test_every_theme_defines_every_hazard_colour_with_contrast(selector):
    css = PAGE.read_text()
    block = _block(css, selector)
    paper = re.search(r'--paper:\s*(#[0-9A-Fa-f]{6})', block).group(1)
    for kind in KINDS:
        colour = re.search(rf'--warn-{kind}:\s*(#[0-9A-Fa-f]{{6}})', block)
        assert colour, f'{kind} missing in {selector}'
        a, b = sorted((_lum(colour.group(1)), _lum(paper)), reverse=True)
        assert (a + 0.05) / (b + 0.05) >= 3.0, (kind, selector, colour.group(1))
    for kind in KINDS:
        assert re.search(rf'\[data-kind="{kind}"\]\s*\{{\s*--w:\s*var\(--warn-{kind}\)', css), kind
