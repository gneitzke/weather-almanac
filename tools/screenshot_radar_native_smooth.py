"""Offline native Smooth comparisons at z8/9/10 in both production themes.

python3 tools/screenshot_radar_native_smooth.py --output /path/to/scratchpad/smooth --prepare-only
python3 tools/screenshot_radar_native_smooth.py --output /path/to/scratchpad/smooth

The second command requires Playwright + Chromium outside the sandbox. All
browser requests are intercepted and fulfilled from generated files; there is
no server, socket, provider request, or LAN access. No browser install is run.
"""
import argparse
import io
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit

from PIL import Image
from PIL.PngImagePlugin import PngInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import radar_basemap as basemap, radar_engine as engine, radar_mosaic as mosaic
from lib.radar_palette import source_palette
from lib.radar_geometry import world_point
from tools.benchmark_radar_native_smooth import inputs


def prepare(output, n0b=None, n0h=None):
    from tests import conftest  # noqa: F401; headless Kivy stub for fixture payload
    from lib.almanac_emit import AlmanacEmitter
    from tests.fixtures.config import make_config
    output.mkdir(parents=True, exist_ok=True)
    root = output/'site'; root.mkdir(exist_ok=True)
    (root/'index.html').write_text(Path('design/almanac/console_live.html').read_text())
    app = SimpleNamespace(config=make_config(), obsParser=SimpleNamespace(api_data={}))
    data = AlmanacEmitter(SimpleNamespace(app=app, Obs={}, Met={}, Astro={}, Sager={}))._build_payload()
    scan = inputs(n0b, n0h)
    center = dict(lat=scan.lat-.15, lon=scan.lon+.6)
    source = 'iem-nexrad-n0b'; palette = source_palette(source)
    stamp = engine._radar_stamp_text(scan.volume_ts)
    cases = []
    for z in (8, 9, 10):
        px, py = world_point(center['lat'], center['lon'], z)
        left, top = round(px-478), round(py-245)
        x0, y0 = math.floor(left/256), math.floor(top/256)
        x1, y1 = math.ceil((left+956)/256), math.ceil((top+490)/256)
        grid = dict(x0=x0, y0=y0, w=x1-x0, h=y1-y0)
        mask = format((1 << ((x1-x0)*(y1-y0)))-1, 'x')
        for smooth in (False, True):
            variant = 'native-smooth' if smooth else 'native'
            revision = engine._radar_render_revision(variant)
            remap = engine._radar_variant_revision(variant)
            key = mosaic.mosaic_key([('KATX', scan.volume_ts, True)], revision)
            echo = Image.new('RGBA', ((x1-x0)*256, (y1-y0)*256))
            for y in range(y0, y1):
                for x in range(x0, x1):
                    image, visible = mosaic.render_mosaic([scan], z, x, y, palette, smooth=smooth)
                    info = PngInfo()
                    info.add_text('radarRemap', json.dumps(dict(remapped=True, unmatchedColors=0,
                        opaqueColors=len(image.getcolors()), unmatchedPixels=0, opaquePixels=visible,
                        ambiguousPixels=0, revision=remap)))
                    info.add_text('radarVisiblePixels', str(visible))
                    for field in ('radarUncoveredPixels', 'radarMeasuredGrid'):
                        info.add_text(field, str(image.info[field]))
                    path = root/f'radar/t/{revision}/{source}/{key}/{stamp}/{z}/{x}/{y}.png'
                    path.parent.mkdir(parents=True, exist_ok=True); image.save(path, pnginfo=info)
                    echo.paste(image.convert('RGBA'), ((x-x0)*256, (y-y0)*256)); image.close()
            for theme in ('paper', 'night'):
                base = Image.new('RGBA', echo.size)
                for y in range(y0, y1):
                    for x in range(x0, x1):
                        raw = basemap.tile(theme, z, x, y)
                        path = root/f'radar/geo/{basemap.version()}/{theme}/{z}/{x}/{y}.png'
                        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
                        with Image.open(io.BytesIO(raw)) as image:
                            base.paste(image.convert('RGBA'), ((x-x0)*256, (y-y0)*256))
                base.alpha_composite(echo)
                ox, oy = left-x0*256, top-y0*256
                base.crop((ox, oy, ox+956, oy+490)).save(output/f'preview-{theme}-z{z}-{"on" if smooth else "off"}.png')
                base.close()
            echo.close()
            site = dict(id='KATX', ts=scan.volume_ts, volumeTs=scan.volume_ts, filtered=True)
            frame = dict(ts=scan.volume_ts, stamp=stamp, at='Fixture', mosaicKey=key,
                         siteScans=[site], levels={str(z):True}, complete=True)
            radar = dict(available=True, reason=None, native=True, smooth=smooth, sourceId=source,
                sourceMode='site', siteId='KATX', sites=[], center=center, zoom=z, zoomDesired=z,
                zoomAuto=False, zoomAutoLevel=z, zoomMin=7, zoomMax=10, zoomSource='NOAA Level III',
                frameCount=1, completeFrameCount=1, ageSec=0, stale=False, staleSec=600, cadenceSec=120,
                observedTs=scan.volume_ts, latestTs=scan.volume_ts, historySpanSec=0,
                attribution='NOAA Level III · synthetic storm-shaped fixture' if n0b is None else 'Local Level III fixture',
                legend=engine._RADAR_RAMP, refresh=dict(state='idle'), rings=[],
                nexrad=dict(id='KATX', name='Fixture', distanceDisp='', bearing=''),
                geo=dict(version=basemap.version(), base='radar/geo/'),
                tiles=dict(base='radar/t/', revision=revision, remapRevision=remap,
                    smooth=smooth, variant=variant, tileSize=256, source=source, site=key, z=z,
                    levels=[z], grid=grid, camera=dict(center, zoom=z),
                    newest=dict(stamp=stamp, mask=mask, expectedMask=mask, completeMask=mask), frames=[frame]))
            case = f'z{z}-{"on" if smooth else "off"}'
            (root/f'{case}.json').write_text(json.dumps(dict(data, radar=radar, ts=scan.volume_ts+60)))
            cases.append(case)
    (output/'fixture.json').write_text(json.dumps(dict(cases=cases,
        description='Synthetic storm-shaped native polar data; not observed weather' if n0b is None else 'Local Level III products',
        zooms=[8,9,10], themes=['paper','night']), indent=2)+'\n')
    return root, cases


