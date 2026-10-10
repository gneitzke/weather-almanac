"""NEXRAD Level III reflectivity (153/N0B) and classification (165/N0H).

Fork-only. IEM's ridge tiles are resampled to roughly 1 km cells before we see
them; the radar's own product is 0.5 degree x 250 m. This module reads that
product exactly as NOAA distributes it (unidata-nexrad-level3 on AWS) and draws
256-pixel Web Mercator tiles from the polar cells, so a tile at zoom 10 shows
the radar's resolution instead of the resampled grid's.

Layout (NOAA ICD 2620001, verified against KATX 2026-09-25 03:42:24):
WMO header (two CRLF-terminated lines), an 18-byte message header, a 102-byte
product description block, then the symbology block, bzip2-compressed as a
whole. The symbology holds one digital radial packet (code 16): per radial a
start azimuth and width in tenths of a degree, and one byte per 250 m gate.
N0B gate codes: 0 below threshold, 1 range folded, n >= 2 is (n-2)/2 - 32 dBZ.
N0H uses categorical codes and its own description thresholds, with 1200 gates
and approximately one-degree rays. The independent bearing tables align QC.
"""
import bz2
import math
import re
import struct
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import numpy as np

PRODUCT_CODE = 153
GATE_METERS = 250.0
MAX_GATES = 1840
MAX_RAW_BYTES = 2 * 1024 * 1024
MAX_SYMBOLOGY_BYTES = 1_600_000  # a full 720 x 1840 grid is 1,329,150
EARTH_RADIUS_M = 6371000.0
EFFECTIVE_RADIUS_M = EARTH_RADIUS_M * 4 / 3  # standard refraction
SITE_TOLERANCE_DEG = 0.05
# The render identity of a native tile; bump when geometry or colouring changes.
NATIVE_REVISION = "level3-n0b-n0h-mosaic-v7"  # v6: radarUncoveredPixels tEXt; v7: radarMeasuredGrid tEXt
NATIVE_SMOOTH_REVISION = NATIVE_REVISION + "-dbz-bilinear-v1"


@lru_cache(maxsize=16)
def _azimuth_runs(bearing_bytes):
    """Actual contiguous ray footprints, including missing sectors and north.

    Bounded by 16 azimuth tables, independent of the number of scans/tiles.
    Half-bin edges put a five-bin ray's centre at 0.25, not 0.20 degrees.
    """
    table = np.frombuffer(bearing_bytes, np.int16)
    starts = np.flatnonzero(table != np.roll(table, 1))
    if not len(starts):
        starts = np.array([0])
    widths = (np.roll(starts, -1) - starts) % 3600
    widths[widths == 0] = 3600
    runs = (np.searchsorted(starts, np.arange(3600), side='right') - 1) % len(starts)
    return starts.astype(np.float32), widths.astype(np.float32), table[starts], runs.astype(np.int16)


def interpolate_codes(scan, bearing, ranges, containing=None):
    """Bilinear dBZ at polar sample positions (range in gate widths).

    Return fractional N0B codes so the shared step palette can be applied
    afterwards. Codes 0 (clear/QC suppressed) and 1 (missing/folded) have no
    numerical dBZ: retain the containing gate if any corner is nonnumeric,
    outside range, or separated by a missing azimuth. Never renormalize a
    partial stencil across a hole, nor fill a nonnumeric containing gate.
    """
    bearing = np.asarray(bearing, dtype=np.float32)
    ranges = np.asarray(ranges, dtype=np.float32)
    bins = (bearing * 10).astype(np.int32) % 3600
    if containing is None:
        rows, gates = scan.bearing_index[bins], ranges.astype(np.int32)
    else:
        # Integer cells come from the float64 projection, so compact float32
        # coordinates cannot move a coverage boundary or change the stencil ray.
        rows, gates, bins = containing
    inside = (rows >= 0) & (gates >= 0) & (gates < scan.gates)
    nearest = scan.codes[np.maximum(rows, 0), np.clip(gates, 0, scan.gates-1)]
    result = np.where(inside, nearest, 1).astype(np.float32)
    numeric = inside & (nearest >= 2)
    if not np.any(numeric):
        return result
    # Clear/missing footprints need no stencil. Compact before interpolation;
    # this is especially useful on the Pi for sparse echoes and disc edges.
    bearing, ranges, bins, rows = bearing[numeric], ranges[numeric], bins[numeric], rows[numeric]
    starts, widths, ray_rows, runs = _azimuth_runs(scan.bearing_index.astype(np.int16).tobytes())
    runs = runs[bins]
    delta = (bearing*10 - (starts[runs] + widths[runs]/2) + 1800) % 3600 - 1800
    adjacent = (runs + np.where(delta < 0, -1, 1)) % len(starts)
    other = ray_rows[adjacent]
    weight_a = np.abs(delta) / ((widths[runs] + widths[adjacent])/2)
    lower = np.floor(ranges - .5).astype(np.int32)
    weight_r = ranges - .5 - lower.astype(np.float32)
    valid = (other >= 0) & (lower >= 0) & (lower+1 < scan.gates)
    r0, r1 = np.maximum(rows, 0), np.maximum(other, 0)
    g0, g1 = np.clip(lower, 0, scan.gates-1), np.clip(lower+1, 0, scan.gates-1)
    corners = [scan.codes[r0, g0], scan.codes[r0, g1], scan.codes[r1, g0], scan.codes[r1, g1]]
    for corner in corners:
        valid &= corner >= 2
    # Decode before interpolation; no RGBA channel is ever averaged.
    a, b, c, d = [(v.astype(np.float32)-2)*.5-32 for v in corners]
    dbz = (a + (b-a)*weight_r)*(1-weight_a) + (c + (d-c)*weight_r)*weight_a
    result[numeric] = np.where(valid, (dbz+32)*2+2, nearest[numeric]).astype(np.float32)
    return result


