""" Warning layer UX pass, page side (console_live.html).

Under node (the production NWS-WARNINGS block with the DOM stand-in of
test_radar_warnings_page): hazard priority in drawing and hit order, the
emergency treatment, the station tag, the details card, small-geometry and
edge targets, the fixed-footprint toggle, and the failed-refresh state.

In a real Chromium (skipped where Playwright or its browser is missing): the
radar-screen alert strip clears the masthead rule and the plate clears the
footer, a fresh strip is drawn at full strength, and the toggle keeps one
footprint. The page is served from memory through request interception: no
server, no network.
"""
import re
from pathlib import Path

import pytest

from tests.test_radar_warnings_page import run, _block, _lum

PAGE = Path('design/almanac/console_live.html')


# ------------------------------------------------------------------ priority
def test_drawing_order_is_emergency_tornado_station_then_the_rest():
    run(r'''
// the payload's order is deliberately the reverse of hazard priority
radarWarnUpdate(payload([
  item('svr',{kind:'severe',label:'Severe Thunderstorm Warning',polygon:[RING(-121.9,47.9)]}),
  item('home',{kind:'severe',label:'Severe Thunderstorm Warning',affectsStation:true,polygon:[RING(-122.33,47.61,.2)]}),
  item('tor',{polygon:[RING(-122.1,47.5)]}),
  item('emg',{kind:'flashflood',label:'Flash Flood Emergency',threat:'emergency',polygon:[RING(-122.6,47.4)]})]));
assert.deepEqual(paths().map(g=>radarWarn.paths.find(h=>h.node===g).item.id),['svr','home','tor','emg']);
''')


def test_selecting_a_lesser_warning_never_lifts_it_over_a_tornado():
    run(r'''
radarWarnUpdate(payload([item('tor',{affectsStation:true,polygon:[RING(-122.33,47.61,.03)]}),
  item('svr',{kind:'severe',label:'Severe Thunderstorm Warning',affectsStation:true,polygon:[RING(-122.33,47.61,.2)]}),
  item('svr2',{kind:'severe',label:'Severe Thunderstorm Warning',affectsStation:true,polygon:[RING(-122.30,47.65,.2)]})]));
const ids=()=>paths().map(g=>radarWarn.paths.find(h=>h.node===g).item.id);
assert.deepEqual(ids(),['svr2','svr','tor']);
radarWarnSelect(radarWarn.nodes.get('svr2').item);
assert.deepEqual(ids(),['svr','svr2','tor'],'selected: last within its own tier only');
assert.equal(radarWarn.nodes.get('svr2').node.attrs['data-selected'],'true');
// a tap where both overlap reaches the tornado first, the severe one second
const hits=radarWarnHits(screen(-122.33,47.61).x,screen(-122.33,47.61).y).map(i=>i.id);
assert.equal(hits[0],'tor');assert.ok(hits.includes('svr'));
''')


def test_casing_and_emergency_rules_in_the_stylesheet():
    css = PAGE.read_text()
    # three strokes, no fill in any state
    assert '.rad-over .rad-warn path { fill: none;' in css
    assert not re.search(r'\.rad-warn[^{]*\{[^}]*fill-opacity', css)
    assert re.search(r'\.rad-warn-case \{ stroke: var\(--warn-case-dark\);\s*stroke-width: calc\(min\(var\(--core\) \+ var\(--sel\), 5px\) \+ 4px\)', css)
    assert re.search(r'\.rad-warn-halo \{ stroke: var\(--warn-case-light\); stroke-width: calc\(min\(var\(--core\) \+ var\(--sel\), 5px\) \+ 2px\)', css)
    # emergency: the heaviest core and a dark centre rail
    assert '.rad-over .rad-warn[data-threat="emergency"] { --core: 5px; --dash: none; }' in css
    assert '.rad-over .rad-warn[data-threat="emergency"] .rad-warn-rail { display: inline; }' in css
    # the casings are one pair for both themes, defined once
    assert css.count('--warn-case-dark:') == 1 and css.count('--warn-case-light:') == 1
    # failed refresh dims, it does not remove
    assert '#rad-warn-g[data-state="failed"] .rad-warn-core { opacity: .55; }' in css


@pytest.mark.parametrize('selector', [':root {\n    --paper', ':root[data-theme="night"] {',
                                      ':root:not([data-theme="paper"]):not([data-theme="light"]) {'])
