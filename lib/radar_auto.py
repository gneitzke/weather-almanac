"""Fork-only settled-camera source policy and spherical viewport coverage."""
import contextlib
import fcntl
import hashlib
import json
import math
import os
import threading
from collections import OrderedDict
from pathlib import Path

from lib.radar_attention import WARM_HOLD_SEC
from lib.radar_geometry import EARTH_RADIUS_METERS, world_point, world_inverse

UP_ZOOM = 8
DOWN_ZOOM = 6
MIN_COVERAGE = .85
STAY_COVERAGE = .70
SWITCH_GUARD_SEC = 10
MANUAL_HOLD_SEC = WARM_HOLD_SEC
_lease_clocks = OrderedDict()
_lease_lock = threading.Lock()


def _lease_name(marker, stamp):
    return '.radar-lease-' + marker + '-' + hashlib.sha256(repr(float(stamp)).encode()).hexdigest()[:24]


def _float(value):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _disk_stamps(root, marker):
    """Every stamp a reader could take for a marker from what is on disk now
    (source_preference's derivations, unvalidated so a superset of them)."""
    root = Path(root)
    stamps = set()
    try:
        if marker == 'presence':
            stamps.add(_float((root / 'presence').read_text()[:128].split()[0]))
        else:
            try:
                stamps.add((root / 'radar_source').stat().st_mtime)
            except OSError:
                pass
            record = json.loads((root / 'radar_intent').read_text()[:4096])
            if isinstance(record, dict):
                stamps.update(_float(record.get(k)) for k in ('sourceAcceptedAt', 'acceptedAt'))
    except (OSError, UnicodeError, ValueError, IndexError):
        pass
    stamps.discard(None)
    return stamps


@contextlib.contextmanager
def _leases_locked(root):
    """Leases are shared by the server and the engine (two processes): every
    creation and removal happens under one advisory file lock beside them."""
    try:
        stream = open(Path(root) / '.radar-lease.lock', 'a')
    except OSError:
        yield False
        return
    with stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _retire_leases(root, marker):
    """Caller holds the lease file lock. Remove this marker's leases for
    stamps no longer on disk. A caller's own stamp is not evidence: another
    process may have read a newer one and created its lease a moment ago, so
    keep every lease whose stamp a reader could still derive from disk."""
    keep = {_lease_name(marker, stamp) for stamp in _disk_stamps(root, marker)}
    try:
        for stale in Path(root).glob('.radar-lease-' + marker + '-*'):
            if stale.name not in keep:
                stale.unlink(missing_ok=True)
    except OSError:
        pass  # cleanup only; the lease itself is decided below


def _lease_timestamp(root, marker, stamp, now):
    # Anchor an unchanged future marker once, so repeated polls cannot slide the
    # start of its hold forward forever after a backwards clock adjustment.
    key = (str(root), marker)
    with _lease_lock:
        old_stamp, anchor = _lease_clocks.get(key, (None, now))
        if stamp != old_stamp:
            # A stamp-specific, exclusive file lets server and engine share the
            # first anchor across restarts. Never rewrite a newer touch/choice.
            path = Path(root) / _lease_name(marker, stamp)
            with _leases_locked(root) as locked:
                # A marker has one current stamp; leases of earlier stamps (one
                # per pre-NTP boot whose clock ran behind) are never read again.
                # Without the lock, removal waits for a call that has it.
                if locked:
                    _retire_leases(root, marker)
                try:
                    try:
                        if stamp > now:
                            with path.open('x') as stream:
                                stream.write(str(now))
                                stream.flush()
                                os.fsync(stream.fileno())
                    except FileExistsError:
                        pass
                    anchor = float(path.read_text())
                    if not math.isfinite(anchor):
                        anchor = 0.
                except FileNotFoundError:
                    anchor = stamp if stamp <= now else 0.
                except (OSError, ValueError):
                    anchor = 0.  # cannot durably anchor: expire, never slide the lease
        anchor = min(anchor, now)
        _lease_clocks[key] = (stamp, anchor)
        _lease_clocks.move_to_end(key)
        while len(_lease_clocks) > 128:
            _lease_clocks.popitem(last=False)
        return anchor


