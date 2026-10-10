"""Use pinned native IEM palettes, including PNG tRNS, rather than green-only fixtures."""
import io
import urllib.error

import pytest
from PIL import Image

from lib import almanac_emit as ae, radar_palette as rp
from lib import radar_engine
from lib.radar_geometry import world_inverse, world_point
from tests.test_radar_hybrid import hybrid, png  # noqa: F401
from tests.test_radar_attention_engine import active, tier  # noqa: F401


def native_png(source, dbz):
    index = int(2*(dbz + (33 if source == 'iem-nexrad-n0b' else 32)))
    colors = rp._indexed_colors(source)
    im = Image.new('P', (256, 256), index)
    im.putpalette([v for c in colors for v in c[:3]])
    im.info['transparency'] = bytes(c[3] for c in colors)
    out = io.BytesIO(); im.save(out, 'PNG')
    return out.getvalue()


@pytest.mark.parametrize('dbz,echo', [(-10, False), (5, False), (15, False), (24.5, False), (30, True)])
def test_sentinel_uses_reflectivity_not_native_alpha(make_emitter, hybrid, active, dbz, echo):
    e = make_emitter(); tier(e, 'rest')
    hybrid.tile = native_png('iem-mrms-lcref', dbz)
    e.radar._acquire(intent_triggered=False)
    assert e.radar._sentinel['echo'] is echo
    assert e.radar._sentinel['complete']
    assert len(e.radar._request_times) == 6  # listing, metadata, four tiles


@pytest.mark.parametrize('bad', ['red', 'corrupt', 'missing', 'unknown-color'])
def test_bad_or_missing_sentinel_tiles_are_unknown(make_emitter, hybrid, active, bad):
    e = make_emitter(); tier(e, 'rest')
    hybrid.tile = png((255, 0, 0, 255)) if bad == 'red' else b'bad' if bad == 'corrupt' else png((1, 2, 3, 255))
    if bad == 'missing':
        def fail(req, timeout):
            if 'mrms::' in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 404, 'missing', {}, None)
        hybrid.failure = fail
    e.radar._acquire(intent_triggered=False)
    assert e.radar._sentinel['echo'] is None
    assert not e.radar._sentinel['complete']


def test_stale_sentinel_metadata_cannot_refresh_weather_hold(make_emitter, hybrid, active):
    e = make_emitter(); tier(e, 'rest')
    hybrid.latest -= 7200
    e.radar._acquire(intent_triggered=False)
    assert e.radar._sentinel is None
    assert not any('mrms::' in c[2] for c in hybrid.calls)


@pytest.mark.parametrize('dbz,echo', [(5, False), (20, False), (30, True)])
def test_frame_echo_and_reloaded_inventory_exclude_low_dbz(make_emitter, hybrid, active, dbz, echo):
    e = make_emitter(); tier(e, 'watch', hour=2)
    hybrid.tile = native_png('iem-mrms-lcref', dbz)
    e.radar._acquire(intent_triggered=False)
    assert e.radar._result.frames[-1]['echo'] is echo
    path, _, metadata = next(iter(e.radar._disk_inventory.records.values()))
    assert metadata['weatherPixels'] == radar_engine._radar_tile_metadata(path, 'iem-mrms-lcref')['weatherPixels']


@pytest.mark.parametrize('dbz', [7, 12.5])
def test_clear_air_and_sub_floor_returns_are_neither_drawn_nor_counted(dbz):
    # 2026-09-24: below DISPLAY_FLOOR_DBZ (15) the panel draws nothing (clutter, insects).
    with Image.open(io.BytesIO(native_png('iem-nexrad-n0b', dbz))) as native:
        with rp.remap(native, 'iem-nexrad-n0b', rp.source_palette('iem-nexrad-n0b')) as mapped:
            assert mapped.getchannel('A').getextrema()[1] == 0
            assert rp.weather_pixels(mapped) == 0


@pytest.mark.parametrize('offset', [0.1, 127.9, 128.1, 255.9])
def test_sentinel_four_tiles_surround_home_at_tile_edges(make_emitter, hybrid, offset):
    e = make_emitter(); e.radar._session = radar_engine.RadarSession()
    z = radar_engine.RADAR_SENTINEL_ZOOM
    home = world_inverse(5*256+offset, 11*256+offset, z)
    e.radar._session.begin_pass(100)
    e.radar._sentinel_pass(dict(station=home, deadline=100))
    urls = [c[2] for c in hybrid.calls if 'mrms::' in c[2]]
    coords = [tuple(map(int, u.removesuffix('.png').split('/')[-2:])) for u in urls]
    px, py = world_point(*home, z)
    assert len(set(coords)) == 4
    assert min(x for x, y in coords)*256 <= px-127
    assert (max(x for x, y in coords)+1)*256 >= px+127
    assert min(y for x, y in coords)*256 <= py-127
    assert (max(y for x, y in coords)+1)*256 >= py+127


def test_sentinel_obeys_request_budget(make_emitter, hybrid, active):
    e = make_emitter(); tier(e, 'rest')
    e.radar._request_times = [ae.time.monotonic()] * (radar_engine.RADAR_REQUESTS_PER_MIN-3)
    e.radar._acquire(intent_triggered=False)
    assert len(e.radar._request_times) <= radar_engine.RADAR_REQUESTS_PER_MIN
    assert len(hybrid.calls) <= 3
    assert e.radar._sentinel is None or e.radar._sentinel['echo'] is not False
