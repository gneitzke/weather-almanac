"""The motion budget's clock in a real Chromium (skipped where Playwright or its
browser is missing): the policy is tested in node (test_motion_budget_page.py);
this drives the page end to end against a scripted wx.json and checks what
only a browser can show. Chromium keeps a keyframe animation alive once script
has paused or played it, after its CSS stops naming it, and builds a twin when
the CSS names it again (found 2026-10-26), so the lifecycle is what matters:
the clock advances real time at its tier, hands animations back at full rate,
leaves no twins or orphans when parts fade, holds nothing off screen, goes
fully still under the power hold, and a LAN page ignores the Pi's CPU.
"""
import json
import time
from pathlib import Path

import pytest

from tests.test_radar_warnings_ux_page import BOOT, PAGE, browser  # noqa: F401

BASE = json.loads(Path('tests/fixtures/radar_engine_contract_payload.json').read_text())['wx.json']

STATE = '''(() => {
  const g = document.getElementById("raingauge"), a = g.getAnimations({ subtree: true });
  const all = [...g.querySelectorAll("*")];
  const keys = a.filter(x => x.animationName).map(x => x.animationName + "|" + all.indexOf(x.effect.target));
  return { fps: MOTION_FPS[motion.st.tier], reason: motion.st.reason, still: g.classList.contains("motion-still"),
           keyframes: keys.length, twins: keys.length - new Set(keys).size,
           clocked: a.filter(x => x._motion && x.playState === "paused").length,
           running: a.filter(x => x.playState === "running").length, loop: !!motion.raf,
           falls: a.filter(x => x.animationName === "rain-fall").length,
           livePlates: g.querySelectorAll(".rain-plate.live").length };
})()'''

ADVANCE = '''(async () => {
  const a = document.getElementById("raingauge").getAnimations({ subtree: true })
              .find(x => x.animationName === "rain-fall");
  const t0 = a.currentTime, w0 = performance.now();
  await new Promise(r => setTimeout(r, 1000));
  return (a.currentTime - t0) / (performance.now() - w0);
})()'''


class Feed:
    """The scripted engine: rain, the machine's load, and who the page is."""

    def __init__(self):
        self.mm, self.panel, self.ts, self.frozen = 30.0, '1', int(time.time()), False
        self.perf = {'cpu': 0.3, 'cores': 4, 'tempC': 55.0, 'undervolt': False, 'undervoltAgeSec': None}
        self.motion = []

    def payload(self):
        if not self.frozen:
            self.ts += 1
        d = dict(BASE, ts=self.ts, rainRateMm=self.mm, rainRateInstMm=self.mm, rainRate=round(self.mm / 25.4, 2),
                 rainUnit='in', rainToday=0.8, windSpd=6.0, windDir=250, windUnit='mph', perf=self.perf)
        return json.dumps(d)


def _open(browser, feed):  # noqa: F811
    html = PAGE.read_text()
    ctx = browser.new_context(viewport=dict(width=1024, height=600))
    page = ctx.new_page()

    def route(r):
        url = r.request.url
        if url.split('?')[0] == BOOT:
            return r.fulfill(status=200, content_type='text/html', body=html)
        if '/wx.json' in url:
            if 'motion=' in url:
                feed.motion.append(url.split('motion=')[1].split('&')[0])
            return r.fulfill(status=200, content_type='application/json', body=feed.payload(),
                             headers={'X-Radar-Panel': feed.panel, 'Cache-Control': 'no-store'})
        return r.fulfill(status=503, body='')

    page.route('**/*', route)
    page.goto(BOOT + '?theme=paper')
    # the policy's clocks, shortened so a whole climb fits in seconds
    page.evaluate('MOTION.dwellMs = 3000; MOTION.downMs = 2000; MOTION.backoffMs = [4000, 30000];')
    return ctx, page


def _until(page, pred, timeout=40):
    end = time.time() + timeout
    while True:
        s = page.evaluate(STATE)
        if pred(s) or time.time() > end:
            return s
        time.sleep(0.4)


def test_the_clock_through_every_tier(browser):  # noqa: F811
    feed = Feed()
    ctx, page = _open(browser, feed)
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        s = _until(page, lambda s: s['clocked'] > 0)
        assert s['fps'] == 30 and s['reason'] == 'start' and s['running'] == 0
        assert page.evaluate(ADVANCE) == pytest.approx(1, abs=0.08)    # fewer frames, same speed

        s = _until(page, lambda s: s['fps'] == 60)
        assert s['reason'] == 'headroom' and s['clocked'] == 0 and s['running'] == s['keyframes'] > 0
        assert page.evaluate(ADVANCE) == pytest.approx(1, abs=0.08)

        feed.perf = dict(feed.perf, cpu=0.97)
        s = _until(page, lambda s: s['fps'] == 20)
        assert s['reason'] == 'cpu' and s['running'] == 0 and s['twins'] == 0
        assert page.evaluate(ADVANCE) == pytest.approx(1, abs=0.08)

        feed.perf = dict(feed.perf, cpu=0.3, undervolt=True, undervoltAgeSec=0)
        s = _until(page, lambda s: s['fps'] == 0)
        assert s['reason'] == 'power' and s['still'] and s['keyframes'] == 0 and not s['loop']

        # the hold is the page's own: the engine forgetting the alarm does not lift it
        feed.perf = dict(feed.perf, undervolt=False, undervoltAgeSec=None)
        time.sleep(6)
        assert page.evaluate(STATE)['fps'] == 0
        page.evaluate('motion.st.powerUntil = 0;')                   # ten minutes later
        s = _until(page, lambda s: s['fps'] == 12 and s['clocked'] > 0)
        assert s['twins'] == 0 and not s['still']

        # a stalled engine's file served again is not new evidence
        feed.perf, feed.frozen = dict(feed.perf, cpu=0.99), True
        ema_before = page.evaluate('motion.st.ema')
        time.sleep(5)
        assert page.evaluate('motion.st.ema') == ema_before
        feed.frozen = False

        # a LAN page governs on its own frames: the Pi's 99 % is not its business
        feed.panel = '0'
        s = _until(page, lambda s: s['fps'] == 60, timeout=60)
        assert s['twins'] == 0

        # back on the panel and busy; then the rain eases and heavy plates fade out
        feed.panel, feed.perf = '1', dict(feed.perf, cpu=0.8)
        s = _until(page, lambda s: s['fps'] in (30, 20) and s['clocked'] > 0)
        page.evaluate('MOTION.dwellMs = 1e9; MOTION.downMs = 1e9;')
        feed.mm = 0.7
        s = _until(page, lambda s: s['livePlates'] <= 3 and s['falls'] == s['livePlates'], timeout=40)
        assert s['falls'] == s['livePlates'] and s['twins'] == 0            # no part moves unseen

        # off screen nothing ticks; back on screen the clock restarts without a poll
        page.evaluate('activate("s-moon")')
        s = page.evaluate(STATE)
        assert not s['loop'] and s['running'] == 0
        assert page.evaluate('[motion.st.active, motion.st.healthy]') == [False, None]   # the dwell starts over
        page.evaluate('activate("s-obs")')
        s = page.evaluate(STATE)
        assert s['loop'] and s['twins'] == 0

        feed.mm = 0.0
        s = _until(page, lambda s: not s['loop'] and s['keyframes'] == 0, timeout=60)
        assert s['keyframes'] == 0 and not s['loop']
        assert {'30:start', '60:headroom', '20:cpu', '0:power'} <= set(feed.motion)
        assert errors == []
    finally:
        ctx.close()