def listing_availability(evidence, now, cadence, max_age):
    """Age listing uncertainty even when no subsequent request can be admitted."""
    if evidence.get('reason') == 'scan unavailable':
        since = evidence.get('failedSince', evidence.get('checkedTs'))
        return False if since is not None and now-since >= cadence else None
    if evidence.get('reporting') is True:
        newest = evidence.get('newestTs')
        return newest is not None and 0 <= now-newest < max_age
    return evidence.get('reporting')


def choose(settled_zoom, showing=None, site_available=False, coverage=0.,
           last_switch_age=None, zoom_moved=0):
    """Use tri-state availability: unknown evidence cannot change the source."""
    current = showing if showing in ('site', 'mosaic') else 'mosaic'
    if site_available is None:
        return current
    if site_available is False:
        return 'mosaic'  # confirmed not reporting / refused
    threshold = STAY_COVERAGE if current == 'site' else MIN_COVERAGE
    target = ('mosaic' if not coverage >= threshold else
              'site' if settled_zoom >= UP_ZOOM else
              'mosaic' if settled_zoom <= DOWN_ZOOM else current)
    if (target != current and last_switch_age is not None and
            last_switch_age < SWITCH_GUARD_SEC and abs(zoom_moved) < 2):
        return current
    return target


def coverage_fraction(bounds, sites, radius_meters, rows=512):
    """Union of range discs as a fraction of the Web Mercator viewport.

    Integrate exact spherical longitude intervals over 512 screen-space rows.
    Overlaps count once; wrapped longitudes and high latitudes use the same
    earth radius as radar_geometry. No tile-margin area enters this calculation.
    """
    width = (bounds['e'] - bounds['w']) % 360
    if not sites or not width or bounds['n'] <= bounds['s']:
        return 0.
    top = world_point(bounds['n'], 0, 0)[1]
    bottom = world_point(bounds['s'], 0, 0)[1]
    discs = [(math.radians(s['lat']), (s['lon']-bounds['w']) % 360) for s in sites]
    cos_range = math.cos(radius_meters / EARTH_RADIUS_METERS)
    total = 0.
    for row in range(rows):
        lat = math.radians(world_inverse(0, top+(bottom-top)*(row+.5)/rows, 0)[0])
        intervals = []
        for site_lat, lon in discs:
            q = (cos_range-math.sin(lat)*math.sin(site_lat))/(math.cos(lat)*math.cos(site_lat))
            if q > 1:
                continue
            half = math.degrees(math.acos(max(-1., q)))
            for center in (lon-360, lon, lon+360):
                left, right = max(0., center-half), min(width, center+half)
                if right > left:
                    intervals.append((left, right))
        end = 0.
        for left, right in sorted(intervals):
            total += max(0., right-max(left, end))
            end = max(end, right)
    return min(1., total/(rows*width))


def source_preference(directory, record=None, now=0):
    """Read the manual lease using the existing presence clock, never view polls.

    The server materializes an expired lease as Auto before recording any new
    touch. The engine can therefore apply expiry even while the page is closed.
    A legacy preference without presence gets its first hold from its mtime.
    """
    root = Path(directory)
    try:
        marker = root / 'radar_source'
        pref = marker.read_text()[:128].strip()
        selected = marker.stat().st_mtime
    except (OSError, UnicodeError):
        pref, selected = 'auto', 0.
    if record is not None:
        # Accepted camera moves do not renew the source lease. Until the
        # debounce persists a new source, its transaction supplies the start.
        if record['source'] != pref or not selected:
            selected = record.get('sourceAcceptedAt', record.get('acceptedAt', selected))
        pref = record['source']
    if pref not in ('mosaic', 'site'):
        return 'auto'
    try:
        touched = float((root / 'presence').read_text()[:128].split()[0])
        if not math.isfinite(touched):
            touched = 0.
    except (OSError, ValueError, IndexError):
        touched = 0.
    selected = _lease_timestamp(root, 'source', selected, now)
    touched = _lease_timestamp(root, 'presence', touched, now)
    return 'auto' if now-max(touched, selected) >= MANUAL_HOLD_SEC else pref
