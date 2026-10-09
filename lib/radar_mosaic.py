"""Immutable native frame inputs and lowest-beam, categorical-QC mosaics.

Blockage occurs along each beam path. Below-floor returns fall through to
higher filtered echoes; filtered clear excludes higher unfiltered clutter.
"""
import hashlib
import json
import math
import os
import re
import time
from collections import OrderedDict
from threading import BoundedSemaphore, RLock

import numpy as np

from lib.radar_level3 import (Scan, NATIVE_REVISION, EARTH_RADIUS_M,
    EFFECTIVE_RADIUS_M, GATE_METERS, _tile_lonlat, colour_table, floor_code)
from lib.radar_palette import DISPLAY_FLOOR_DBZ
from lib.radar_geometry import (circle_intersects_bounds, world_inverse,
                                EARTH_RADIUS_METERS)

# A render has one cold projection at a time (at most twelve float64
# sample arrays, including ufunc temporaries), plus 12 bytes/sample/candidate
# for preceding projections and sampled codes. Ownership sorting separately
# needs 26 bytes/sample/candidate plus 16 bytes/sample for masks and indices.
# Charge cold geometry
# even if it will be admitted to the independent retained cache. The fixed MiB
# covers the 256px indexed Pillow output, palette and small Python allocations.
RENDER_MAX_CANDIDATES = 4
RENDER_TRANSIENT_BYTES = 80_000_000


def render_peak_bytes(size, candidates):
    return 1024**2 + size*size*max(96 + 12*candidates, 16 + 26*candidates)


