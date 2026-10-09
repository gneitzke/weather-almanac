"""Both-theme deterministic paint-deadline collision (the v6.0 picker/refusal checks
were retired with the source picker)."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright
from tests.verify_radar_headless import radar_server
from tests.verify_radar_v59 import SCENARIO, context_page


def main():
    output = Path('/private/tmp/radar-v60-headless')
    output.mkdir(exist_ok=True)
    results = []
    # Force camera/tile damage to coincide with every playback deadline. The
    # real v5.9 compositor, retained pixels and adoption checks still run.
    scenario = SCENARIO.replace(
        '    radarFrame(clock);',
        '''    let echoCalls=0;const echo=radarEchoPaint;radarEchoPaint=(...args)=>{echoCalls++;return echo(...args);};
    if(radarView.nextAt&&clock>=radarView.nextAt)radarEchoDirty=true;
    radarFrame(clock);radarEchoPaint=echo;check(echoCalls<=1,'duplicate deadline repaint '+echoCalls);''')
    with radar_server() as server, sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--disable-gpu'])
        for theme in ('paper', 'night'):
            context, page = context_page(browser, server, theme)
            page.route('**/wx.json*', lambda route: route.abort())
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            result = dict(theme=theme, geometry=page.evaluate(scenario, 'zoom'))
            assert not errors, errors
            results.append(result)
            context.close()
            print(theme, 'RADAR V6.0 PASS: one deadline paint', flush=True)
        browser.close()
    (output/'assertions.json').write_text(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