def test_emergency_fill_text_contrast_in_every_theme(selector):
    block = _block(PAGE.read_text(), selector)
    ink = re.search(r'--warn-ink:\s*(#[0-9A-Fa-f]{6})', block).group(1)
    for kind in ('tornado', 'flashflood'):          # the two hazards with an emergency variant
        fill = re.search(rf'--warn-{kind}:\s*(#[0-9A-Fa-f]{{6}})', block).group(1)
        a, b = sorted((_lum(ink), _lum(fill)), reverse=True)
        assert (a + 0.05) / (b + 0.05) >= 4.5, (selector, kind, ink, fill)


def test_casings_contrast_with_the_strongest_echo_colours():
    # one of the two casings must reach 3:1 against every reflectivity colour
    from lib import almanac_emit as ae
    dark, light = '#080B0D', '#FFFFFF'
    for _, rgba in ae._RADAR_LUT:
        if len(rgba) > 3 and rgba[3] == 0:
            continue
        colour = '#%02X%02X%02X' % tuple(rgba[:3])
        best = max(((max(_lum(c), _lum(colour)) + .05) / (min(_lum(c), _lum(colour)) + .05)) for c in (dark, light))
        assert best >= 3.0, colour


# ----------------------------------------------------------------- station tag
def test_the_station_tag_names_the_most_dangerous_warning_over_the_station():
    run(r'''
radarWarnUpdate(payload([
  item('svr',{kind:'severe',label:'Severe Thunderstorm Warning',affectsStation:true,ends:wall+3000,until:'2:37 PM'}),
  item('tor',{affectsStation:true,ends:wall+22*60+30,until:'2:19 PM'}),
  item('far',{polygon:[RING(-121.0,47.0)]})]));
const tag=$('rad-warn-tag');
assert.equal(tag.hidden,false);
assert.equal($('rad-warn-tag-name').textContent,'Tornado Warning');
assert.equal($('rad-warn-tag-when').textContent,'At this station · until 2:19 PM · 22 min left');
assert.equal(tag.dataset.kind,'tornado');
// an emergency outranks it
radarWarnUpdate(payload([item('tor',{affectsStation:true,ends:wall+600}),
  item('emg',{label:'Tornado Emergency',threat:'emergency',affectsStation:true,ends:wall+600,until:'1:55 PM'})]));
assert.equal($('rad-warn-tag-name').textContent,'Tornado Emergency');assert.equal(tag.dataset.threat,'emergency');
// the remaining time keeps itself true on the page's clock
wall+=300;fire();assert.ok($('rad-warn-tag-when').textContent.endsWith('5 min left'),$('rad-warn-tag-when').textContent);
wall+=290;fire();assert.ok($('rad-warn-tag-when').textContent.endsWith('under 1 min left'));
''')


def test_the_tag_survives_hidden_areas_but_not_stale_data_and_opens_the_card():
    run(r'''
radarWarnUpdate(payload([item('tor',{affectsStation:true}),item('svr',{kind:'severe',label:'Severe Thunderstorm Warning',affectsStation:true,polygon:[RING(-122.33,47.61,.2)]})]));
const tag=$('rad-warn-tag');
$('rad-warnings').listeners.click[0]();                     // areas hidden
assert.equal(paths().length,0);assert.equal(tag.hidden,false,'the station message is not an area');
tag.listeners.click[0]();
const card=$('rad-warn-card');assert.equal(card.hidden,false);
assert.ok(card.text().startsWith('Tornado Warning'),card.text());
assert.ok(card.text().includes('Also here')&&card.text().includes('Severe Thunderstorm Warning'),card.text());
assert.equal(tag.hidden,true,'the open card for the same warning replaces the tag');
radarWarnSelect(null);assert.equal(tag.hidden,false);
radarWarnUpdate(payload([item('tor',{affectsStation:true})],{stale:true}));
assert.equal(tag.hidden,true,'never from stale data');
''')


def test_no_tag_without_a_warning_over_the_station():
    run(r'''
radarWarnUpdate(payload([item('a',{polygon:[RING(-121.5,47.2)]})]));
assert.equal($('rad-warn-tag').hidden,true);
''')