def _launch(p, chrome=None):
    """Playwright's bundled Chromium, else an explicit or installed Chrome."""
    if chrome:
        return p.chromium.launch(headless=True, executable_path=str(chrome))
    try:
        return p.chromium.launch(headless=True)
    except Exception:
        return p.chromium.launch(headless=True, channel='chrome')


def screenshots(root, output, cases, chrome=None):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = _launch(p, chrome)
        for case in cases:
            payload = (root/f'{case}.json').read_bytes()
            on = case.endswith('-on')
            for theme in ('paper', 'night'):
                context = browser.new_context(viewport=dict(width=1024, height=600), reduced_motion='reduce')
                def route(request):
                    path = urlsplit(request.request.url).path
                    if path == '/wx.json':
                        return request.fulfill(body=payload, content_type='application/json',
                                               headers={'X-Radar-Smooth':'on' if on else 'off'})
                    file = root/('index.html' if path == '/' else path.lstrip('/'))
                    if file.is_file():
                        kind = 'text/html' if file.suffix == '.html' else 'image/png' if file.suffix == '.png' else 'application/json'
                        return request.fulfill(body=file.read_bytes(), content_type=kind)
                    request.fulfill(status=404, body='fixture resource absent')
                context.route('**/*', route)
                page = context.new_page(); errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'https://radar-fixture.invalid/?tabs=1&theme={theme}')
                page.locator('.tab[data-screen="s-radar"]').click()
                page.wait_for_function('radarView.good && radarView.good.bitmap && !radarView.holdingWindow', timeout=30000)
                page.wait_for_function('radarGeoBusy === 0 && radarGeoQueue.length === 0')
                assert page.locator('#rad-smooth').get_attribute('aria-disabled') == 'false'
                assert page.locator('#rad-smooth').get_attribute('aria-pressed') == str(on).lower()
                assert not errors, errors
                label = json.loads((output/'fixture.json').read_text())['description']
                page.evaluate("text => document.getElementById('rad-status').textContent = text", label)
                page.screenshot(path=str(output/f'{theme}-{case}.png'))
                context.close()
        browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--n0b', type=Path)
    parser.add_argument('--n0h', type=Path)
    parser.add_argument('--chrome', type=Path, help='Chrome/Chromium executable if Playwright has no bundled browser')
    args = parser.parse_args()
    root, cases = prepare(args.output, args.n0b, args.n0h)
    if not args.prepare_only:
        screenshots(root, args.output, cases, args.chrome)
    print('Prepared native tiles, payloads and 12 map previews:', args.output)
    print('Browser screenshots pending outside sandbox.' if args.prepare_only else 'Saved 12 production-page screenshots.')


if __name__ == '__main__':
    main()
