"""The motion budget: the rain's frame rate follows the machine's headroom.

motionGovern is the policy (pure), motionLate the pacing verdict (pure); both
run here in node exactly as the page ships them. The page wiring (clock, poll
report, still tier) is pinned by source checks. Costs behind the tiers were
measured on the Pi 4 panel, 2026-10-26: Chromium ~205 % of a core at 60 fps,
~130 % at 30, ~95 % at 20, ~62 % at 12, 15 % still.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = Path('design/almanac/console_live.html').read_text()

needs_node = pytest.mark.skipif(shutil.which('node') is None, reason='node not installed')


def js_function(name):
    start = HTML.index('function ' + name + '(')
    depth = 0
    for i in range(HTML.index('{', start), len(HTML)):
        depth += {'{': 1, '}': -1}.get(HTML[i], 0)
        if depth == 0:
            return HTML[start:i + 1]
    raise AssertionError('unbalanced ' + name)


def js_var(name):
    m = re.search(r'var ' + name + r' = .*?;\n', HTML, re.S)
    assert m, name
    return m.group(0)


PRELUDE = '\n'.join([js_function('isNum'), js_var('MOTION_FPS'), js_var('MOTION'),
                     js_function('motionLate'), js_function('motionGovern')]) + '''
function fresh(tier) {
  return { tier: tier === undefined ? 1 : tier, since: 0, reason: "start", ema: null, emaAt: 0, late: null, lateAt: 0,
           active: false, healthy: null, powerUntil: 0, blocked: {}, backoff: {} };
}
/* poll every 2 s for `secs`, cpu from cost(tier); a moving tier shows smooth
   pacing (the page always measures its frames), still shows none */
function run(st, secs, cost, extra, t0) {
  var t = t0 || 0, changes = [];
  for (; t < (t0 || 0) + secs * 1000; t += 2000) {
    var sig = Object.assign({ active: true, cpu: cost ? cost(st.tier) : null,
                              late: MOTION_FPS[st.tier] > 0 ? 0.02 : null }, extra ? extra(t, st) : {});
    var prev = st.tier;
    st = motionGovern(st, sig, t);
    if (st.tier !== prev) changes.push([t / 1000, prev, st.tier, st.reason]);
  }
  return { st: st, changes: changes, t: t };
}
'''


def node(body):
    out = subprocess.run(['node'], input=PRELUDE + body, text=True, capture_output=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_pacing_at_the_displays_own_rate_is_judged_against_the_display_interval():
    r = node('''
      var steady = [], half = [], jank = [], hz48 = [], hz120 = [];
      for (var i = 0; i < 60; i++) {
        steady.push(16.7); half.push(33.4); jank.push(i % 4 ? 16.7 : 50); hz48.push(20.8); hz120.push(8.3);
      }
      console.log(JSON.stringify([motionLate(steady), motionLate(half), motionLate(jank), motionLate(hz48),
                                  motionLate(hz120), motionLate([16.7, 16.7])]));
    ''')
    assert r[0] == 0
    assert r[1] == 1                               # stuck at half rate is janky, not a 30 Hz screen
    assert r[2] == pytest.approx(15 / 60)
    assert r[3] == 0 and r[4] == 0                 # a real 48 Hz or 120 Hz display is fine
    assert r[5] is None                            # too few frames to judge


@needs_node
def test_a_clocked_tier_is_judged_on_its_own_ticks_whatever_the_display_does():
    r = node('''
      var hold30 = [], collapse = [], miss20 = [], hold30on120 = [];
      for (var i = 0; i < 40; i++) {
        hold30.push(33.4);                         // 30 fps held on a 60 Hz display
        collapse.push(50.1);                       // the page manages 20 fps when asked for 30
        miss20.push(i % 4 ? 50 : 66.7);            // 20 fps, every fourth tick a missed frame late
        hold30on120.push(i % 3 ? 33.3 : 41.7);     // 120 Hz panel: 30 fps with a frame of slack
      }
      console.log(JSON.stringify([motionLate(hold30, 1000 / 30), motionLate(collapse, 1000 / 30),
                                  motionLate(miss20, 50), motionLate(hold30on120, 1000 / 30),
                                  motionLate([33, 33, 33], 1000 / 30)]));
    ''')
    assert r[0] == 0
    assert r[1] == 1                               # sustained collapse is janky, not a new normal
    assert r[2] == pytest.approx(10 / 40)
    assert r[3] == 0
    assert r[4] is None                            # three ticks are no verdict


@needs_node
def test_a_pi4_like_machine_climbs_to_the_full_rate_and_stays():
    # system busy share by tier at extreme rain: (Chromium + engine) / 4 cores
    r = node('''
      var cost = function (t) { return [0.56, 0.37, 0.28, 0.2, 0.06][t]; };
      var out = run(fresh(1), 3600, cost);
      console.log(JSON.stringify({ tier: out.st.tier, changes: out.changes }));
    ''')
    assert r['tier'] == 0
    assert len(r['changes']) == 1 and r['changes'][0][3] == 'headroom'
    assert r['changes'][0][0] >= 30                # not before a healthy dwell


@needs_node
def test_a_machine_near_its_limit_settles_instead_of_hunting():
    # 60 fps overloads it, 30 fits: it must settle at 30, probing 60 ever more rarely
    r = node('''
      var cost = function (t) { return [0.86, 0.48, 0.38, 0.3, 0.1][t]; };
      var out = run(fresh(1), 3600, cost);
      console.log(JSON.stringify({ tier: out.st.tier, changes: out.changes, backoff: out.st.backoff }));
    ''')
    assert r['tier'] == 1
    probes = [c for c in r['changes'] if c[2] == 0]
    gaps = [b[0] - a[0] for a, b in zip(probes, probes[1:])]
    assert len(probes) <= 6                        # an hour: 1, 2, 4, 8, 16, 30 min bars at most
    assert gaps == sorted(gaps)                    # each probe waits longer than the last
    assert all(c[3] == 'cpu' for c in r['changes'] if c[2] == 1)


@needs_node
def test_a_slow_machine_walks_down_until_it_fits_and_severe_load_drops_two_tiers():
    r = node('''
      var cost = function (t) { return [1.0, 0.95, 0.7, 0.55, 0.12][t]; };
      var out = run(fresh(0), 120, cost);
      console.log(JSON.stringify({ tier: out.st.tier, changes: out.changes }));
    ''')
    assert r['changes'][0][1:3] == [0, 2]          # 100 % busy: straight past 30 fps
    assert r['tier'] == 2                          # 0.70 is under the 0.75 line: it holds at 20 fps


@needs_node
def test_one_janky_window_is_noise_and_sustained_jank_is_not():
    # measured on the Pi 4 at a steady 30 fps: windows 0-7 % late, one at 23 %;
    # the live run stepped down on single windows like that before pacing was averaged
    r = node('''
      var spike = run(fresh(1), 60, function () { return 0.3; }, function (t) { return { late: t === 20000 ? 0.4 : 0.03 }; });
      var bad = run(fresh(1), 20, function () { return 0.3; }, function () { return { late: 0.4 }; });
      console.log(JSON.stringify({ spike: spike.changes.filter(function (c) { return c[3] === "frames"; }).length,
                                   bad: bad.changes[0] }));
    ''')
    assert r['spike'] == 0
    assert r['bad'][1:] == [1, 2, 'frames']


@needs_node
def test_one_busy_poll_does_not_trip_the_average():
    r = node('''
      var st = fresh(0), t = 0;
      for (; t < 40000; t += 2000) st = motionGovern(st, { active: true, cpu: 0.3 }, t);
      st = motionGovern(st, { active: true, cpu: 1.0 }, t);          // one radar render burst
      console.log(JSON.stringify({ tier: st.tier, ema: st.ema }));
    ''')
    assert r['tier'] == 0 and r['ema'] < 0.75


@needs_node
def test_under_voltage_holds_still_for_ten_minutes_then_climbs_one_tier_at_a_time():
    r = node('''
      var cost = function (t) { return [0.5, 0.35, 0.25, 0.2, 0.08][t]; };
      var age = function (t) { return { undervoltAgeSec: t < 4000 ? 0 : (t - 4000) / 1000 }; };
      var out = run(fresh(0), 900, cost, age);
      console.log(JSON.stringify({ tier: out.st.tier, changes: out.changes }));
    ''')
    first = r['changes'][0]
    assert first[1:] == [0, 4, 'power']                           # still at once, whatever the load
    ups = [c for c in r['changes'][1:]]
    assert ups[0][0] >= 4 + 600                                    # not before ten steady minutes
    assert [c[2] for c in ups] == [3, 2, 1, 0]                     # then one tier per healthy dwell
    assert all(b[0] - a[0] >= 30 for a, b in zip(ups, ups[1:]))


@needs_node
def test_a_restarted_engine_forgetting_the_alarm_cannot_cut_the_power_hold_short():
    r = node('''
      var cost = function () { return 0.2; };
      // alarm seen at t=0; at t=20 s the engine restarts and reports null from then on
      var age = function (t) { return { undervoltAgeSec: t < 20000 ? t / 1000 : null }; };
      var out = run(fresh(0), 700, cost, age);
      console.log(JSON.stringify(out.changes.map(function (c) { return [c[0], c[2], c[3]]; })));
    ''')
    assert r[0] == [0, 4, 'power']
    assert r[1][0] >= 600                          # still until ten minutes after the alarm, not 30 s


@needs_node
def test_the_dwell_counts_only_unbroken_healthy_time():
    r = node('''
      var st = fresh(2), t, sig = function (cpu) { return { active: true, cpu: cpu, late: 0.02 }; };
      for (t = 0; t <= 40000; t += 2000) st = motionGovern(st, sig(0.6), t);   // between low and high: holding, not healthy
      st = motionGovern(st, sig(0.0), 42000);
      var at42 = st.tier, start = st.healthy;
      for (t = 44000; t < 72000; t += 2000) st = motionGovern(st, sig(0.0), t);
      var before = st.tier;
      st = motionGovern(st, sig(0.0), 72000);
      console.log(JSON.stringify([at42, start, before, st.tier]));
    ''')
    # the first calm sample starts the dwell, it does not complete it
    assert r == [2, 42000, 2, 1]


@needs_node
def test_a_moving_tier_needs_pacing_evidence_to_climb():
    r = node('''
      var silent = run(fresh(1), 120, function () { return 0.1; }, function () { return { late: null }; });
      console.log(JSON.stringify(silent.st.tier));
    ''')
    assert r == 1                                  # no frames measured is not the same as smooth frames


@needs_node
def test_power_hold_applies_even_while_dry_so_rain_starts_still():
    r = node('''
      var st = motionGovern(fresh(0), { active: false, undervoltAgeSec: 12 }, 1000);
      console.log(JSON.stringify([st.tier, st.reason]));
    ''')
    assert r == [4, 'power']


@needs_node
def test_heat_steps_down_and_warm_blocks_the_climb():
    r = node('''
      var cool = function (t) { return 0.3; };
      var hot = run(fresh(0), 20, cool, function () { return { tempC: 81 }; });
      var warm = run(fresh(2), 600, cool, function () { return { tempC: 77 }; });
      console.log(JSON.stringify({ hot: hot.changes[0], warm: warm.st.tier }));
    ''')
    assert r['hot'][1:] == [0, 1, 'heat']
    assert r['warm'] == 2


@needs_node
def test_a_lan_page_governs_on_its_own_frames_alone():
    r = node('''
      var janky = run(fresh(0), 20, null, function () { return { late: 0.4 }; });
      var smooth = run(fresh(3), 200, null, function () { return { late: 0.02 }; });
      console.log(JSON.stringify({ janky: janky.changes[0], smooth: smooth.st.tier, ema: smooth.st.ema }));
    ''')
    assert r['janky'][1:] == [0, 1, 'frames']
    assert r['smooth'] == 0 and r['ema'] is None


@needs_node
def test_no_evidence_while_the_rain_is_idle_and_a_fresh_look_when_it_returns():
    r = node('''
      var st = fresh(1), t = 0;
      for (; t < 120000; t += 2000) st = motionGovern(st, { active: false, cpu: 0.05 }, t);
      var idleTier = st.tier;
      st = motionGovern(st, { active: true, cpu: 0.05 }, t);         // rain starts
      var sinceReset = st.since === t, tierAtStart = st.tier;
      st = motionGovern(st, { active: true, cpu: 0.05 }, t + 2000);
      console.log(JSON.stringify([idleTier, sinceReset, tierAtStart, st.tier]));
    ''')
    assert r == [1, True, 1, 1]                    # dry time is not a healthy dwell


@needs_node
def test_a_tier_held_healthy_for_ten_minutes_forgets_its_bar_and_mere_residence_does_not():
    r = node('''
      function hold(cpu) {
        var st = fresh(0), t; st.backoff = { 0: 960000 };
        for (t = 0; t <= 602000; t += 2000) st = motionGovern(st, { active: true, cpu: cpu, late: 0.02 }, t);
        return st.backoff;
      }
      console.log(JSON.stringify([hold(0.4), hold(0.6)]));
    ''')
    assert r[0] == {}                              # ten unbroken healthy minutes
    assert r[1] == {'0': 960000}                   # ten minutes merely holding at 0.6 is not proof


def test_page_wiring():
    rain = HTML[HTML.index('var rainMm = isNum(data.rainRateMm)'):HTML.index('/* NOTE: no intensity word here')]
    assert rain.rstrip().endswith('motionTick(data);')             # last, after every glide the update starts
    tick = js_function('motionTick')
    assert 'radarIntent.panel' in tick                             # the Pi's CPU governs the Pi's own screen only
    assert '_reduceMotion' in tick                                 # a person's reduced-motion wins outright
    assert 'motionSync()' in js_function('activate')
    assert '"&motion=" + MOTION_FPS[motion.st.tier] + ":" + motion.st.reason' in js_function('motionQuery')
    assert 'motionReport + (ack ? "&r=1" : "")' in js_function('poll')
    assert '#raingauge.motion-still, #raingauge.motion-still * { transition: none !important; }' in HTML
    sync = js_function('motionSync')
    assert 'motionReconcile(g.getAnimations({ subtree: true }))' in sync   # glides are clocked too, not just keyframes
    assert 'a._motion' in sync                                     # only what the clock paused is handed back
    assert 'a.finish()' in sync                                    # a held glide lands when the tier goes still
    # Chromium keeps a script-touched keyframe animation alive after its CSS
    # drops it and builds a twin when the CSS names it again (measured,
    # 2026-10-26): every touched one the CSS no longer names, or has rebuilt,
    # is cancelled, so no part goes on moving unseen
    rec = js_function('motionReconcile')
    assert 'getComputedStyle(t).animationName' in rec and 'a.cancel()' in rec and 'rebuilt' in rec
    # cleanup never waits for a successful poll: a part fading out, the rain
    # stopping and the page coming back on screen each re-sync at once
    assert 'g._offT = null; motionSync();' in js_function('rainShow')
    assert '_rainOffT = null; motionSync(); }, RAIN_FADE_MS);' in HTML
    assert 'document.addEventListener("visibilitychange", function () { motionSync(); });' in HTML
    assert '$("s-obs").classList.contains("active")' in sync        # off screen nothing touched ticks
    # one engine sample counts once: a stalled engine's file served again is not new evidence
    assert 'data.ts !== m.perfTs' in tick
    # a slow page's few ticks add up to a verdict instead of being thrown away each poll
    assert 'if (late !== null || !active) { m.gaps = []; m.ticks = []; }' in tick
    # off screen is no evidence: the dwell starts over when the gauge is seen again
    assert '{ active: false, healthy: null }' in sync
    # the first verdict after a tier change (or a return to the screen) measures the change: dropped
    assert 'if (late !== null && m.settling) { m.settling = false; late = null; }' in tick
    assert 'm.settling = true' in sync and 'm.settling = true' in tick