class Scan:
    """One decoded elevation: codes indexed by 0.1-degree bearing, then gate."""
    __slots__ = ('lat', 'lon', 'height_m', 'elevation_deg', 'vcp', 'volume_ts',
                 'radials', 'gates', 'codes', 'bearing_index')

    def __init__(self, lat, lon, height_m, elevation_deg, vcp, volume_ts, codes, bearing_index):
        self.lat, self.lon, self.height_m = lat, lon, height_m
        self.elevation_deg, self.vcp, self.volume_ts = elevation_deg, vcp, volume_ts
        self.codes, self.bearing_index = codes, bearing_index
        self.radials, self.gates = codes.shape


def _julian_ts(day, seconds):
    # Day 1 is 1970-01-01 in the ICD's modified Julian date.
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(days=day - 1, seconds=seconds)).timestamp()


def decode(raw, expect_site=None, speckle_dbz=None):
    return _decode(raw, expect_site, speckle_dbz, 153)


def decode_n0h(raw, expect_site=None):
    """Decode product 165 using its own categorical description layout."""
    return _decode(raw, expect_site, None, 165)


def _decode(raw, expect_site, speckle_dbz, product):
    """Return a validated Scan. Every structural doubt raises ValueError.

    `expect_site` is (lat, lon); a product from a different radar is refused.
    `speckle_dbz` despeckles gates at or above that reflectivity.
    """
    if not isinstance(raw, (bytes, bytearray)) or not 200 < len(raw) <= MAX_RAW_BYTES:
        raise ValueError('level3 size')
    try:
        first = raw.index(b'\r\r\n')
        offset = raw.index(b'\r\r\n', first + 3) + 3
    except ValueError:
        raise ValueError('level3 WMO header') from None
    if offset > 64 or offset + 120 > len(raw):
        raise ValueError('level3 WMO header')
    code, _, _, length, _, _, blocks = struct.unpack('>hhIIhhh', raw[offset:offset + 18])
    if code != product:
        raise ValueError('level3 product %d is not %s' % (code, 'N0B' if product == 153 else 'N0H'))
    if length != len(raw) - offset or blocks != 3:
        raise ValueError('level3 message length')
    words = struct.unpack('>51h', raw[offset + 18:offset + 120])
    if words[0] != -1 or words[6] != product:
        raise ValueError('level3 description block')
    lat, lon = (v / 1000 for v in struct.unpack('>ii', raw[offset + 20:offset + 28]))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError('level3 site position')
    if expect_site is not None and (abs(lat - expect_site[0]) > SITE_TOLERANCE_DEG or
                                    abs(lon - expect_site[1]) > SITE_TOLERANCE_DEG):
        raise ValueError('level3 product is from another radar')
    if (product == 153 and words[21:24] != (-320, 5, 254) or
            product == 165 and (words[21:24] != (0, 0, 0) or words[26] != 255)):
        raise ValueError('level3 data thresholds')
    elevation = words[20] / 10
    # Every site swept on 2026-09-25 reported 0.5; a few mountain sites are
    # licensed for slightly negative lowest tilts, which the beam model handles.
    if not -1 <= elevation <= 2:
        raise ValueError('level3 elevation %.1f is not a lowest tilt' % elevation)
    day, seconds = struct.unpack('>HI', raw[offset + 40:offset + 46])
    if day == 0 or seconds >= 86400:
        raise ValueError('level3 volume time')
    volume_ts = _julian_ts(day, seconds)
    expanded = struct.unpack('>I', raw[offset + 102:offset + 106])[0]
    body = bytes(raw[offset + 120:])
    if body[:3] == b'BZh':
        if not 0 < expanded <= MAX_SYMBOLOGY_BYTES:
            raise ValueError('level3 uncompressed size')
        decompressor = bz2.BZ2Decompressor()
        try:
            body = decompressor.decompress(body, MAX_SYMBOLOGY_BYTES + 1)
        except OSError as error:
            raise ValueError('level3 invalid bzip2 stream') from error
        if len(body) != expanded or not decompressor.eof or decompressor.unused_data:
            raise ValueError('level3 decompressed size')
    if not 30 <= len(body) <= MAX_SYMBOLOGY_BYTES:
        raise ValueError('level3 symbology')
    divider, block_id, block_length, layers = struct.unpack('>hhIh', body[:10])
    layer_divider, layer_length = struct.unpack('>hI', body[10:16])
    if (divider != -1 or block_id != 1 or layers != 1 or layer_divider != -1 or
            block_length != len(body) or layer_length != len(body) - 16):
        raise ValueError('level3 symbology header')
    packet, first_gate, gates, _, _, _, radials = struct.unpack('>7h', body[16:30])
    if packet != 16 or first_gate != 0 or not 0 < gates <= (MAX_GATES if product == 153 else 1200) or not 300 <= radials <= 800:
        raise ValueError('level3 radial packet')
    codes = np.zeros((radials, gates), np.uint8)
    bearing_index = np.full(3600, -1, np.int16)
    cursor = 30
    previous_start, previous_width, swept = None, 0, 0
    for radial in range(radials):
        if cursor + 6 > len(body):
            raise ValueError('level3 truncated radial')
        count, start, width = struct.unpack('>3h', body[cursor:cursor + 6])
        cursor += 6
        if not 0 <= count <= gates or not 0 <= start < 3600 or not 1 <= width <= 20 or cursor + count + (count & 1) > len(body):
            raise ValueError('level3 radial header')
        # QC uses adjacent rows as adjacent azimuths. Reject duplicate or
        # scrambled rays even if their union happens to cover the circle.
        if previous_start is not None:
            step = (start - previous_start) % 3600
            swept += step
            if step == 0 or swept >= 3600 or step < previous_width - 1:
                raise ValueError('level3 radial order')
        previous_start, previous_width = start, width
        codes[radial, :count] = np.frombuffer(body, np.uint8, count, cursor)
        cursor += count + (count & 1)
        bearing_index[(start + np.arange(width)) % 3600] = radial
    if cursor != len(body):
        raise ValueError('level3 trailing radial data')
    if 3600 - swept < previous_width - 1:
        raise ValueError('level3 radial overlap')
    if (bearing_index < 0).mean() > .02:
        raise ValueError('level3 azimuth coverage')
    if speckle_dbz is not None:
        codes = despeckle(codes, floor_code(speckle_dbz))
    return Scan(lat, lon, words[5] * 0.3048, elevation, words[8], volume_ts, codes, bearing_index)


