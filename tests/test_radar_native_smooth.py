"""Native dBZ interpolation, coverage, QC and bounded variant identities."""
from pathlib import Path

import numpy as np
import pytest

from lib import radar_level3 as l3, radar_mosaic as mosaic, radar_engine as engine
from lib.radar_palette import source_palette
from tests.test_radar_mosaic import scan, classes, classified  # noqa: F401
from tests.test_radar_level3 import tile_of, native  # noqa: F401
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401


def sample(s, bearing, distance):
    value = l3.interpolate_codes(s, np.array([bearing]), np.array([distance]))[0]
    return (value-2)/2-32 if value >= 2 else value


def test_bilinear_dbz_range_azimuth_centres_and_north():
    s = scan(106)  # 20 dBZ
    s.codes[0, 11] = 126  # 30
    s.codes[1, 10:12] = [146, 186]  # 40, 60
    assert sample(s, .25, 10.5) == 20
    assert sample(s, .25, 11) == 25
    assert sample(s, .5, 10.5) == 30
    assert sample(s, .5, 11) == 37.5
    s.codes[719, 10:12] = 146
    assert sample(s, 0, 10.5) == 30


def test_actual_nonuniform_ray_widths_and_gap():
    s = scan(106)
    s.bearing_index[:15] = [0]*5 + [1]*10
    s.codes[1] = 146
    assert sample(s, .5, 10.5) == pytest.approx(20+20/3)
    s.bearing_index[5] = -1  # never skip a missing sector
    assert sample(s, .49, 10.5) == 20
    assert sample(s, .55, 10.5) == 1


@pytest.mark.parametrize('missing', [0, 1])
def test_nonnumeric_corners_and_containing_gate_never_fill(missing):
    s = scan(146)
    s.codes[1, 11] = missing
    assert sample(s, .5, 11) == missing  # containing cell is authoritative
    assert sample(s, .4, 10.9) == 40  # no partial-stencil normalization
    s.codes[0, 10] = missing
    assert sample(s, .25, 10.5) == missing
    assert sample(s, .25, s.gates+.1) == 1
    assert sample(s, .25, .1) == 40  # range edge clamps to measured gate


def test_interpolation_precedes_floor_and_palette(monkeypatch):
    s = scan(96)  # 15 dBZ
    s.codes[0, 10] = 94  # 14 dBZ
    assert sample(s, .25, 10.99) < 15
    # Use the real sampler through mosaic and palette, at controlled geometry.
    def geometry(*args):
        shape = (256, 256)
        return ((slice(None), slice(None)), np.zeros(shape, np.uint16),
                np.full(shape, 10, np.uint16), np.zeros(shape, np.float32),
                np.full(shape, .25), np.full(shape, 10.99))
    monkeypatch.setattr(mosaic, '_geometry', geometry)
    x, y = map(int, tile_of(s.lat, s.lon, 10))
    image, visible = mosaic.render_mosaic([s], 10, x, y, source_palette('iem-nexrad-n0b'), smooth=True)
    assert visible == 0 and image.info['radarUncoveredPixels'] == 0
    image.close()
    s.codes[0, 10] = 96
    image, visible = mosaic.render_mosaic([s], 10, x, y, source_palette('iem-nexrad-n0b'), smooth=True)
    assert visible == 65536
    image.close()


def test_qc_and_measured_grid_stay_truthful():
    s = scan(146)
    h = classes(60)
    h.codes[:180, :200] = 150
    h.codes[180:, :100] = 10
    s = mosaic.quality_control(s, h)
    # Excluded clutter/folding stays missing; biological suppression stays clear.
    assert sample(s, .25, 20.5) == 1
    assert sample(s, 200.25, 20.5) == 0
    x, y = map(int, tile_of(s.lat, s.lon, 10))
    images = [mosaic.render_mosaic([s], 10, x, y, source_palette('iem-nexrad-n0b'), smooth=v)[0]
              for v in (False, True)]
    assert images[0].info == images[1].info
    for image in images:
        assert np.count_nonzero(np.array(image)) + image.info['radarUncoveredPixels'] <= 65536
        image.close()


