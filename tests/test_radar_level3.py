"""v2 radar: NOAA Level III N0B decoding, polar tile drawing and the engine variant."""
import bz2
import io
import json
import math
import re
import struct
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import numpy as np
import pytest
from PIL import Image

from lib import almanac_emit as ae, radar_palette as rp
from lib import radar_level3 as l3
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401
from tests.test_freshness_health import _load_serve, _payload

SITE = (47.61, -122.33)
PALETTE = rp.source_palette('iem-nexrad-n0b')


def product(lat=SITE[0], lon=SITE[1], volume_ts=1789257600, codes=None, radials=720, start=0,
            width=5, gates=1840, code=153, thresholds=(-320, 5, 254), elevation=5, compress=True,
            expanded=None, layout=None):
    """A Level III N0B product in NOAA's distributed layout (as KATX sends it)."""
    codes = np.zeros((radials, gates), np.uint8) if codes is None else codes
    symbology = bytearray(struct.pack('>7h', 16, 0, gates, 0, 0, 999, radials))
    for radial in range(radials):
        row = bytes(codes[radial])
        symbology += struct.pack('>3h', len(row), (start + radial * width) % 3600, width) + row
        symbology += b'\0' * (len(row) & 1)
    layer = struct.pack('>hI', -1, len(symbology)) + symbology
    body = struct.pack('>hhIh', -1, 1, 10 + len(layer), 1) + layer
    day, seconds = divmod(int(volume_ts), 86400)
    words = [0] * 51
    words[0], words[5], words[6], words[7], words[8] = -1, 642, code, 2, 215
    words[11], words[14], words[19], words[20] = day + 1, day + 1, 1, elevation
    words[21:24] = thresholds
    pdb = bytearray(struct.pack('>51h', *words))
    pdb[2:10] = struct.pack('>ii', round(lat * 1000), round(lon * 1000))
    pdb[24:28] = struct.pack('>I', seconds)
    pdb[84:88] = struct.pack('>I', len(body) if expanded is None else expanded)
    payload = bz2.compress(bytes(body)) if compress else bytes(body)
    message = bytes(pdb) + payload
    header = struct.pack('>hhIIhhh', code, day + 1, seconds, 18 + len(message), 542, 0, 3)
    return b'SDUS56 KSEW 130002\r\r\nN0BNEA\r\r\n' + header + message


def destination(site, bearing, meters):
    a, b, t, d = math.radians(site[0]), math.radians(site[1]), math.radians(bearing), meters / l3.EARTH_RADIUS_M
    lat = math.asin(math.sin(a) * math.cos(d) + math.cos(a) * math.sin(d) * math.cos(t))
    lon = b + math.atan2(math.sin(t) * math.sin(d) * math.cos(a), math.cos(d) - math.sin(a) * math.sin(lat))
    return math.degrees(lat), math.degrees(lon)


def gate_code(dbz):
    return int(round((dbz + 32) * 2 + 2))