def code_dbz(code):
    return None if code < 2 else (code - 2) / 2 - 32


def despeckle(codes, floor_code):
    """Clear gates at or above the floor with fewer than two such neighbours.

    Lone gates are aircraft, birds and interference far more often than rain;
    a real shower spans many 250 m gates. Azimuth wraps; range does not.
    """
    strong = codes >= floor_code
    padded = np.pad(strong, ((1, 1), (1, 1)), mode='wrap')
    padded[:, 0] = padded[:, -1] = False
    rows, gates = strong.shape
    neighbours = sum(padded[1 + dr:1 + dr + rows, 1 + dg:1 + dg + gates].astype(np.uint8)
                     for dr in (-1, 0, 1) for dg in (-1, 0, 1) if dr or dg)
    cleaned = codes.copy()
    cleaned[strong & (neighbours < 2)] = 0
    return cleaned


def floor_code(dbz):
    return int(math.ceil((dbz + 32) * 2 + 2))


@lru_cache(maxsize=4)
def colour_table(palette):
    """Map gate code -> palette slot (0 transparent) and the slot colours.

    `palette` is the shared stepwise LUT ((floor dBZ, RGBA), ...): a gate takes
    the highest floor at or below its value, exactly like the IEM remap, so
    v1 and v2 draw identical colours for identical reflectivity.
    """
    floors = [float(f) for f, _ in palette]
    slots, colours = np.zeros(256, np.uint8), [(0, 0, 0, 0)]
    for code in range(2, 256):
        dbz = code_dbz(code)
        index = max((i for i, f in enumerate(floors) if f <= dbz), default=None)
        if index is None or palette[index][1][3] == 0:
            continue
        colour = tuple(palette[index][1])
        if colour not in colours:
            colours.append(colour)
        slots[code] = colours.index(colour)
    return slots, colours