# ----------------------------------------------------------------------- card
def test_the_card_reads_in_order_with_the_feeds_own_instruction():
    run(r'''
radarWarnUpdate(payload([item('a',{affectsStation:true,until:'2:19 PM',ends:wall+1320,
  instruction:'TAKE COVER NOW! Move to a basement or an interior room on the lowest floor of a sturdy building.'})]));
tap(screen(-122.33,47.61));
const card=$('rad-warn-card'),parts=card.children.map(c=>c.className);
assert.deepEqual(parts,['rad-warn-head','rad-warn-when','rad-warn-fit','rad-warn-detail','rad-warn-do','rad-warn-src']);
const [head,when,fit,detail,act,src]=card.children;
assert.equal(head.children[0].textContent,'Tornado Warning');
assert.equal(head.children[1].tagName,'button');assert.equal(head.children[1].getAttribute('aria-label'),'Close warning details');
assert.equal(when.textContent,'At this station · until 2:19 PM · 22 min left');
assert.equal(detail.textContent,'Radar indicated');
assert.equal(act.textContent,'TAKE COVER NOW! Move to a basement or an interior room on the lowest floor of a sturdy building.');
assert.ok(src.textContent.includes('by NWS Seattle WA'));
// the remaining time ticks in place: no rebuild under a finger on Close
wall+=120;fire();assert.equal(card.children[1],when);assert.equal(when.textContent,'At this station · until 2:19 PM · 20 min left');
head.children[1].listeners.click[0]();assert.equal(card.hidden,true);
''')


def test_no_instruction_in_the_feed_means_no_action_text():
    run(r'''
radarWarnUpdate(payload([item('a',{polygon:[RING(-122.0,47.9)]})]));
radarWarnSelect(radarWarn.nodes.get('a').item);
const card=$('rad-warn-card');
assert.ok(!card.children.some(c=>c.className==='rad-warn-do'));
assert.ok(card.children[1].textContent.startsWith('Not at this station'),card.children[1].textContent);
''')


def test_an_also_here_row_switches_the_card():
    run(r'''
radarWarnUpdate(payload([item('tor',{affectsStation:true}),item('svr',{kind:'severe',label:'Severe Thunderstorm Warning',affectsStation:true,until:'2:37 PM',polygon:[RING(-122.33,47.61,.2)]})]));
tap(screen(-122.33,47.61));
const also=$('rad-warn-card').children.find(c=>c.className==='rad-warn-also'),row=also.children[1];
assert.equal(row.textContent,'Severe Thunderstorm Warning · until 2:37 PM');assert.equal(row.dataset.kind,'severe');
row.listeners.click[0]();
assert.equal(radarWarn.selected,'svr');
const back=$('rad-warn-card').children.find(c=>c.className==='rad-warn-also').children[1];
assert.ok(back.textContent.startsWith('Tornado Warning · until 1:45'),'the other stays one tap away');
''')


# ------------------------------------------------------------ touch targets
def test_a_small_outline_answers_a_56px_target():
    run(r'''
radarCamera={lat:47.61,lon:-122.33,zoom:6};
radarWarnUpdate(payload([item('tiny',{polygon:[RING(-122.33,47.61,.02)]})]));
const c=screen(-122.33,47.61);
assert.ok(radarWarnHits(c.x+26,c.y-26).length===1,'inside the 56 px box');
assert.equal(radarWarnHits(c.x+40,c.y).length,0,'outside it');
''')


def test_a_large_outline_answers_within_12px_of_its_edge():
    run(r'''
radarCamera={lat:47.61,lon:-122.33,zoom:10};
radarWarnUpdate(payload([item('big',{polygon:[RING(-122.33,47.61,.05)]})]));
const e=screen(-122.33+.05,47.61);                      // the east edge
assert.equal(radarWarnHits(e.x+10,e.y).length,1);
assert.equal(radarWarnHits(e.x+16,e.y).length,0);
''')