def tile_of(lat, lon, z):
    n = 2**z
    return ((lon + 180) / 360 * n,
            (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)


def test_decode_reads_layout_time_site_and_gates():
    codes = np.zeros((720, 1840), np.uint8)
    codes[180, 80:84] = gate_code(40)  # due east, 20-21 km
    scan = l3.decode(product(codes=codes, volume_ts=1789257624), expect_site=SITE)
    assert (scan.lat, scan.lon, scan.elevation_deg, scan.vcp) == (47.61, -122.33, .5, 215)
    assert scan.volume_ts == 1789257624 and scan.codes.shape == (720, 1840)
    assert scan.bearing_index[900] == 180 and scan.codes[180, 81] == gate_code(40)
    assert l3.code_dbz(gate_code(40)) == 40 and l3.code_dbz(0) is None and l3.code_dbz(1) is None


def test_uncompressed_and_unaligned_radials_decode():
    scan = l3.decode(product(start=3597, compress=False))
    assert scan.bearing_index[3597] == 0 and scan.bearing_index[1] == 0 and scan.bearing_index[2] == 1
    assert (scan.bearing_index >= 0).all()


@pytest.mark.parametrize('change,message', [
    (dict(code=94), 'not N0B'), (dict(thresholds=(-320, 10, 254)), 'thresholds'),
    (dict(lat=48.19), 'another radar'), (dict(elevation=35), 'lowest tilt'),
    (dict(gates=2000), 'radial packet'), (dict(expanded=5), 'decompressed size'),
    (dict(radials=100, width=5), 'radial packet'), (dict(radials=360, width=5), 'azimuth coverage')])
def test_structural_doubts_are_refused(change, message):
    with pytest.raises(ValueError, match=message):
        l3.decode(product(**change), expect_site=SITE)


@pytest.mark.parametrize('damage', ['short', 'no_header', 'truncated', 'length'])
def test_damaged_bytes_are_refused(damage):
    raw = product(compress=False)
    if damage == 'short': raw = raw[:150]
    elif damage == 'no_header': raw = raw.replace(b'\r\r\n', b'\n\n\n')
    elif damage == 'truncated': raw = raw[:-5000]
    else: raw = raw + b'\0'
    with pytest.raises(ValueError):
        l3.decode(raw)


def test_despeckle_clears_lone_gates_and_keeps_showers():
    codes = np.zeros((720, 1840), np.uint8)
    codes[10, 500] = gate_code(50)                 # a lone hit: aircraft, bird
    codes[400:403, 600:603] = gate_code(30)        # a 3x3 shower core
    codes[719, 700] = codes[0, 700] = gate_code(30)  # wraps across north
    codes[200, 900] = gate_code(10)                # below the floor: untouched
    cleaned = l3.despeckle(codes, l3.floor_code(15))
    assert cleaned[10, 500] == 0 and (cleaned[400:403, 600:603] == gate_code(30)).all()
    assert cleaned[200, 900] == gate_code(10)
    assert l3.floor_code(15) == gate_code(15)


def test_colours_match_the_v1_remap_for_the_same_reflectivity():
    slots, colours = l3.colour_table(PALETTE)
    source = 'iem-nexrad-n0b'
    indexed = rp._indexed_colors(source)
    for code in range(256):
        # IEM's N0B colour index i is (i/2 - 33) dBZ: the same scale as the gate code.
        image = Image.new('P', (1, 1))
        image.putpalette([v for color in indexed for v in color], rawmode='RGBA')
        image.putdata([code])
        with rp.remap(image, source, PALETTE) as mapped:
            v1 = mapped.getpixel((0, 0))
        v2 = colours[slots[code]]
        assert (v1[3] == 0 and v2[3] == 0) or v1 == v2, code
    assert colours[slots[0]][3] == colours[slots[1]][3] == colours[slots[gate_code(14.5)]][3] == 0
    assert colours[slots[gate_code(15)]][3] == 255


def test_tile_places_gates_at_their_ground_position():
    codes = np.zeros((720, 1840), np.uint8)
    codes[180, 150:170] = gate_code(45)  # bearing 90-90.5, 37.5-42.5 km slant
    scan = l3.decode(product(codes=codes))
    z = 10
    for km in (38, 42):
        lat, lon = destination(SITE, 90.25, km * 1000)
        fx, fy = tile_of(lat, lon, z)
        image, visible = l3.render_tile(scan, z, int(fx), int(fy), PALETTE)
        px, py = int((fx % 1) * 256), int((fy % 1) * 256)
        with image.convert('RGBA') as rgba:
            assert rgba.getpixel((px, py))[3] == 255, km
            assert rgba.getpixel((px, py))[:3] == dict(PALETTE)[45][:3]
        assert visible > 0
    far = tile_of(*destination(SITE, 90.25, 60000), z)
    image, visible = l3.render_tile(scan, z, int(far[0]), int(far[1]), PALETTE)
    assert visible == 0


def test_tile_beyond_range_is_empty_and_gates_follow_beam_geometry():
    scan = l3.decode(product(codes=np.full((720, 1840), gate_code(30), np.uint8)))
    row, gate = l3.gate_lookup(scan, 6, 9, 22)  # Seattle's z6 tile reaches 460 km+
    assert (row < 0).any() and (row >= 0).any()
    assert gate.max() < 1840
    # Refraction: 200 km on the ground is a slightly longer slant path at 0.5 degree.
    central = 200000 / l3.EFFECTIVE_RADIUS_M
    slant = l3.EFFECTIVE_RADIUS_M * math.sin(central) / math.cos(math.radians(.5) + central)
    assert 200000 < slant < 200400


def test_match_key_uses_the_iem_minute():
    stamp = datetime(2026, 9, 25, 3, 42, tzinfo=timezone.utc).timestamp()
    keys = ['ATX_N0B_2026_09_25_03_36_10', 'ATX_N0B_2026_09_25_03_42_24', 'ATX_N0H_2026_09_25_03_42_24', 'junk']
    assert l3.match_key(keys, stamp) == 'ATX_N0B_2026_09_25_03_42_24'
    assert l3.match_key(keys[:1], stamp) is None
    assert l3.s3_key_time('ATX_N0B_2026_13_25_03_42_24') is None


@pytest.fixture
def native(hybrid, multisite, monkeypatch, tmp_path):
    """S3 routes beside the IEM fixture: listings and products for each scan."""
    # Exercise the attended full mosaic; watch has separate primary/newest tests.
    monkeypatch.setattr(ae, 'RADAR_ATTENTION_MODE', 'active')
    hybrid.view()
    (tmp_path / 'radar_viewing').write_text(json.dumps(dict(since=hybrid.now, last=hybrid.now)))
    state = type('S3', (), {})()
    state.calls, state.missing, state.bad = [], set(), set()
    codes = np.zeros((720, 1840), np.uint8)
    codes[:, 40:400] = gate_code(35)  # a 10-100 km ring of moderate rain
    opened = ae.RadarSession.open
    def fetch(self, req, timeout):
        url = req.full_url
        if url.startswith(ae.RADAR_LEVEL3_BUCKET):
            if '?' in url:
                prefix = parse_qs(urlsplit(url).query)['prefix'][0]
                state.calls.append(('list', prefix))
                site = 'K' + prefix[:3]
                keys = ''.join('<Key>%s_N0B_%s</Key>' % (site[1:], datetime.fromtimestamp(ts + 24, timezone.utc).strftime('%Y_%m_%d_%H_%M_%S'))
                               for ts in multisite.scans[site] if ts not in state.missing)
                return io.BytesIO(('<ListBucketResult>%s</ListBucketResult>' % keys).encode())
            key = url.rsplit('/', 1)[1]
            state.calls.append(('get', key))
            site = 'K' + key[:3]
            ts = l3.s3_key_time(key)
            if ts in state.bad:
                return io.BytesIO(b'not a product' * 50)
            lat, lon, _ = ae._NEXRAD_SITES[site]
            return io.BytesIO(product(lat=lat, lon=lon, volume_ts=ts, codes=codes))
        return opened(self, req, timeout)
    monkeypatch.setattr(ae.RadarSession, 'open', fetch)
    monkeypatch.setattr(ae.AlmanacEmitter, '_radar_level3_down', hybrid.level3_down)
    monkeypatch.setattr(ae.AlmanacEmitter, '_radar_primary_only', staticmethod(hybrid.primary_only))
    return state


def test_v2_draws_site_scans_from_level3_once_per_scan(make_emitter, hybrid, multisite, native, tmp_path):
    hybrid.view()
    emitter = make_emitter(); emitter._do_radar()
    r = emitter._build_payload()['radar']
    assert r['available'] and r['siteId'] == 'KNEA' and r['native'] is True and r['smooth'] is False
    assert r['tiles']['variant'] == 'native' and r['tiles']['remapRevision'] == l3.NATIVE_REVISION
    assert r['tiles']['revision'] == ae._radar_render_revision('native')
    assert r['attribution'] == 'NOAA NEXRAD Level III'
    assert not [c for c in multisite.calls if c[0] == 'tile'], 'v2 never fetches IEM ridge tiles'
    gets = [c[1] for c in native.calls if c[0] == 'get']
    assert len(gets) == len(set(gets)), 'one download per scan, shared by every tile thread'
    records = {k: v for k, v in emitter._radar_disk_inventory.records.items() if k[0] == 'iem-nexrad-n0b'}
    assert records and all(k[-1] == 'native' and len(k) == 7 for k in records)
    # Region warming beside it stays v1: MRMS has no single radar to draw.
    assert all(len(k) == 6 for k in emitter._radar_disk_inventory.records if k[0] == 'iem-mrms-lcref')
    for key, (path, _, meta) in records.items():
        assert Path(path).parts[-7] == ae._radar_render_revision('native')
        assert ae._radar_tile_metadata(Path(path), key[0])['revision'] == l3.NATIVE_REVISION
    assert any(meta['weatherPixels'] for _, _, meta in records.values())
    # The page asks for exactly these tiles; the server marks the directory immutable.
    assert (Path(ae.RADAR_DIR) / '.native-revision').read_text() == r['tiles']['revision']

    native.calls.clear(); emitter._do_radar()
    assert not [c for c in native.calls if c[0] == 'get'], 'cached tiles are reused'

    restart = make_emitter(); restart._do_radar()
    assert records.keys() <= restart._radar_disk_inventory.records.keys(), 'native tiles survive a restart'

    emitter._radar_level3_fallback(ConnectionError('test outage')); emitter._do_radar()
    assert emitter._build_payload()['radar']['native'] is False
    assert emitter._radar_result.tiles['variant'] is False
    assert [c for c in multisite.calls if c[0] == 'tile']


def test_v2_leaves_region_unchanged(make_emitter, hybrid, multisite, native, tmp_path):
    (tmp_path / 'radar_source').write_text('mosaic')
    hybrid.view()
    emitter = make_emitter(); emitter._do_radar()
    r = emitter._build_payload()['radar']
    assert r['sourceId'] == 'iem-mrms-lcref' and r['native'] is False and r['tiles']['variant'] is False
    assert r['attribution'] == 'IEM / NOAA MRMS'
    # Warming the site view for an instant switch warms what the switch will show.
    records = emitter._radar_disk_inventory.records
    assert all(k[-1] == 'native' for k in records if k[0] == 'iem-nexrad-n0b')
    assert all(len(k) == 6 for k in records if k[0] == 'iem-mrms-lcref')


@pytest.mark.parametrize('failure', ['unpublished', 'corrupt'])
def test_a_missing_scan_is_not_requested_per_tile(make_emitter, hybrid, multisite, native, failure):
    (native.missing if failure == 'unpublished' else native.bad).add(hybrid.latest + 24 if failure == 'corrupt' else hybrid.latest)
    hybrid.view()
    emitter = make_emitter(); emitter._do_radar()
    newest = [c for c in native.calls if c[1].endswith(datetime.fromtimestamp(hybrid.latest + 24, timezone.utc).strftime('%H_%M_%S'))]
    listings = [c[1] for c in native.calls if c[0] == 'list']
    # Two products, two reporting sites, and the midnight hour boundary.
    assert len(listings) <= 8 and len(listings) == len(set(listings))
    assert len(newest) <= 1
    failed = emitter._radar_level3_failed[('KNEA', hybrid.latest)]
    assert failed[0] > ae.time.monotonic()
    assert not emitter._radar_level3_flights


def test_native_tiles_are_immutable():
    source = Path('design/almanac/kiosk/serve.py').read_text()
    assert "revision('native',tile[1])" in source