def _tile_lonlat(z, x, y, size):
    frac = (np.arange(size) + .5) / size
    lon = (x + frac) / 2**z * 360 - 180
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * (y + frac) / 2**z))))
    return lat, lon


def gate_lookup(scan, z, x, y, size=256):
    """Radial row and gate for each pixel centre of a tile (-1 outside coverage)."""
    lat, lon = _tile_lonlat(z, x, y, size)
    la = np.radians(lat)[:, None]
    dl = np.radians(lon)[None, :] - math.radians(scan.lon)
    a = math.radians(scan.lat)
    hav = np.sin((la - a) / 2)**2 + math.cos(a) * np.cos(la) * np.sin(dl / 2)**2
    ground = 2 * EARTH_RADIUS_M * np.arcsin(np.minimum(1, np.sqrt(hav)))
    bearing = np.degrees(np.arctan2(np.sin(dl) * np.cos(la),
                                    math.cos(a) * np.sin(la) - math.sin(a) * np.cos(la) * np.cos(dl))) % 360
    # Ground distance -> slant range along the refracted beam (4/3 earth).
    central = ground / EFFECTIVE_RADIUS_M
    slant = EFFECTIVE_RADIUS_M * np.sin(central) / np.cos(math.radians(scan.elevation_deg) + central)
    gate = (slant / GATE_METERS).astype(np.int32)
    row = scan.bearing_index[(bearing * 10).astype(np.int32) % 3600].astype(np.int32)
    outside = (gate < 0) | (gate >= scan.gates) | (row < 0)
    gate[outside] = 0
    row[outside] = -1
    return row, gate


def render_tile(scan, z, x, y, palette, supersample=None):
    """Return (PIL 'P' image, visible pixel count) for one 256-pixel tile.

    At zoom 7 one pixel (~830 m at 47 N) spans several gates; each pixel then
    takes the strongest of a 2x2 sample so a narrow core is not lost between
    samples. From zoom 8 a pixel is about one gate, and 2x2 would quadruple
    the trigonometry (measured on the Pi 4: 105 ms against 28 ms per tile).
    """
    from PIL import Image
    slots, colours = colour_table(palette)
    factor = supersample or (2 if z < 8 else 1)
    row, gate = gate_lookup(scan, z, x, y, 256 * factor)
    codes = scan.codes[np.maximum(row, 0), gate]
    codes[row < 0] = 0
    if factor > 1:
        codes = codes.reshape(256, factor, 256, factor).max(axis=(1, 3))
    pixels = slots[codes]
    image = Image.fromarray(pixels, 'P')
    flat = [channel for colour in colours for channel in colour[:3]]
    image.putpalette(flat + [0] * (768 - len(flat)))
    image.info['transparency'] = bytes(colour[3] for colour in colours)
    return image, int(np.count_nonzero(pixels))


def s3_key_time(key, product="N0B"):
    """'ATX_N0B_2026_09_25_03_42_24' -> epoch seconds, or None."""
    try:
        name = key.rsplit('/', 1)[-1]
        if not re.fullmatch(r'[A-Z0-9]{3}_' + re.escape(product) + r'_[0-9]{4}(?:_[0-9]{2}){5}', name):
            return None
        parts = name.split('_')
        return datetime(*map(int, parts[2:]), tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def match_key(keys, stamp_ts, product="N0B"):
    """The product whose volume starts in the IEM scan's minute (IEM floors it)."""
    best = None
    for key in keys:
        ts = s3_key_time(key, product)
        if ts is not None and 0 <= ts - stamp_ts < 60:
            if best is None or abs(ts - stamp_ts - 30) < abs(best[0] - stamp_ts - 30):
                best = (ts, key)
    return best[1] if best else None