def test_tornado_names_are_placed_inside_the_unobstructed_map():
    run(r'''
radarWarnUpdate(payload([item('home',{kind:'severe',label:'Severe Thunderstorm Warning',affectsStation:true}),
  item('tor',{polygon:[RING(-122.2,47.7,.03)]}),item('svr',{kind:'severe',label:'Severe Thunderstorm Warning',polygon:[RING(-122.5,47.5)]}),
  item('gone',{polygon:[RING(-110,40)]})]));
const chips=$('rad-warn-chips').children;
assert.equal(chips.length,1,'only the tornado: not the tag\'s subject, not lesser hazards, not off-map');
const chip=chips[0];assert.equal(chip.textContent,'Tornado Warning');
const x=parseFloat(chip.style.left),y=parseFloat(chip.style.top);
assert.ok(x>=RAD_WARN_SAFE.l&&x<=RAD_WARN_SAFE.r&&y>=RAD_WARN_SAFE.t&&y<=RAD_WARN_SAFE.b,[x,y]);
''')


# ---------------------------------------------------------- failed refresh
def test_a_failed_refresh_keeps_outlines_dimmed_until_the_deadline():
    run(r'''
radarWarnUpdate(payload([item('a',{affectsStation:true,expires:wall+7200})],{staleAt:wall+150}));
assert.equal(radarWarnProblem(),'');assert.equal(radarOverlay.warn.attrs['data-state'],'current');
const before=states;
radarWarnUpdate(payload([item('a',{affectsStation:true,expires:wall+7200})],{staleAt:wall+150,refreshFailedAt:wall-1}));
assert.equal(paths().length,1,'still drawn');assert.equal(radarOverlay.warn.attrs['data-state'],'failed');
assert.equal(radarWarnProblem(),'Warnings refresh failed');assert.ok(states>before,'the status slot is refreshed');
assert.equal($('rad-warn-tag').hidden,false,'the station message stays');
wall+=151;fire();                                    // the last success's deadline passes
assert.equal(paths().length,0);assert.equal(radarWarnProblem(),'Warnings unavailable');
assert.equal($('rad-warn-tag').hidden,true);
radarWarnUpdate(payload([item('a',{affectsStation:true,expires:wall+7200})],{staleAt:wall+150}));
assert.equal(paths().length,1);assert.equal(radarWarnProblem(),'');assert.equal(radarOverlay.warn.attrs['data-state'],'current');
''')


def test_a_failed_refresh_reports_unknown_coverage_even_with_nothing_drawn():
    run(r'''
radarWarnUpdate(payload([],{staleAt:wall+150,refreshFailedAt:wall-1}));
assert.equal(radarWarnProblem(),'Warnings refresh failed');
radarWarn.on=false;radarWarnUpdate(payload([item('a')],{staleAt:wall+150,refreshFailedAt:wall-1}));
assert.equal(radarWarnProblem(),'Warnings refresh failed','area visibility does not determine coverage health');
''')


def test_a_warning_that_expires_during_a_failure_still_goes():
    run(r'''
radarWarnUpdate(payload([item('a',{expires:wall+60})],{staleAt:wall+150,refreshFailedAt:wall-1}));
assert.equal(paths().length,1);wall+=61;fire();assert.equal(paths().length,0);
''')


# --------------------------------------------------------------------- toggle
def test_toggle_has_one_footprint_and_an_honest_count():
    css = PAGE.read_text()
    assert re.search(r'\.rad-warn-toggle \{ width:112px;', css)
    assert not re.search(r'\.rad-warn-toggle\[[^\]]*\][^{]*\{[^}]*width', css), 'no state changes the width'
    button = re.search(r'<button[^>]*id="rad-warnings"[^>]*>.*?</button>', css).group(0)
    assert '>Warning areas<' in button and 'id="rad-warnings-count"' in button
    run(r'''
radarWarnUpdate(payload([]));assert.equal($('rad-warnings-count').textContent,'None');
assert.equal($('rad-warnings').getAttribute('aria-label'),'Warning areas, none in force');
radarWarnUpdate(payload([item('a'),item('b',{polygon:[RING(-122.0,47.9)]})]));
assert.equal($('rad-warnings-count').textContent,'Shown · 2');
assert.equal($('rad-warnings').getAttribute('aria-label'),'Warning areas, 2 shown');
''')


def test_controls_and_details_are_kept_out_of_map_gestures():
    html = PAGE.read_text()
    controls = re.search(r"RAD_CONTROLS='([^']*)'", html).group(1).split(',')
    assert '.rad-warn-card' in controls and '.rad-warn-tag' in controls