def test_variants_separate_keys_revisions_and_shared_geometry_cap(monkeypatch):
    ctx = dict(native=True, attention='live')
    assert engine._radar_variant(ctx, 'iem-nexrad-n0b') == 'native'
    ctx['smooth'] = True
    assert engine._radar_variant(ctx, 'iem-nexrad-n0b') == 'native-smooth'
    assert engine._radar_variant(ctx, 'iem-mrms-lcref') is True
    assert len({engine._radar_render_revision(v) for v in engine.RADAR_RENDER_VARIANTS}) == 4
    args = ('iem-nexrad-n0b', 'KATX', 1789257600, 10, 164, 357)
    assert len({engine._radar_disk_key(*args, v) for v in engine.RADAR_RENDER_VARIANTS}) == 4
    assert len({engine._radar_tile_path(*args, v) for v in engine.RADAR_RENDER_VARIANTS}) == 4
    monkeypatch.setattr(mosaic, 'GEOMETRY_MAX_BYTES', 2*1024**2)
    mosaic.clear_geometry_cache()
    s = scan(); x, y = map(int, tile_of(s.lat, s.lon, 10))
    for smooth in (False, True, False, True):
        mosaic.render_mosaic([s], 10, x, y, source_palette('iem-nexrad-n0b'), smooth=smooth)[0].close()
        assert mosaic.geometry_cache_info()['bytes'] <= mosaic.GEOMETRY_MAX_BYTES
    mosaic.clear_geometry_cache()


def test_preference_acquisition_restart_and_variant_return(make_emitter, hybrid, multisite, classified):
    emitter = make_emitter(); emitter.radar._acquire()
    old = emitter.radar._result.frames[-1]['mosaicKey']
    marker = Path(emitter.radar.output_path).with_name('radar_smooth')
    marker.write_text('on')
    emitter.radar._acquire()
    r = emitter._build_payload()['radar']
    assert r['native'] and r['smooth'] and r['tiles']['variant'] == 'native-smooth'
    assert r['tiles']['remapRevision'] == l3.NATIVE_SMOOTH_REVISION
    assert r['tiles']['frames'][-1]['mosaicKey'] != old
    records = emitter.radar._disk_inventory.records
    variants = {k[-1] for k in records if k[0] == 'iem-nexrad-n0b'}
    assert {'native', 'native-smooth'} <= variants
    for key, record in records.items():
        if key[-1] == 'native-smooth':
            meta = engine._radar_tile_metadata(record[0], key[0])
            assert meta['revision'] == l3.NATIVE_SMOOTH_REVISION
            assert 'measuredGrid' in meta
    restart = make_emitter(); restart.radar._acquire()
    assert restart.radar._result.tiles['variant'] == 'native-smooth'
    assert restart.radar._result.frames[-1]['mosaicKey'] == r['tiles']['frames'][-1]['mosaicKey']
    marker.write_text('off'); restart.radar._acquire()
    assert restart.radar._result.tiles['variant'] == 'native'
    assert restart.radar._result.frames[-1]['mosaicKey'] == old


@pytest.mark.parametrize('zoom', [7, 10])
def test_smooth_worst_case_allocations_fit_render_admission(zoom):
    import tracemalloc
    candidates = [scan(height=i*200) for i in range(4)]
    x, y = map(int, tile_of(candidates[0].lat, candidates[0].lon, zoom))
    palette = source_palette('iem-nexrad-n0b')
    mosaic.clear_geometry_cache()
    tracemalloc.start()
    try:
        mosaic.render_mosaic(candidates, zoom, x, y, palette, smooth=True)[0].close()
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
        mosaic.clear_geometry_cache()
    assert peak <= mosaic.render_peak_bytes(512 if zoom == 7 else 256, 4, smooth=True)
    assert mosaic.RENDER_SLOT_COUNT * mosaic.RENDER_PEAK_BYTES <= mosaic.RENDER_TRANSIENT_BYTES


