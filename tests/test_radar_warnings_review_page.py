""" Round-3 adversarial review of NWS warning polygons: page fixes.

The production NWS-WARNINGS block of console_live.html under node (the
harness of test_radar_warnings_page, whose DOM stand-in models a real tree:
moving or removing a focused node takes focus with it).
  4 warnings update on every payload, independent of imagery staging
  5 removal at the published (earliest) deadline, not the event end
  6 the freshness deadline is enforced on the page's producer clock
  + paths reconciled by warning id, keeping keyboard focus
"""
import re
from pathlib import Path

from tests.test_radar_warnings_page import run

PAGE = Path('design/almanac/console_live.html')


# ------------------------------------------- 4 independent of imagery staging
def _render_radar():
    html = PAGE.read_text()
    start = html.index('  function renderRadar(d){')
    return html[start:html.index('\n  function radarInterval(', start)]


def test_warnings_update_before_any_imagery_decision_can_return():
    body = _render_radar()
    call = body.index('radarWarnUpdate(r&&r.warnings,d.ts);')
    # every early return (tab hidden, starting, older payload, source staging) comes after it
    assert all(m.start() > call for m in re.finditer(r'return;', body))
    assert body.count('radarWarnUpdate(') == 1
    assert 'radarStageSource(r,d.ts);return;' in body          # the staging path that used to skip it


def test_a_payload_during_source_staging_replaces_the_warnings_and_the_hidden_count():
    run(r'''
radarWarnUpdate(payload([item('old')]),100);
assert.equal(paths().length,1);
// the next payloads arrive while a new source is being staged: same function, same effect
radarWarnUpdate(payload([item('tor',{affectsStation:true})]),110);
assert.deepEqual(paths().map(p=>p.attrs['data-covers']),['true']);
$('rad-warnings').listeners.click[0]();                        // viewer turns warnings off
radarWarnUpdate(payload([item('tor'),item('b',{polygon:[RING(-122.0,47.9)]})]),120);
assert.equal($('rad-warnings-count').textContent,'Hidden · 2');
// an older payload (out-of-order response) never undoes a newer one
radarWarnUpdate(payload([]),115);
assert.equal($('rad-warnings-count').textContent,'Hidden · 2');
''')


# ----------------------------------------------- 5 earliest removal deadline
def test_removal_follows_the_published_deadline_not_the_event_end():
    run(r'''
radarWarnUpdate(payload([item('a',{expires:wall+60,ends:wall+600,until:'1:55 PM'})]));
assert.equal(paths().length,1);assert.ok(paths()[0].attrs['aria-label'].includes('until 1:55 PM'));
wall+=61;fire();assert.equal(paths().length,0);
''')


# ------------------------------------------------------ 6 freshness deadline
def test_a_frozen_payload_goes_stale_at_its_deadline():
    run(r'''
radarWarnUpdate(payload([item('a',{expires:wall+7200})],{staleAt:wall+150}));
assert.equal(paths().length,1);assert.equal(radarWarnProblem(),'');
const t=[...timers.values()].at(-1);assert.ok(t.ms>=150000&&t.ms<151000,String(t.ms));
const before=states;
wall+=151;fire();                                   // no new payload: the page's own clock decides
assert.equal(paths().length,0);
assert.equal(radarWarnProblem(),'Warnings unavailable');
assert.ok(states>before,'the status slot is refreshed');
''')


def test_an_engine_without_stale_at_still_ages_its_payload():
    run(r'''
radarWarnUpdate(payload([item('a',{expires:wall+7200})],{fetchedTs:wall-10}));
assert.equal(paths().length,1);
wall+=1800;fire();
assert.equal(paths().length,0);assert.equal(radarWarnProblem(),'Warnings unavailable');
''')


def test_data_the_engine_calls_stale_hides_at_once():
    # `stale` now means past the deadline (or never fetched); a failed refresh
    # inside the deadline is refreshFailedAt (test_radar_warnings_ux_page).
    run(r'''
radarWarnUpdate(payload([item('a')],{staleAt:wall+900}));assert.equal(paths().length,1);
radarWarnUpdate(payload([item('a')],{staleAt:wall+900,stale:true}));
assert.equal(paths().length,0);assert.equal(radarWarnProblem(),'Warnings unavailable');
radarWarnUpdate(payload([item('a')],{staleAt:wall+900}));
assert.equal(paths().length,1);assert.equal(radarWarnProblem(),'');
''')


# --------------------------------------------- reconcile by id, keep focus
def test_an_unchanged_payload_keeps_the_same_nodes_and_focus():
    run(r'''
radarWarnUpdate(payload([item('a'),item('b',{polygon:[RING(-122.0,47.9)]})]));
const [b,a]=paths();a.focus();const calls=focusCalls;
radarWarnUpdate(payload([item('a'),item('b',{polygon:[RING(-122.0,47.9)]})]));
assert.equal(paths()[0],b);assert.equal(paths()[1],a);
assert.equal(document.activeElement,a,'focus survives a refresh');
assert.equal(focusCalls,calls,'nothing had to be refocused');
''')


def test_a_reorder_keeps_focus_on_the_same_warning():
    run(r'''
radarWarnUpdate(payload([item('a'),item('b',{polygon:[RING(-122.0,47.9)]})]));
const a=radarWarn.nodes.get('a').node;a.focus();
// a new, more important warning leads: 'a' now draws lower in the stack
radarWarnUpdate(payload([item('new',{affectsStation:true,polygon:[RING(-122.5,47.5)]}),item('b',{polygon:[RING(-122.0,47.9)]}),item('a')]));
assert.equal(paths().length,3);
assert.equal(document.activeElement,radarWarn.nodes.get('a').node,'focus follows the warning');
assert.equal(radarWarn.nodes.get('a').node,a,'the same node was kept');
// a changed warning updates its node in place
radarWarnUpdate(payload([item('new',{affectsStation:true,polygon:[RING(-122.5,47.5)]}),item('b',{polygon:[RING(-122.0,47.9)]}),item('a',{polygon:[RING(-122.33,47.61,.1)]})]));
assert.equal(radarWarn.nodes.get('a').node,a);assert.equal(document.activeElement,a);
// the lead warning is still drawn last, on top
assert.equal(paths().at(-1),radarWarn.nodes.get('new').node);
''')


def test_a_removed_warning_loses_its_node_and_hit_target():
    run(r'''
radarWarnUpdate(payload([item('a'),item('b',{polygon:[RING(-122.0,47.9)]})]));
radarWarnUpdate(payload([item('b',{polygon:[RING(-122.0,47.9)]})]));
assert.equal(paths().length,1);assert.equal(radarWarn.paths.length,1);
assert.ok(!radarWarn.nodes.has('a'));
''')