def test_controls_have_an_opaque_surface_over_reflectivity():
    css = PAGE.read_text()
    for selector in ('.rad-play', '.rad-step', '.rad-reset'):
        rule = re.search(re.escape(selector) + r'\s*\{([^}]*)\}', css, re.S)
        assert rule and 'background: var(--plate-scrim)' in rule.group(1), selector


# ------------------------------------------------------- in a real browser
BOOT = 'http://almanac.test/'


def _chromes():
    """Playwright's own browser first; else any headless shell already in its
    cache (a newer one than this Playwright pins still drives fine); or
    ALMANAC_TEST_CHROME. Nothing is downloaded."""
    import os
    yield None
    if os.environ.get('ALMANAC_TEST_CHROME'):
        yield os.environ['ALMANAC_TEST_CHROME']
    roots = [os.environ.get('PLAYWRIGHT_BROWSERS_PATH'), '~/Library/Caches/ms-playwright', '~/.cache/ms-playwright']
    for root in filter(None, roots):
        for shell in sorted(Path(root).expanduser().glob('chromium_headless_shell-*/*/chrome-headless-shell'), reverse=True):
            yield str(shell)


@pytest.fixture(scope='module')
def browser():
    import os
    if os.environ.get('RADAR_BROWSER_TEST') == '0':
        pytest.skip('Chromium disabled for this offline sandbox run')
    sync = pytest.importorskip('playwright.sync_api')
    with sync.sync_playwright() as p:
        b, errors = None, []
        for path in _chromes():
            try:
                b = p.chromium.launch(executable_path=path)
                break
            except Exception as error:                                 # noqa: BLE001
                errors.append(str(error).splitlines()[0])
        if b is None:
            pytest.skip(f'no Chromium for Playwright: {errors}')
        yield b
        b.close()


def _open(browser, theme):
    html = PAGE.read_text()
    ctx = browser.new_context(viewport=dict(width=1024, height=600))
    page = ctx.new_page()
    # served from memory; every other request (wx.json, tiles) is refused locally
    page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html)
               if route.request.url.split('?')[0] == BOOT else route.fulfill(status=503, body=''))
    page.goto(BOOT + '?tabs=1&theme=' + theme)
    return ctx, page


STRIP_ON_RADAR = '''async (flag)=>{
  renderAlerts({alerts:[{event:'Tornado Warning',tone:'accent',areaShort:'King, Snohomish',untilText:'Fri 2:19\\u00a0PM'},
                        {event:'Severe Thunderstorm Warning',tone:'brass',areaShort:'King',untilText:'Fri 2:37\\u00a0PM'}],
                alertsStale:false,alertsAsOf:'1:57\\u00a0PM'});
  document.querySelector('.page').dataset.radarAlerts='on';activate('s-radar');
  if(flag){document.getElementById('mastflag').classList.add('on');document.getElementById('staleflag').classList.add('on');}
  await new Promise(done=>setTimeout(done,900));            // the strip's 0.5 s entrance and the flags' fade are over
  const r=s=>{const b=document.querySelector(s).getBoundingClientRect();return {top:b.top,bottom:b.bottom,left:b.left,right:b.right,height:b.height}};
  const strip=document.getElementById('alert-strip');
  return {scotch:r('.masthead .scotch'),hair:r('.masthead .hair'),masthead:r('.masthead'),strip:r('#alert-strip'),
          head:r('#s-radar .sc-head'),plate:r('#rad-plate'),foot:r('.foot'),
          event:r('#al-event'),opacity:getComputedStyle(strip).opacity,stale:strip.classList.contains('stale'),
          eventSize:getComputedStyle(document.getElementById('al-event')).fontSize};
}'''


@pytest.mark.parametrize('theme', ['paper', 'night'])
@pytest.mark.parametrize('flag', [False, True])
def test_radar_alert_strip_sits_clear_of_the_masthead_rule(browser, theme, flag):
    ctx, page = _open(browser, theme)
    try:
        m = page.evaluate(STRIP_ON_RADAR, flag)
    finally:
        ctx.close()
    strip = m['strip']
    for rule in ('scotch', 'hair'):
        assert m[rule]['bottom'] <= strip['top'], (rule, m[rule], strip)   # no rule crosses the strip box
    assert m['masthead']['bottom'] <= strip['top']
    assert strip['bottom'] <= m['head']['top'] and m['head']['bottom'] <= m['plate']['top']
    assert m['plate']['height'] == 490 and m['plate']['bottom'] <= m['foot']['top'], m
    assert strip['height'] >= 26 and float(m['eventSize'][:-2]) >= 18
    assert strip['top'] <= m['event']['top'] and m['event']['bottom'] <= strip['bottom']
    # a fresh strip is drawn at full strength
    assert m['stale'] is False and m['opacity'] == '1'