def test_render_admission_is_variant_and_contributor_specific():
    # The full non-Smooth 512px bound is exactly HEAD e084f78's 144 bytes per
    # sample, so the default path still has its two simultaneous render slots.
    plain = mosaic.render_peak_bytes(512, 4)
    assert plain == 1024**2 + 512**2 * 144
    assert mosaic.RENDER_TRANSIENT_BYTES // plain == 2

    smooth = mosaic.render_peak_bytes(512, 4, smooth=True)
    assert smooth == 1024**2 + 512**2 * 272
    assert smooth > mosaic.RENDER_TRANSIENT_BYTES // 2

    near, far = scan(), scan(lat=0)
    x, y = map(int, tile_of(near.lat, near.lon, 10))
    assert not mosaic._intersects(far, 10, x, y, 230000)
    assert mosaic.render_admission_bytes([near, far], 10, x, y) == mosaic.render_peak_bytes(256, 1)

    # Exercise admission without threads or timing: every successful claim is
    # included in reserved, and a claim that would cross the budget is refused.
    admission = mosaic._RenderAdmission(mosaic.RENDER_TRANSIENT_BYTES, workers=4)
    assert admission.acquire(plain, 0)
    assert admission.acquire(plain, 0)
    assert not admission.acquire(plain, 0)
    assert admission.reserved == 2*plain
    admission.release(plain); admission.release(plain)

    small_smooth = mosaic.render_peak_bytes(256, 4, smooth=True)
    assert admission.acquire(plain, 0)
    assert admission.acquire(small_smooth, 0)
    assert admission.acquire(small_smooth, 0)
    assert admission.reserved == plain + 2*small_smooth <= mosaic.RENDER_TRANSIENT_BYTES
    assert not admission.acquire(small_smooth, 0)
    admission.release(small_smooth); admission.release(small_smooth); admission.release(plain)


def test_both_native_variants_share_disk_limits_and_reject_cross_serving(tmp_path, monkeypatch):
    import shutil
    from lib.radar_cache import TileInventory
    from tests.test_freshness_health import _load_serve, _payload
    module = _load_serve(monkeypatch, tmp_path, _payload())
    monkeypatch.setattr(module.http.server.SimpleHTTPRequestHandler, 'send_head', lambda h: None)
    monkeypatch.setattr(engine, 'RADAR_DIR', str(tmp_path/'radar'))
    inventory = TileInventory()
    inventory.MAX_FILES = 2; inventory.MAX_BYTES = 6
    paths = []
    for index, variant in enumerate(engine.RADAR_RENDER_VARIANTS):
        site = mosaic.mosaic_key([('KATX', 1789257624, True)], engine._radar_render_revision(variant))
        args = ('iem-nexrad-n0b', site, 1789257600, 10, 164, 357, variant)
        path = engine._radar_tile_path(*args)
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'png')
        inventory.evict(incoming_size=3, incoming_files=1)
        inventory.add(engine._radar_disk_key(*args), path, 3, {})
        paths.append(path)
        assert inventory.bytes <= 6 and len(inventory) <= 2
    assert not paths[0].exists() and not paths[1].exists()
    assert paths[2].exists() and paths[3].exists()
    for variant, path in zip(('native', 'native-smooth'), paths[2:]):
        marker = tmp_path/'radar'/f'.{variant}-revision'
        marker.write_text(engine._radar_render_revision(variant))
        h = object.__new__(module.Handler); h.client_address = ('127.0.0.1', 1)
        h.directory = str(tmp_path); h.path = '/'+str(path.relative_to(tmp_path))
        h.send_error = lambda *args: None
        h.send_head(); assert h._immutable_radar
        marker.write_text('000000000000')
        h.send_head(); assert not h._immutable_radar
    # A PNG copied into the other variant's tree fails its internal revision.
    s = scan(); x, y = map(int, tile_of(s.lat, s.lon, 10))
    from PIL.PngImagePlugin import PngInfo
    image, visible = mosaic.render_mosaic([s], 10, x, y, source_palette('iem-nexrad-n0b'))
    import json
    info = PngInfo(); info.add_text('radarRemap', json.dumps(dict(revision=l3.NATIVE_REVISION, remapped=True)))
    image.save(paths[2], pnginfo=info); image.close()
    shutil.copyfile(paths[2], paths[3])
    with pytest.raises(ValueError, match='revision'):
        engine._radar_tile_metadata(paths[3], 'iem-nexrad-n0b')


@pytest.mark.parametrize('zoom', [8, 9, 10])
def test_storm_fixture_changes_intensity_not_coverage_or_palette(zoom):
    from tests.fixtures.radar_native_shaped import scans
    raw, hca = scans()
    s = mosaic.quality_control(raw, hca)
    x, y = map(int, tile_of(s.lat-.15, s.lon+.6, zoom))
    palette = source_palette('iem-nexrad-n0b')
    images = [mosaic.render_mosaic([s], zoom, x, y, palette, smooth=v)[0] for v in (False, True)]
    try:
        assert images[0].info == images[1].info
        assert np.count_nonzero(np.array(images[0]) != np.array(images[1])) > 0
        colours = {tuple(c) for _, c in palette}
        for image in images:
            with image.convert('RGBA') as rgba:
                assert all(c[3] == 0 or c in colours for _, c in rgba.getcolors(65536))
    finally:
        for image in images:
            image.close()