RENDER_PEAK_BYTES = render_peak_bytes(512, RENDER_MAX_CANDIDATES)
RENDER_SLOT_COUNT = max(1, min(os.cpu_count() or 1, RENDER_TRANSIENT_BYTES // RENDER_PEAK_BYTES))
_RENDER_SLOTS = BoundedSemaphore(RENDER_SLOT_COUNT)
GEOMETRY_MAX_BYTES = 48 * 1024 * 1024
_GEOMETRY = OrderedDict()
_GEOMETRY_LOCK = RLock()
_GEOMETRY_BYTES = _GEOMETRY_HITS = _GEOMETRY_MISSES = 0


def clear_geometry_cache():
    global _GEOMETRY_BYTES, _GEOMETRY_HITS, _GEOMETRY_MISSES
    with _GEOMETRY_LOCK:
        _GEOMETRY.clear()
        _GEOMETRY_BYTES = _GEOMETRY_HITS = _GEOMETRY_MISSES = 0


def geometry_cache_info():
    with _GEOMETRY_LOCK:
        return dict(bytes=_GEOMETRY_BYTES, hits=_GEOMETRY_HITS,
                    misses=_GEOMETRY_MISSES, entries=len(_GEOMETRY))


def mosaic_key(pairs, revision=NATIVE_REVISION):
    """Pairs are (site, exact N0B volume second, has N0H), never arrival order."""
    wire = json.dumps([revision, sorted(pairs)], separators=(',', ':'))
    return 'M' + hashlib.sha256(wire.encode()).hexdigest()[:24]


def quality_control(scan, classification):
    """Apply HCA to N0B gate centres using each product's actual azimuth table.

    Code 1 is our existing no-data/range-folded sentinel. Biological becomes
    code 0 unless embedded in precipitation. Missing HCA bearings/range retain N0B.
    """
    if classification is None:
        return scan
    if scan.volume_ts != classification.volume_ts:
        raise ValueError('N0H volume differs from N0B')
    codes = scan.codes.copy()
    # Circular mean handles the ray straddling north without assuming row//2.
    bearings = np.arange(3600) * np.pi / 1800
    valid = scan.bearing_index >= 0
    rows = scan.bearing_index[valid]
    sine = np.bincount(rows, weights=np.sin(bearings[valid]), minlength=scan.radials)
    cosine = np.bincount(rows, weights=np.cos(bearings[valid]), minlength=scan.radials)
    bearing = (np.degrees(np.arctan2(sine, cosine)) % 360 * 10).round().astype(int) % 3600
    hrow = classification.bearing_index[bearing]
    # Both products use 250 m slant gates at the same lowest tilt.
    if abs(scan.elevation_deg - classification.elevation_deg) > .01:
        raise ValueError('N0H elevation differs from N0B')
    gates = min(scan.gates, classification.gates)
    classes = classification.codes[np.maximum(hrow, 0), :gates]
    target = codes[:, :gates]
    present = hrow[:, None] >= 0
    target[present & ((classes == 20) | (classes == 150))] = 1
    # Never turn N0B range folding into a valid clear measurement.
    # Wrap azimuth, never range: the outermost gates are not neighbours of
    # gates beside the antenna. Missing/outside gates count as non-precipitation
    # in the fixed 45-cell neighbourhood. Sum separably to avoid a large window.
    precipitation = (classification.codes >= 30) & (classification.codes <= 120)
    radial_sum = sum(np.roll(precipitation.astype(np.uint8), shift, axis=0)
                     for shift in range(-2, 3))
    padded = np.pad(radial_sum, ((0, 0), (4, 4)))
    neighbours = sum(padded[:, shift:shift+classification.gates] for shift in range(9))
    embedded = neighbours[np.maximum(hrow, 0), :gates] > 22
    target[present & (classes == 10) & ~embedded & (target != 1)] = 0
    return Scan(scan.lat, scan.lon, scan.height_m, scan.elevation_deg, scan.vcp,
                scan.volume_ts, codes, scan.bearing_index)


def _intersects(scan, z, x, y, radius):
    north, west = world_inverse(x*256, y*256, z)
    south, east = world_inverse((x+1)*256, (y+1)*256, z)
    # The shared bounds helper uses the mean-earth radius; projection uses the
    # Level III radius. Scale the angular cap so the cull cannot lose an edge.
    return circle_intersects_bounds(scan.lat, scan.lon,
        radius * EARTH_RADIUS_METERS / EARTH_RADIUS_M,
        dict(n=north, s=south, w=west, e=east))


def _geometry(site, z, x, y, size, radius, cache_geometry=True):
    """Volume-independent bearing bin, gate and beam height, byte-bounded LRU.

    Cache bearing bins, not radial rows: azimuth tables can differ by volume.
    Compute gate boundaries in float64 before compacting the integer result.
    """
    global _GEOMETRY_BYTES, _GEOMETRY_HITS, _GEOMETRY_MISSES
    key = (*site, z, x, y, size, radius)
    with _GEOMETRY_LOCK:
        cached = _GEOMETRY.get(key)
        if cached is not None:
            if cache_geometry:
                _GEOMETRY.move_to_end(key)
            _GEOMETRY_HITS += 1
            return cached
        _GEOMETRY_MISSES += 1
    lat, lon = _tile_lonlat(z, x, y, size)
    la, lo = np.radians(lat)[:, None], np.radians(lon)[None, :]
    a, b, height, elevation = site
    a, b, elevation = map(math.radians, (a, b, elevation))
    dl = lo-b
    hav = np.sin((la-a)/2)**2 + math.cos(a)*np.cos(la)*np.sin(dl/2)**2
    ground = 2*EARTH_RADIUS_M*np.arcsin(np.minimum(1, np.sqrt(hav)))
    # Store only the rectangular sampled footprint of the disc in this tile.
    # Cropping is lossless: outside it every beam height would be infinity.
    covered = ground <= radius
    row = np.flatnonzero(covered.any(axis=1))
    col = np.flatnonzero(covered.any(axis=0))
    if not len(row) or not len(col):
        return None
    region = (slice(int(row[0]), int(row[-1])+1), slice(int(col[0]), int(col[-1])+1))
    ground = ground[region].copy()
    la, dl = la[region[0]], dl[:, region[1]]
    del hav, covered
    central = ground/EFFECTIVE_RADIUS_M
    slant = EFFECTIVE_RADIUS_M*np.sin(central)/np.cos(elevation+central)
    altitude = height + (EFFECTIVE_RADIUS_M + slant*np.sin(elevation))/np.cos(central)-EFFECTIVE_RADIUS_M
    altitude[ground > radius] = np.inf
    bearing = np.degrees(np.arctan2(np.sin(dl)*np.cos(la),
        math.cos(a)*np.sin(la)-math.sin(a)*np.cos(la)*np.cos(dl))) % 360
    gates = np.clip(slant/GATE_METERS, 0, 32767).astype(np.uint16)
    bins = ((bearing*10).astype(np.uint16) % 3600)
    cached = (region, bins, gates, altitude.astype(np.float32))
    for array in cached[1:]:
        array.flags.writeable = False
    length = sum(array.nbytes for array in cached[1:])
    with _GEOMETRY_LOCK:
        if cache_geometry and key not in _GEOMETRY and length <= GEOMETRY_MAX_BYTES:
            while _GEOMETRY and _GEOMETRY_BYTES + length > GEOMETRY_MAX_BYTES:
                _, victim = _GEOMETRY.popitem(last=False)
                _GEOMETRY_BYTES -= sum(array.nbytes for array in victim[1:])
            _GEOMETRY[key] = cached
            _GEOMETRY_BYTES += length
    return cached


def mosaic_codes(scans, z, x, y, size=256, radius=230000, filtered=None, *, cache_geometry=True,
                 with_coverage=False):
    """Lowest echo wins; lower filtered clear excludes higher unfiltered echo.

    Output code 0 means both "measured, below the display floor" and "no
    measurement". with_coverage also returns the boolean mask of samples that
    some contributor actually measured: inside its range, on a bearing it
    scanned, and not a missing/range-folded gate (code 1, which QC also uses
    for removed clutter). Everything else is uncovered, never clear.
    """
    result = np.zeros(size*size, np.uint8)
    covered = np.zeros(size*size, bool)
    def done():
        codes = result.reshape(size, size)
        return (codes, covered.reshape(size, size)) if with_coverage else codes
    if not scans:
        return done()
    filtered = tuple(filtered) if filtered is not None else (True,) * len(scans)
    if len(filtered) != len(scans):
        raise ValueError('classification flags must match scans')
    values, heights, flags = [], [], []
    for scan, classified in zip(scans, filtered):
        if not _intersects(scan, z, x, y, radius):
            continue
        projection = _geometry((scan.lat, scan.lon, scan.height_m,
            scan.elevation_deg), z, x, y, size, radius, cache_geometry)
        if projection is None:
            continue
        region, bins, gates, altitude = projection
        rows = scan.bearing_index[bins]
        valid = (rows >= 0) & (gates < scan.gates) & np.isfinite(altitude)
        codes = np.ones((size, size), np.uint8)
        codes[region][valid] = scan.codes[rows[valid], gates[valid]]
        height = np.full((size, size), np.inf, np.float32)
        height[region] = altitude
        values.append(codes.ravel())
        heights.append(height.ravel())
        flags.append(classified)
        covered |= values[-1] != 1
    if not values:
        return done()
    values = np.asarray(values)
    order = np.argsort(heights, axis=0, kind='stable').astype(np.uint8)
    blocked = np.zeros(size*size, bool)
    indices = np.arange(size*size)
    flags = np.asarray(flags)
    floor = floor_code(DISPLAY_FLOOR_DBZ)
    for owners in order:
        codes, classified = values[owners, indices], flags[owners]
        good = (result == 0) & (codes >= floor) & (classified | ~blocked)
        result[good] = codes[good]
        blocked |= classified & (codes != 1) & (codes < floor)
    return done()


def render_mosaic(scans, z, x, y, palette, radius=230000, *, filtered=None, deadline=None, cache_geometry=True):
    if not scans:
        raise ValueError('mosaic render requires scan inputs')
    if len(scans) > RENDER_MAX_CANDIDATES:
        raise ValueError('mosaic render supports at most four candidates')
    remaining = None if deadline is None else max(0, deadline-time.monotonic())
    if not _RENDER_SLOTS.acquire(timeout=remaining):
        raise TimeoutError('mosaic render slot deadline')
    try:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError('mosaic render deadline')
        return _render_mosaic(scans, z, x, y, palette, radius, filtered, cache_geometry)
    finally:
        _RENDER_SLOTS.release()


def _render_mosaic(scans, z, x, y, palette, radius, filtered=None, cache_geometry=True):
    from PIL import Image
    factor = 2 if z < 8 else 1
    codes, covered = mosaic_codes(scans, z, x, y, 256*factor, radius, filtered,
                                  cache_geometry=cache_geometry, with_coverage=True)
    if factor > 1:
        codes = codes.reshape(256, factor, 256, factor).max(axis=(1, 3))
        # A pixel is covered when any of its samples was measured, so every
        # visible pixel (some sample echoed) is a covered one.
        covered = covered.reshape(256, factor, 256, factor).any(axis=(1, 3))
    slots, colours = colour_table(palette)
    pixels = slots[codes]
    image = Image.fromarray(pixels, 'P')
    flat = [v for colour in colours for v in colour[:3]]
    image.putpalette(flat + [0]*(768-len(flat)))
    image.info['transparency'] = bytes(c[3] for c in colours)
    # Uncovered pixels stay transparent like clear ones. The count and a coarse
    # spatial mask travel with the tile (PNG tEXt radarUncoveredPixels and
    # radarMeasuredGrid) so "no echoes" is never claimed where nothing was
    # measured, including in just the part of a tile a viewport shows.
    image.info['radarUncoveredPixels'] = int(covered.size - np.count_nonzero(covered))
    image.info['radarMeasuredGrid'] = measured_grid(covered)
    return image, int(np.count_nonzero(pixels))


GRID_CELLS = 16  # per tile side: 16 x 16 cells of 16 x 16 pixels


def measured_grid(covered):
    """256x256 boolean coverage -> 64 lowercase hex digits, one bit per cell,
    row-major from the north-west cell, most significant bit first. A bit is
    set only when every pixel of its cell was measured."""
    side = covered.shape[0] // GRID_CELLS
    cells = covered.reshape(GRID_CELLS, side, GRID_CELLS, side).all(axis=(1, 3))
    return np.packbits(cells.ravel()).tobytes().hex()


def grid_cells(text):
    """Inverse of measured_grid: a (16, 16) boolean array. Raises ValueError."""
    if not isinstance(text, str) or not re.fullmatch('[0-9a-f]{%d}' % (GRID_CELLS*GRID_CELLS//4), text):
        raise ValueError('measured grid')
    raw = bytes.fromhex(text)
    return np.unpackbits(np.frombuffer(raw, np.uint8)).astype(bool).reshape(GRID_CELLS, GRID_CELLS)


def read_frame_metadata(root, stamp, pairs, revision, index):
    """Recover immutable input identity, without opening or acquiring a scan."""
    candidates = []
    expected = sorted([list(p) for p in pairs])
    for path in index.paths(root, stamp):
        try:
            if path.is_symlink() or path.stat().st_size > 8192:
                continue
            value = json.loads(path.read_text())
            if (value['revision'] != revision or value['stamp'] != stamp
                    or value['requestedPairs'] != expected):
                continue
            contributors = value['siteScans']
            if not isinstance(contributors, list) or not 1 <= len(contributors) <= len(expected):
                continue
            seen = set()
            for p in contributors:
                site, ts, volume, filtered = p['id'], p['ts'], p['volumeTs'], p['filtered']
                if (site in seen or [site, ts] not in expected or type(ts) not in (int, float)
                        or type(volume) not in (int, float) or not math.isfinite(volume)
                        or volume != int(volume) or not 0 <= volume - ts < 60
                        or type(filtered) is not bool):
                    raise ValueError('invalid contributor')
                seen.add(site)
            key = mosaic_key([(p['id'], p['volumeTs'], p['filtered']) for p in contributors], revision)
            if key != value['mosaicKey'] or key != path.parent.parent.name:
                continue
            candidates.append(dict(mosaicKey=key, siteScans=contributors,
                requestedPairs=expected, unfilteredSites=[p['id'] for p in contributors if not p['filtered']]))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    # An older unfiltered identity may remain on disk after an upgrade.
    return sorted(candidates, key=lambda v: (len(v['siteScans'])-len(v['unfilteredSites']),
                                           len(v['siteScans'])), reverse=True)


def write_frame_metadata(path, stamp, pairs, metadata, revision, index=None):
    """Commit a small sidecar only after tiles exist, beside that frame's tiles."""
    import os
    import tempfile
    value = dict(metadata, stamp=stamp, revision=revision,
                 requestedPairs=sorted([list(p) for p in pairs]))
    wire = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    try:
        if path.read_text() == wire:
            if index is not None:
                index.add(path)
            return
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, suffix='.tmp', delete=False) as output:
            temporary = output.name
            output.write(wire)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        if index is not None:
            index.add(path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)