def test_the_warning_toggle_keeps_its_footprint(browser):
    ctx, page = _open(browser, 'paper')
    try:
        size = page.evaluate('''()=>{activate('s-radar');document.getElementById('rad-zoom').hidden=false;
          const b=document.getElementById('rad-warnings'),out=[];
          const now=radarServerNow(),it=id=>({id,event:'Tornado Warning',label:'Tornado Warning',kind:'tornado',expires:now+900,
            polygon:[[[-122.4,47.5],[-122.2,47.5],[-122.2,47.7],[-122.4,47.5]]]});
          const measure=()=>{const r=b.getBoundingClientRect(),z=document.getElementById('rad-zoom-out').getBoundingClientRect();out.push([r.width,r.height,z.left,b.innerText]);};
          radarWarnUpdate({available:true,fetchedTs:now,staleAt:now+900,stale:false,items:[]});measure();
          radarWarnUpdate({available:true,fetchedTs:now,staleAt:now+900,stale:false,items:[it('a'),it('b'),it('c'),it('d'),it('e')]});measure();
          b.click();measure();b.click();return out;}''')
    finally:
        ctx.close()
    widths = {(w, h, left) for w, h, left, _ in size}
    assert len(widths) == 1, size                      # same box, and the rail does not move
    w, h, _ = widths.pop()
    assert w == 112 and h == 44
    assert [t.split('\n')[-1] for *_, t in size] == ['None', 'Shown · 5', 'Hidden · 5']


def test_small_and_clipped_warnings_offer_a_complete_ordered_list():
    run(r'''
radarCamera={lat:47.61,lon:-122.33,zoom:6};
const all=[item('tor',{affectsStation:true}),...Array.from({length:7},(_,n)=>item('svr'+n,{kind:'severe',label:'Severe Thunderstorm Warning'}))];
radarWarnUpdate(payload(all));
assert.equal($('rad-warn-list').hidden,false);
assert.equal($('rad-warn-tag').hidden,false,'station tornado remains explicit at small scale');
$('rad-warn-list').listeners.click[0]();
assert.equal(radarWarn.selected,'tor');
assert.equal(radarWarn.also.length,7,'no arbitrary three-warning truncation');
const rows=$('rad-warn-card').children.find(c=>c.className==='rad-warn-also').children;
assert.equal(rows[0].textContent,'Other warning areas');
rows.at(-1).listeners.click[0]();
assert.equal(radarWarn.selected,'svr6');assert.equal(radarWarn.also.length,7);
assert.equal($('rad-warn-tag').hidden,false,'lesser selection never hides the tornado');
radarWarnSelect(null);
radarCamera={lat:47.61,lon:-122.33,zoom:10};
radarWarnUpdate(payload([item('off',{polygon:[RING(-122.33,47.1)]})]));
assert.equal(count(),'Off map · 1');assert.equal($('rad-warn-list').hidden,false);
$('rad-warn-list').listeners.click[0]();assert.equal(radarWarn.selected,'off');
''')


def test_the_control_band_is_clipped_in_camera_coordinates_and_not_hittable():
    run(r'''
radarOverlay.warnClip=new Node('rect');
for(const zoom of [6,8,10]){
  radarCamera={lat:47.61,lon:-122.33,zoom};
  radarWarnUpdate(payload([item('big',{polygon:[RING(-122.33,47.61,10)]})]));
  const v=radarWarnScreen(),r=radarOverlay.warnClip.attrs;
  assert.ok(Math.abs(Number(r.y)*v.s+v.ty)<1e-8);
  assert.ok(Math.abs((Number(r.y)+Number(r.height))*v.s+v.ty-426)<1e-8);
  assert.equal(radarWarnHits(478,425).length,1);
  assert.equal(radarWarnHits(478,426).length,0);
  assert.equal(radarWarnHits(478,460).length,0);
}
''')


