"""The engraved rain gauge: wind lean, seamless plates, Pi-safe animation.

The rate gauge's water level, pointer and graduations are data; the rain,
the surface and the splashes are texture. These tests pin what keeps it
that way and what keeps it cheap on the Pi 4: every keyframe moves only
transform and opacity, every animation stops with the rain and under
prefers-reduced-motion, the loops are exactly one tile long, and the lean
follows the measured wind by one bounded, tested formula.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = Path('design/almanac/console_live.html').read_text()

CSS = re.sub(r'/\*.*?\*/', '', HTML[HTML.index('/* ———— rate gauge · engraved rain over a water wash'):
                                    HTML.index('#raingauge .tick       { stroke-width: 2.6; }')], flags=re.S)
BUILD = HTML[HTML.index('var rainPtr, rainArt = null'):
             HTML.index('/* ———— 24-hour barograph — pressure-trend shape')]
RENDER = HTML[HTML.index('var rainMm = isNum(data.rainRateMm)'):
              HTML.index('/* NOTE: no intensity word here')]

needs_node = pytest.mark.skipif(shutil.which('node') is None, reason='node not installed')


def js_function(name):
    """Source of a top-level `function name(...) {...}` by brace matching."""
    start = BUILD.index('function ' + name + '(')
    depth = 0
    for i in range(BUILD.index('{', start), len(BUILD)):
        depth += {'{': 1, '}': -1}.get(BUILD[i], 0)
        if depth == 0:
            return BUILD[start:i + 1]
    raise AssertionError('unbalanced ' + name)


def js_var(name):
    """Source of a `var NAME = ...;` declaration (object or array literal)."""
    m = re.search(r'var ' + name + r' = .*?;\n', BUILD, re.S)
    assert m, name
    return m.group(0)


def run_node(script):
    result = subprocess.run(['node'], input=script, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout


def keyframes(css):
    """{name: body} for every @keyframes block (one level of nesting)."""
    out = {}
    for m in re.finditer(r'@keyframes\s+([\w-]+)\s*\{', css):
        depth, i = 1, m.end()
        while depth:
            depth += {'{': 1, '}': -1}.get(css[i], 0)
            i += 1
        out[m.group(1)] = css[m.end():i - 1]
    return out


# ---------------------------------------------------------------- the lean

@needs_node
def test_lean_follows_the_eastward_wind_bounded_and_unit_aware():
    script = js_function('rainLeanDeg') + r'''
const assert = require('node:assert/strict');
const near = (a, b, tol = 1e-6) => assert.ok(Math.abs(a - b) < tol, a + ' vs ' + b);
// calm is upright; a wind with no eastward component does not lean the rain
near(rainLeanDeg(0, 270, 'mph'), 0);
near(rainLeanDeg(20, 0, 'mph'), 0);
near(rainLeanDeg(20, 180, 'mph'), 0, 1e-9);
// a westerly (from 270) drives the rain right (+); an easterly left (-)
const w = rainLeanDeg(25, 270, 'mph'), e = rainLeanDeg(25, 90, 'mph');
assert.ok(w > 20 && w < 28, 'westerly ' + w);
near(e, -w);
// the documented curve: 28 deg * tanh(u / 8 m/s)
near(w, 28 * Math.tanh(25 * 0.44704 / 8));
// partial: from the south-west only the eastward component counts
near(rainLeanDeg(10, 225, 'm/s'), 28 * Math.tanh(10 * Math.sin(Math.PI / 4) / 8));
// bounded, and monotonic in speed
assert.ok(Math.abs(rainLeanDeg(400, 270, 'mph')) <= 30);
let last = -1;
for (const v of [0, 2, 5, 10, 20, 40, 80]) { const l = rainLeanDeg(v, 270, 'm/s'); assert.ok(l > last || v === 0); last = l; }
// every emitted unit converts to the same wind
const ref = rainLeanDeg(10, 270, 'm/s');
near(rainLeanDeg(10 / 0.44704, 270, 'mph'), ref);
near(rainLeanDeg(36, 270, 'km/h'), ref);
near(rainLeanDeg(10 / 0.514444, 270, 'kts'), ref);
// Beaufort: v = 0.836 * B^1.5 m/s
near(rainLeanDeg(6, 270, 'bft'), rainLeanDeg(0.836 * Math.pow(6, 1.5), 270, 'm/s'));
// unknown unit or missing reading: no lean
for (const args of [[10, 270, 'furlongs'], [10, 270, undefined], [null, 270, 'mph'],
                    [10, null, 'mph'], [NaN, 270, 'mph'], [10, Infinity, 'mph']])
  near(rainLeanDeg(...args), 0);
'''
    run_node(script)


def test_lean_is_slewed_from_the_first_reading_and_shared_by_every_part():
    # seeded by the first real wind reading, then a bounded step per poll
    assert 'if (_leanShown === null) { if (isNum(data.windSpd)) _leanShown = leanTo; }' in RENDER
    assert 'clamp((leanTo - _leanShown) * RAIN_LEAN_SLEW, -RAIN_LEAN_STEP, RAIN_LEAN_STEP)' in RENDER
    assert re.search(r'RAIN_LEAN_SLEW = 0\.\d+, RAIN_LEAN_STEP = \d', BUILD)
    # one slant: rain, plunging drops and crowns all take it (crowns throw
    # downwind, i.e. the opposite shear), and it glides between polls
    assert 'leans = [[rainLean, 1]]' in BUILD
    assert 'leans.push([plungeLean, P.lean])' in BUILD and 'leans.push([crownLean, -0.6])' in BUILD
    assert '"skewX(" + (lean * l[1]).toFixed(2) + "deg)"' in RENDER
    assert re.search(r'\.rain-lean\s*\{ transition: transform [\d.]+s', CSS)


# ---------------------------------------------------- Pi-safe animation

def test_keyframes_move_only_transform_and_opacity():
    frames = keyframes(CSS)
    assert {'rain-fall', 'rain-plunge', 'surf-drift', 'imp-ring-a', 'imp-ring-b', 'imp-crown-a',
            'imp-crown-b', 'imp-spray'} <= set(frames)
    for name, body in frames.items():
        props = set(re.findall(r'([\w-]+)\s*:', body))
        assert props <= {'transform', 'opacity'}, (name, props)


def test_transitions_are_transform_and_opacity_only():
    for decl in re.findall(r'transition:\s*([^;]+);', CSS):
        if decl.strip() in ('none', 'none !important'):
            continue
        props = {part.split()[0] for part in decl.split(',')}
        assert props <= {'transform', 'opacity'}, decl
    # the old per-poll dash growth repainted every frame; nothing like it remains
    assert 'stroke-dasharray' not in CSS and 'strokeDasharray' not in RENDER


def test_every_animation_runs_only_while_raining_and_never_under_reduced_motion():
    animated = re.findall(r'([^{};]+)\{\s*animation:(?!\s*none)[^;]+;', CSS)
    assert animated
    targets = set()
    for selector in animated:
        sel = selector.strip()
        assert sel.startswith('#raingauge.rain-anim '), sel   # stops once the gauge is dry
        targets.add(re.findall(r'\.([\w-]+)', sel.split()[-1])[0])
    reduce = CSS[CSS.index('@media (prefers-reduced-motion: reduce)'):]
    still = reduce[:reduce.index('{ animation: none !important; }')]
    assert targets <= set(re.findall(r'#raingauge \.([\w-]+)', still)), targets
    # parts animate only while they are visible or fading (the `live` gate),
    # and the gauge-wide gate is lifted one fade after the last drop
    assert '.rain-plate.live' in CSS and '.rain-plunge.live' in CSS and '.imp-tier.live' in CSS
    assert 'setTimeout(function () { g.classList.remove("live"); g._offT = null; }, RAIN_FADE_MS)' in BUILD
    assert 'setTimeout(function () { gauge.classList.remove("rain-anim"); _rainOffT = null; }, RAIN_FADE_MS)' in RENDER


def test_loops_travel_exactly_one_tile_so_they_are_seamless():
    frames = keyframes(CSS)
    tile_h = float(re.search(r'RAIN_TILE = \{ w: [\d.]+, h: ([\d.]+)', BUILD).group(1))
    plunge_h = float(re.search(r'RAIN_PLUNGE = \{[^}]*h: ([\d.]+)', BUILD).group(1))
    wavelength = float(re.search(r'RAIN_SURF = \{ wl: ([\d.]+)', BUILD).group(1))
    assert f'translateY({tile_h:g}px)' in frames['rain-fall']
    assert f'translateY({plunge_h:g}px)' in frames['rain-plunge']
    assert f'translateX({wavelength:g}px)' in frames['surf-drift']
    plunge_secs = re.search(r'RAIN_PLUNGE = \{[^}]*secs: ([\d.]+)', BUILD).group(1)
    assert f'rain-plunge {plunge_secs}s' in CSS


def test_drops_change_at_the_surface():
    # under water a drop is slower, shorter, more upright and dimmer than
    # anything falling in the air, so the surface reads as a change of medium
    start = BUILD.index('var RAIN_PLATES = [')
    plates = BUILD[start:BUILD.index('];', start)]
    plunge = re.search(r'var RAIN_PLUNGE = \{([^}]*)\}', BUILD).group(1)
    tile_h = float(re.search(r'RAIN_TILE = \{ w: [\d.]+, h: ([\d.]+)', BUILD).group(1))
    slowest_air = min(tile_h / float(s) for s in re.findall(r'secs: ([\d.]+)', plates))
    bead_speed = float(re.search(r'\bh: ([\d.]+)', plunge).group(1)) / float(re.search(r'secs: ([\d.]+)', plunge).group(1))
    assert bead_speed < slowest_air / 2, (bead_speed, slowest_air)
    shortest_air = min(float(a) for a in re.findall(r'len: \[([\d.]+),', plates))
    assert float(re.search(r'len: \[[\d.]+, ([\d.]+)\]', plunge).group(1)) <= shortest_air
    assert float(re.search(r'lean: ([\d.]+)', plunge).group(1)) < 0.5
    faintest_air = min(float(o) for o in re.findall(r'\bop: ([\d.]+)', plates))
    upper, lower = (float(o) for o in re.search(r'op: \[([\d.]+), ([\d.]+)\]', plunge).groups())
    assert lower < upper < faintest_air


@needs_node
def test_rain_tiles_cover_the_air_column_and_the_steepest_lean():
    script = js_var('RAIN_TILE') + js_var('RAIN_PLATES') + r'''
const assert = require('node:assert/strict');
const T = RAIN_TILE, top = 13, bot = 136, x1 = 16, x2 = 56;   // the air clip: mouth to floor
const longest = Math.max(...RAIN_PLATES.map(p => p.len[1]));
// two copies down, translated by one tile: every drop that can show is drawn
assert.ok(T.y0 <= top - longest, 'tile starts too low');
assert.ok(T.y0 + T.h > bot, 'tile ends too high');
// three copies across reach the walls at a 30 degree slant about mid-column
const reach = Math.tan(30 * Math.PI / 180) * (bot - top) / 2;
assert.ok(T.x0 - T.w <= x1 - reach && T.x0 + 2 * T.w >= x2 + reach, 'lean runs out of rain');
// drizzle is short and slow, a torrent long and fast; they never share a period
for (let i = 1; i < RAIN_PLATES.length; i++) {
  assert.ok(RAIN_PLATES[i].len[1] > RAIN_PLATES[i - 1].len[1]);
  assert.ok(RAIN_PLATES[i].secs < RAIN_PLATES[i - 1].secs);
}
assert.equal(RAIN_PLATES[0].on[0], 0, 'the first drop in is a drizzle drop');
'''
    run_node(script)


@needs_node
def test_plates_are_engraved_identically_on_every_load_and_wrap_the_tile():
    script = js_function('rainRand') + js_function('rainStreaks') + r'''
const assert = require('node:assert/strict');
const a = rainStreaks(rainRand(1903), 40, [8, 14], 48, 150), b = rainStreaks(rainRand(1903), 40, [8, 14], 48, 150);
assert.deepEqual(a, b);
const starts = [...a.tails.matchAll(/M([\d.]+) ([\d.]+)V([\d.]+)/g)];
assert.equal(starts.length, 40);
for (const [, x, y0, y1] of starts) {
  assert.ok(+x >= 0 && +x < 48 && +y0 >= 0 && +y0 < 150);
  assert.ok(+y1 - +y0 >= 8 - 1e-9 && +y1 - +y0 <= 14 + 1e-9);
}
// no two drops share a column closely enough to read as a dashed line
const xs = starts.map(s => +s[1]).sort((p, q) => p - q);
const dup = xs.filter((x, i) => i && x - xs[i - 1] < 0.05).length;
assert.ok(dup <= 2, 'columns repeat: ' + dup);
'''
    run_node(script)


# ---------------------------------------------------------- data honesty

def test_level_pointer_and_waterline_ride_the_measurement_not_the_texture():
    assert 'var ride = "translateY(" + (-rr * (rainGeom.bot - rainGeom.top)).toFixed(1) + "px)";' in RENDER
    for part in ('rainArt.body.style.transform = ride;', 'rainArt.surface.style.transform = ride;',
                 'rainPtr.style.transform = ride;'):
        assert part in RENDER
    # the texture lags by design; the first real measurement seeds it outright
    assert 'if (_rrShown === null) { if (isNum(rainMm)) _rrShown = rr; }' in RENDER
    assert '_rrDryPolls = rr > 0 ? 0 : _rrDryPolls + 1;' in RENDER
    assert 'else _rrShown = rainEase(_rrShown, rr, _rrDryPolls);' in RENDER
    # nothing per frame: no rAF loop, no filters, no gradients, no raster
    code = BUILD + RENDER
    for banned in ('requestAnimationFrame', 'filter', 'Gradient', '<image', 'canvas'):
        assert banned not in code, banned
    assert not re.search(r'(?<![\w-])filter\s*:|gradient\(', CSS)


@needs_node
def test_texture_eases_through_flux_and_thins_out_when_dry():
    # one step = one 2 s poll, counting dry polls exactly as renderGauges does
    script = js_var('RAIN_EASE') + js_function('rainEase') + r'''
const run = (s, targets) => { let dry = 0;   // each case starts from steady rain
  return targets.map(t => (dry = t > 0 ? 0 : dry + 1, s = rainEase(s, t, dry))); };
const rep = (t, n) => Array(n).fill(t);
const spikeTop = run(0.5, rep(0.65, 30)).pop();
console.log(JSON.stringify({
  onset: run(0, rep(0.7, 12)), stop: run(0.7, rep(0, 12)), lightStop: run(0.3, rep(0, 12)),
  blip: run(0.7, [0, 0.7, 0.7, 0.7, 0.7]), dip: run(0.6, rep(0.45, 15)),
  spikeBack: run(spikeTop, rep(0.5, 15)), spikeTop }));
'''
    r = json.loads(run_node(script))
    # rain shows on the first wet poll, reaches 90% in 7 polls (14 s) and lands
    assert r['onset'][0] >= 0.2 and r['onset'][6] >= 0.9 * 0.7 and r['onset'][-1] == 0.7
    # a dry reading first eases like a dip; a dry spell then thins out quickly
    # and is gone by the 9th poll (18 s), from a downpour or a shower
    assert r['stop'][0] > 0.55 and r['stop'][3] < 0.3 and r['stop'][8] == 0
    assert r['lightStop'][8] == 0
    # one dry blip between wet readings is a small dip, not a stop
    assert min(r['blip']) > 0.6 and r['blip'][-1] == 0.7
    # while it still rains a dip is eased (at most ~a sixth of the gap per poll)
    # and settles within 30 s; a one-minute spike lands back the same way
    assert r['dip'][0] >= 0.57 and r['dip'][-1] == 0.45
    assert r['spikeTop'] == 0.65 and r['spikeBack'][0] > 0.6 and r['spikeBack'][-1] == 0.5
    # never overshoots, always moves toward the measurement
    for start, seq, target in ((0, r['onset'], 0.7), (0.7, r['stop'], 0), (0.6, r['dip'], 0.45)):
        prev = start
        for v in seq:
            assert min(prev, target) <= v <= max(prev, target)
            prev = v


def test_gauge_colours_are_the_pages_tokens():
    # every paint is a token, or the water wash mixed from two of them
    for prop, value in re.findall(r'(?<![\w-])(fill|stroke)\s*:\s*([^;]+);', CSS):
        assert value.strip() in ('none', 'var(--water)', 'var(--water-tint)', 'var(--paper)', 'var(--ink)',
                                 'var(--rg-water)'), (prop, value)
    assert not re.search(r'#[0-9a-fA-F]{3,6}\b|rgba?\(', CSS)
    for mix in re.findall(r'--rg-water:\s*([^;]+);', CSS):
        assert re.fullmatch(r'color-mix\(in srgb, var\(--water\) \d+%, var\(--paper\)\)', mix.strip()), mix
    # night and dark-scheme get the same water so both themes read
    night = re.search(r':root\[data-theme="night"\] #raingauge \{([^}]*)\}', CSS).group(1)
    dark = re.search(r'prefers-color-scheme: dark\) \{\s*:root:not\(\[data-theme="paper"\]\):not\(\[data-theme="light"\]\) #raingauge \{([^}]*)\}', CSS).group(1)
    assert night.split() == dark.split()


def test_water_is_the_original_wash_without_hatching():
    # the gauge's original water: --water at 0.42 group opacity over the
    # tube's --water-tint, baked to one opaque mix per theme (paper, night,
    # dark scheme, in the order the tokens are defined)
    alphas = [float(a) for a in re.findall(r'--water-tint:\s*rgba\([^)]*,\s*([\d.]+)\)', HTML)]
    mixes = [int(p) for p in re.findall(r'--rg-water: color-mix\(in srgb, var\(--water\) (\d+)%', CSS)]
    assert len(alphas) == len(mixes) == 3
    for a, pct in zip(alphas, mixes):
        assert pct == round(100 * (1 - (1 - a) * (1 - 0.42))), (a, pct)
    assert re.search(r'\.gauge-fill\s+\{ fill: var\(--water-tint\); \}', HTML)
    # one flat wash for the body and its drifting cap, no engraved lines
    assert BUILD.count('class: "water-fill"') == 2
    for gone in ('water-hatch', 'waterHatch', 'rain-band-cut'):
        assert gone not in HTML, gone


def test_graduations_sit_over_the_water():
    block = BUILD[BUILD.index('for (var v = 1; v < RAIN_BANDS.length; v++)'):BUILD.index('rainPtr = el("g"')]
    assert 'class: "rain-band"' in block
    assert re.search(r'#raingauge \.rain-band\s+\{ stroke: var\(--ink\); stroke-width: 1; opacity: 0\.28; \}', HTML)
    # drawn after the water and the waterline, so nothing covers the scale
    assert BUILD.index('class: "water-g"') < BUILD.index('class: "rain-band"')
    assert BUILD.index('"imp-ring" + pat') < BUILD.index('class: "rain-band"')
