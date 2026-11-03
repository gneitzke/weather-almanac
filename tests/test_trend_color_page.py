"""The temperature trend's colour follows its direction: a falling temperature
shows its arrow and per-hour rate in the water blue, a rising one keeps the
vermilion accent. The snippet runs in node exactly as the page ships it, and
uses the same threshold as the Rising/Falling word, so the two never disagree."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = Path('design/almanac/console_live.html').read_text()
SNIPPET = HTML[HTML.index('/* temperature trend arrow + word */'):HTML.index('/* 24h delta sentence */')]

needs_node = pytest.mark.skipif(shutil.which('node') is None, reason='node not installed')


@needs_node
def test_falling_is_blue_and_everything_else_keeps_the_accent():
    script = r'''
      function isNum(v) { return typeof v === "number" && isFinite(v); }
      var DASH = "—";
      var trend = { cls: {}, classList: { toggle: function (c, on) { trend.cls[c] = !!on; } } };
      var els = { "trend-arrow": { textContent: "", closest: function () { return trend; } },
                  "trend-word": { textContent: "" } };
      function $(id) { return els[id]; }
      var out = {};
      [-1.8, -0.06, -0.05, 0, 0.3, 4.6, null].forEach(function (v) {
        var data = { tempTrendPerHr: v };
        SNIPPET
        out[String(v)] = [els["trend-word"].textContent, els["trend-arrow"].textContent, trend.cls.falling];
      });
      console.log(JSON.stringify(out));
    '''.replace('SNIPPET', SNIPPET)
    r = subprocess.run(['node'], input=script, text=True, capture_output=True, timeout=10)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got['-1.8'] == ['Falling', '↓', True]
    assert got['-0.06'] == ['Falling', '↓', True]
    assert got['-0.05'] == ['Steady', '→', False]     # the word's own threshold: no blue on "Steady"
    assert got['0'] == ['Steady', '→', False]
    assert got['0.3'] == ['Rising', '↑', False]
    assert got['4.6'] == ['Rising', '↑', False]
    assert got['null'] == ['—', '', False]           # no reading: no colour claim either


def test_the_colours_come_from_the_theme_tokens():
    css = re.sub(r'/\*.*?\*/', '', HTML, flags=re.S)
    assert '.trend .arrow { color: var(--accent);' in css
    assert '.trend-main b { color: var(--accent);' in css
    assert '.trend.falling .arrow, .trend.falling .trend-main b { color: var(--water); }' in css
    # --water is defined for the paper theme and both night paths, like --accent
    assert css.count('--water:') >= 3 and css.count('--accent:') >= 3