def test_counts_follow_the_unobstructed_view_when_camera_moves():
    run(r'''
radarWarnUpdate(payload([item('home'),item('far',{polygon:[RING(-115,40)]})]));
assert.equal(count(),'Shown · 1');
assert.ok($('rad-warnings').getAttribute('aria-label').endsWith('1 off map'));
radarCamera={lat:40,lon:-115,zoom:10};radarWarnPlace();
assert.equal(count(),'Shown · 1');
radarCamera={lat:35,lon:-110,zoom:10};radarWarnPlace();
assert.equal(count(),'Off map · 2');
''')


@pytest.mark.parametrize('theme', ['paper', 'night'])
def test_long_cards_keep_every_instruction_and_list_row_reachable(browser, theme):
    ctx, page = _open(browser, theme)
    try:
        result = page.evaluate('''()=>{
          activate('s-radar');const now=radarServerNow();
          radarView.data={center:{lat:47.61,lon:-122.33}};radarCamera={lat:47.61,lon:-122.33,zoom:6};
          radarOverlayBuild();
          const items=Array.from({length:9},(_,n)=>({id:String(n),kind:n?'severe':'tornado',affectsStation:!n,
            label:n?'Severe Thunderstorm Warning':'Tornado Warning',expires:now+900,
            instruction:'Official instruction. '.repeat(15),polygon:[[[-122.4,47.5],[-122.2,47.5],[-122.2,47.7],[-122.4,47.5]]]}));
          radarWarnUpdate({available:true,fetchedTs:now,staleAt:now+900,items});
          const list=document.getElementById('rad-warn-list'),target=list.getBoundingClientRect();list.click();
          const card=document.getElementById('rad-warn-card'),rows=card.querySelectorAll('.rad-warn-pick');
          card.scrollTop=card.scrollHeight;
          const box=card.getBoundingClientRect(),close=card.querySelector('.rad-warn-close').getBoundingClientRect(),last=rows[rows.length-1].getBoundingClientRect();
          return {target:[target.width,target.height],rows:rows.length,box:[box.top,box.bottom],close:[close.top,close.bottom],
            last:[last.top,last.bottom],lineClamp:getComputedStyle(card.querySelector('.rad-warn-do')).webkitLineClamp,
            safeBottom:document.getElementById('rad-plate').getBoundingClientRect().top+414};
        }''')
        assert min(result['target']) >= 56
        assert result['rows'] == 8 and result['lineClamp'] == 'none'
        assert result['box'][1] <= result['safeBottom']
        for part in ('close', 'last'):
            assert result['box'][0] <= result[part][0] < result[part][1] <= result['box'][1]
    finally:
        ctx.close()


def test_open_card_tracks_revisions_and_drops_expired_overlap_choices():
    run(r'''
const a=item('a',{instruction:'Original feed instruction.'}),b=item('b',{expires:wall+10});
radarWarnUpdate(payload([a,b]));radarWarnSelect(a,[a,b]);
radarWarnUpdate(payload([{...a,instruction:'Updated feed instruction.'},b]));
assert.ok($('rad-warn-card').text().includes('Updated feed instruction.'));
wall+=11;fire();assert.equal(radarWarn.also.length,0);
assert.ok(!$('rad-warn-card').text().includes('Also here'));
''')


def test_hidden_areas_still_report_failure_when_station_message_is_visible():
    run(r'''
radarWarn.on=false;
radarWarnUpdate(payload([item('a',{affectsStation:true})],{refreshFailedAt:wall-1}));
assert.equal($('rad-warn-tag').hidden,false);
assert.equal(radarWarnProblem(),'Warnings refresh failed');
''')


def test_viewport_count_uses_area_intersection_not_combined_ring_bounds():
    run(r'''
radarCamera={lat:47.61,lon:-122.33,zoom:10};
radarWarnUpdate(payload([item('islands',{polygon:[RING(-124,47.61),RING(-120,47.61)]})]));
assert.equal(count(),'Off map · 1','the empty space between separate rings is not warning area');
radarWarnUpdate(payload([item('enclosing',{polygon:[RING(-122.33,47.61,10)]})]));
assert.equal(count(),'Shown · 1','area can cover the whole view with every vertex off map');
radarWarnUpdate(payload([item('crossing',{polygon:[RING(-122.33,47.61,1)]})]));
assert.equal(count(),'Shown · 1','edges can cross the view with every vertex off map');
''')
