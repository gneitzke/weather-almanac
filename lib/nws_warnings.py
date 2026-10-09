"""NWS storm-based warning polygons for the radar map.

api.weather.gov has no bounding-box query. `alerts/active` filters by `area`
(state/territory and marine area codes, comma-separated), `event`, `status`
and `message_type`, so the engine asks for:

    /alerts/active?status=actual&message_type=alert,update,cancel
                  &area=<every code whose extent the radar camera can reach>
                  &event=<the storm-based warning events>

What the camera can reach (Reach) is derived from the page's own camera
limits, not from the station's automatic zoom: console_live.html lets the
viewer zoom out to radar.zoomMin (4) and pan PAN_DIAGONALS viewport diagonals
from the station at whatever zoom is showing. At zoom 4 that region spans most
of a hemisphere, so for any US station the query names every NWS area and is
the national storm-warning feed. That is deliberate: filtering by event on the
server keeps it small (measured 2026-10-09: 6 warnings, 43 KB; an outbreak
with ~100 warnings in force is ~0.7 MB, under MAX_BODY_BYTES), and one query
is right for every viewer, zoom and pan with no round trip after a gesture.
The alternative, a footprint that follows the view, would leave every pan
blank until the next fetch and needs every viewer's camera on the engine.

Only storm-based warnings are drawn: they carry their own polygon. Zone-based
products (watches, advisories, Flood/Winter/Tropical warnings) carry either no
geometry or a merged zone outline and are NOT drawn; the station strip still
lists them by point.

Measured on api.weather.gov (2026-10-09): responses carry a weak ETag and
`Cache-Control: max-age=5` but no Last-Modified, and a matching If-None-Match
is still answered 200 with the full body. The tracker sends If-None-Match
anyway (a 304 is handled if the service starts honouring it).

Everything here is pure (no network, no clock reads): the emitter supplies
`now`, the HTTP result and the radar attention; tests drive it directly.
"""
import json
import math
import re
import threading
from datetime import datetime, timezone
from urllib.parse import quote

from lib.radar_geometry import MAX_LAT

API_URL = 'https://api.weather.gov/alerts/active'

# Storm-based (polygon) warnings, NWS event names. PDS and emergency variants
# keep these event names and say so in parameters (see _threat).
STORM_EVENTS = ('Tornado Warning', 'Severe Thunderstorm Warning', 'Flash Flood Warning',
                'Special Marine Warning', 'Snow Squall Warning', 'Extreme Wind Warning',
                'Dust Storm Warning')

# kind, NWS published map colour (weather.gov/help-map), draw/sort rank (higher = more severe)
EVENT_STYLE = {
    'Tornado Warning':             ('tornado',     '#FF0000', 60),
    'Extreme Wind Warning':        ('extremewind', '#FF8C00', 55),
    'Flash Flood Warning':         ('flashflood',  '#8B0000', 50),
    'Severe Thunderstorm Warning': ('severe',      '#FFA500', 40),
    'Snow Squall Warning':         ('snowsquall',  '#C71585', 35),
    'Special Marine Warning':      ('marine',      '#FFA500', 30),
    'Dust Storm Warning':          ('dust',        '#FFE4C4', 25),
}
# The VTEC phenomenon that identifies each event's own warning (NWSI 10-1703).
EVENT_PHENOMENON = {
    'Tornado Warning': 'TO', 'Severe Thunderstorm Warning': 'SV', 'Flash Flood Warning': 'FF',
    'Special Marine Warning': 'MA', 'Snow Squall Warning': 'SQ', 'Extreme Wind Warning': 'EW',
    'Dust Storm Warning': 'DS',
}
ENDING_ACTIONS = ('CAN', 'EXP')   # VTEC actions that end the segment they are on
# At most 0.1% uncovered area PER predecessor component. This accommodates
# tiny vertex-rounding slivers, ten times stricter than a 1% allowance, without
# buffering every edge by hundreds of metres or dropping a small whole island.
SUPERSESSION_AREA_TOLERANCE = 0.001
# Shared by all removal attempts in one feed. Charge edge-pair tests AND
# scanline work (including interval sorting/overlap), not just intersections.
# 50 pairs of 48-vertex rings parse in ~0.05 s locally (~0.2-0.3 s at 4-6x
# slower on a Pi). This bounds supersession work, not input decoding/display.
SUPERSESSION_WORK_BUDGET = 50_000
SUPERSESSION_RING_VERTICES = 48

FAST_SEC = 90            # weather nearby, a warning near the station, or someone on the radar
SLOW_SEC = 900           # otherwise: the station alert strip's own 15-minute cadence
TICK_SEC = 15            # how often the emitter asks whether a fetch is due
RETRY_BASE_SEC = 120     # first retry after a failure, doubling ...
BACKOFF_MAX_SEC = 1800   # ... to at most 30 minutes; Retry-After is honoured within it
FETCH_DEADLINE_SEC = 20  # one fetch end to end: DNS, connect, TLS, request, every body read, a retry
STALE_GRACE_SEC = 60     # a due refresh lands within a tick + the fetch deadline + an emit
STALE_MAX_SEC = 2 * SLOW_SEC   # data this old is stale whatever the schedule says
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_PAYLOAD_BYTES = MAX_BODY_BYTES  # reject an oversized refresh, never publish a partial list
MAX_RING_VERTICES = 48   # per ring, after simplification
MAX_ITEM_VERTICES = 96   # per warning, all rings
TARGET_TOTAL_VERTICES = 12 * MAX_ITEM_VERTICES  # scales to keep every reachable component
MIN_RING_VERTICES = 4
HEADLINE_MAX = 160
VIEW_W, VIEW_H = 956, 490
PAN_DIAGONALS = 1.5      # console_live.html radarClampCamera: cap = 1.5 * hypot(956, 490) screen px
MIN_ZOOM = 4             # console_live.html camera floor: radar.zoomMin (almanac_emit.RADAR_MIN_ZOOM)
EARTH_CIRCUMFERENCE_M = 40075016.686

# State, territory and marine area codes (api.weather.gov AreaCode) with their
# approximate extents (s, n, w, e degrees), padded outward by PAD below. An
# extent only chooses what to ask for; each polygon is then tested itself.
PAD = 0.3
AREAS = {
    'AL': (30.1, 35.0, -88.5, -84.9), 'AK': (51.2, 71.4, 172.0, -129.9), 'AZ': (31.3, 37.0, -114.9, -109.0),
    'AR': (33.0, 36.5, -94.7, -89.6), 'CA': (32.5, 42.0, -124.5, -114.1), 'CO': (37.0, 41.0, -109.1, -102.0),
    'CT': (40.9, 42.1, -73.8, -71.8), 'DE': (38.4, 39.9, -75.8, -75.0), 'DC': (38.8, 39.0, -77.2, -76.9),
    'FL': (24.4, 31.0, -87.7, -80.0), 'GA': (30.3, 35.0, -85.7, -80.8), 'HI': (18.9, 22.3, -160.3, -154.8),
    'ID': (42.0, 49.0, -117.3, -111.0), 'IL': (36.9, 42.5, -91.6, -87.0), 'IN': (37.7, 41.8, -88.1, -84.8),
    'IA': (40.3, 43.6, -96.7, -90.1), 'KS': (36.9, 40.0, -102.1, -94.6), 'KY': (36.5, 39.2, -89.6, -81.9),
    'LA': (28.9, 33.0, -94.1, -88.8), 'ME': (43.0, 47.5, -71.1, -66.9), 'MD': (37.9, 39.8, -79.5, -75.0),
    'MA': (41.2, 42.9, -73.6, -69.9), 'MI': (41.7, 48.3, -90.5, -82.1), 'MN': (43.5, 49.4, -97.3, -89.5),
    'MS': (30.1, 35.0, -91.7, -88.1), 'MO': (36.0, 40.7, -95.8, -89.1), 'MT': (44.3, 49.0, -116.1, -104.0),
    'NE': (40.0, 43.0, -104.1, -95.3), 'NV': (35.0, 42.0, -120.0, -114.0), 'NH': (42.7, 45.3, -72.6, -70.6),
    'NJ': (38.9, 41.4, -75.6, -73.9), 'NM': (31.3, 37.0, -109.1, -103.0), 'NY': (40.5, 45.0, -79.8, -71.8),
    'NC': (33.8, 36.6, -84.3, -75.4), 'ND': (45.9, 49.0, -104.1, -96.5), 'OH': (38.4, 42.3, -84.8, -80.5),
    'OK': (33.6, 37.0, -103.0, -94.4), 'OR': (41.9, 46.3, -124.6, -116.5), 'PA': (39.7, 42.3, -80.5, -74.7),
    'RI': (41.1, 42.0, -71.9, -71.1), 'SC': (32.0, 35.2, -83.4, -78.5), 'SD': (42.5, 45.95, -104.1, -96.4),
    'TN': (35.0, 36.7, -90.3, -81.6), 'TX': (25.8, 36.5, -106.6, -93.5), 'UT': (37.0, 42.0, -114.1, -109.0),
    'VT': (42.7, 45.0, -73.4, -71.5), 'VA': (36.5, 39.5, -83.7, -75.2), 'WA': (45.5, 49.0, -124.8, -116.9),
    'WV': (37.2, 40.6, -82.6, -77.7), 'WI': (42.5, 47.3, -92.9, -86.2), 'WY': (41.0, 45.0, -111.1, -104.0),
    'PR': (17.9, 18.5, -67.3, -65.2), 'VI': (17.6, 18.4, -65.1, -64.6), 'GU': (13.2, 13.7, 144.6, 145.0),
    'AS': (-14.6, -11.0, -171.1, -168.1), 'MP': (14.1, 20.6, 144.9, 146.1), 'PW': (2.8, 8.1, 131.1, 134.8),
    'FM': (1.0, 10.1, 137.3, 163.1), 'MH': (4.5, 14.7, 160.8, 172.2),
    # marine areas (NWS Directive 10-302)
    'PZ': (30.0, 49.0, -130.0, -117.0), 'PK': (51.0, 72.0, 170.0, -129.0), 'PH': (17.0, 24.0, -162.0, -153.0),
    'PM': (12.0, 21.0, 143.0, 147.0), 'PS': (-15.0, -10.5, -172.0, -168.0),
    'AN': (35.5, 45.0, -77.0, -65.0), 'AM': (17.0, 36.6, -82.0, -60.0), 'GM': (24.0, 30.8, -98.0, -80.4),
    'LS': (46.4, 49.1, -92.2, -84.3), 'LM': (41.6, 46.2, -88.1, -84.7), 'LH': (43.0, 46.3, -84.8, -79.6),
    'LE': (41.3, 42.95, -83.5, -78.8), 'LO': (43.1, 44.3, -79.9, -76.0), 'LC': (42.3, 42.7, -83.0, -82.4),
    'SL': (44.3, 45.1, -76.4, -74.6),
}

_VTEC = re.compile(r'/[OTEX]\.([A-Z]{3})\.([A-Z]{4})\.([A-Z]{2})\.([A-Z])\.(\d{4})\.')


# ----------------------------------------------------------------- reach / area
def _mercator(lat, lon):
    """Web Mercator world units: the world is 1 wide at every zoom."""
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    return (lon + 180) / 360, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2


def unwrap(ring, ref_lon):
    """Longitudes made continuous: the first vertex is moved by whole turns to
    the copy nearest `ref_lon`, every later one to the copy nearest the vertex
    before it. A ring across the antimeridian (179.9, -179.9) becomes
    (179.9, 180.1), so planar tests on it are right."""
    out, previous = [], ref_lon
    for lon, lat in ring:
        lon = lon + 360 * round((previous - lon) / 360)
        out.append([lon, lat])
        previous = lon
    return out


def _interval_gap(lo, hi):
    """Distance from 0 to the interval [lo, hi]; 0 inside."""
    return lo if lo > 0 else -hi if hi < 0 else 0.0


def _rect_gap(x, y, hw, hh):
    return math.hypot(max(abs(x) - hw, 0.0), max(abs(y) - hh, 0.0))


def _segment_hits_rect(a, b, hw, hh):
    """Liang-Barsky: does segment a-b touch the rectangle |x|<=hw, |y|<=hh?"""
    t0, t1 = 0.0, 1.0
    dx, dy = b[0] - a[0], b[1] - a[1]
    for p, q in ((-dx, a[0] + hw), (dx, hw - a[0]), (-dy, a[1] + hh), (dy, hh - a[1])):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return True


def _polygon_gap(points, hw, hh):
    """Exact distance from a polygon (planar points, origin-centred frame) to
    the rectangle |x|<=hw, |y|<=hh; 0 when they overlap. Non-overlapping
    convex/any shapes are closest between a vertex of one and an edge of the
    other, so endpoints and rectangle corners are all that need testing."""
    if any(abs(x) <= hw and abs(y) <= hh for x, y in points) or point_in_ring(0.0, 0.0, points):
        return 0.0
    if hw == 0 and hh == 0:
        # A point has one corner, not four identical corners and four edges.
        return min(_perp((0.0, 0.0), a, b) for a, b in zip(points, points[1:] + points[:1]))
    corners = ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))
    best = math.inf
    for a, b in zip(points, points[1:] + points[:1]):
        if _segment_hits_rect(a, b, hw, hh):
            return 0.0
        best = min(best, _rect_gap(a[0], a[1], hw, hh),
                   *(_perp(c, a, b) for c in corners))
    return best


class Reach:
    """Everything the radar camera can put on screen around the station at
    `zoom` or any closer zoom.

    console_live.html radarClampCamera keeps the camera centre within
    PAN_DIAGONALS viewport diagonals of the station, in screen pixels at the
    current zoom and across the world wrap, and the viewport shows half its
    width and height around that centre. In Web Mercator world units the
    reachable region is therefore every point within `pan` of the rectangle
    (±hw, ±hh) centred on the station; both halve at each closer zoom, so the
    camera's floor zoom bounds every view. Polygons are tested against that
    region exactly (a bounding-box test first), not by their bounds."""

    def __init__(self, lat, lon, zoom=MIN_ZOOM):
        self.lat, self.lon, self.zoom = float(lat), float(lon), zoom
        world = 256 * 2 ** zoom
        self.hw, self.hh = VIEW_W / 2 / world, VIEW_H / 2 / world
        self.pan = PAN_DIAGONALS * math.hypot(VIEW_W, VIEW_H) / world
        self.x, self.y = _mercator(self.lat, self.lon)

    def _local(self, ring):
        """Station-centred Mercator points of a [lon, lat] ring, unwrapped."""
        return [((lon - self.lon) / 360, _mercator(lat, lon)[1] - self.y) for lon, lat in unwrap(ring, self.lon)]

    def gap(self, ring, hw=None, hh=None):
        """World units from the ring to the rectangle (default: the viewport
        rectangle), over the ring's nearest copies across the wrap."""
        hw = self.hw if hw is None else hw
        hh = self.hh if hh is None else hh
        points = self._local(ring)
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        best = math.inf
        for shift in (0.0, -1.0, 1.0):
            # bounding box first: the exact test only runs where it can matter
            box = math.hypot(max(_interval_gap(min(xs) + shift, max(xs) + shift) - hw, 0.0),
                             max(_interval_gap(min(ys), max(ys)) - hh, 0.0))
            if box > self.pan or box >= best:
                continue
            best = min(best, _polygon_gap([(x + shift, y) for x, y in points], hw, hh))
        return best

    def reaches(self, ring):
        return self.gap(ring) <= self.pan

    def distance_m(self, ring):
        """Approximate ground distance from the station to the ring, 0 inside."""
        return self.gap(ring, 0.0, 0.0) * EARTH_CIRCUMFERENCE_M * math.cos(math.radians(self.lat))


def area_codes(reach):
    """Every NWS area code whose (padded) extent the camera can reach, sorted;
    () when none can (no NWS coverage)."""
    codes = []
    for code, (s, n, w, e) in AREAS.items():
        s, n, w, e = max(-90.0, s - PAD), min(90.0, n + PAD), w - PAD, e + PAD
        if e < w:
            e += 360                                  # crosses the antimeridian (AK, PK)
        if reach.reaches([[w, s], [e, s], [e, n], [w, n]]):
            codes.append(code)
    return tuple(sorted(codes))


def query_url(codes):
    events = ','.join(quote(e) for e in STORM_EVENTS)
    return f'{API_URL}?status=actual&message_type=alert,update,cancel&area={",".join(codes)}&event={events}'


# ------------------------------------------------------------------- parsing
def _epoch(iso):
    if not isinstance(iso, str) or not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _parameters(props):
    parameters = props.get('parameters')
    return parameters if isinstance(parameters, dict) else {}


def _param(props, name):
    values = _parameters(props).get(name)
    return values[0] if isinstance(values, list) and values and isinstance(values[0], str) else None


def _vtec(props):
    """[(action, office, phenomenon, significance, etn), ...] from parameters.VTEC."""
    values = _parameters(props).get('VTEC')
    out = []
    for value in values if isinstance(values, list) else ():
        match = _VTEC.search(value) if isinstance(value, str) else None
        if match:
            out.append(match.groups())
    return out


def _threat(event, props):
    """PDS / emergency variants are flagged in parameters, not the event name."""
    tor = (_param(props, 'tornadoDamageThreat') or '').upper()
    ffw = (_param(props, 'flashFloodDamageThreat') or '').upper()
    svr = (_param(props, 'thunderstormDamageThreat') or '').upper()
    if event == 'Tornado Warning':
        return {'CATASTROPHIC': 'emergency', 'CONSIDERABLE': 'pds'}.get(tor)
    if event == 'Flash Flood Warning':
        return {'CATASTROPHIC': 'emergency', 'CONSIDERABLE': 'considerable'}.get(ffw)
    if event == 'Severe Thunderstorm Warning':
        return {'DESTRUCTIVE': 'destructive', 'CONSIDERABLE': 'considerable'}.get(svr)
    return None


def _label(event, threat):
    if threat == 'emergency':
        return event.replace(' Warning', ' Emergency')
    if threat == 'pds':
        return 'PDS ' + event
    return event


def _detail(props):
    """One short line from the warning's own tags: source, hail, gusts."""
    parts = []
    source = _param(props, 'tornadoDetection') or _param(props, 'waterspoutDetection') \
        or _param(props, 'flashFloodDetection')
    if source:
        parts.append(source.capitalize())
    hail = _param(props, 'maxHailSize')
    try:
        if hail is not None and float(hail) > 0:
            parts.append(f'hail {float(hail):g} in')
    except ValueError:
        pass
    gust = _param(props, 'maxWindGust')
    if gust and not gust.startswith('0'):
        parts.append(f'gusts {gust.lower()}')
    text = ' · '.join(parts)
    return text[:80] or None


def _instruction(props):
    """The complete warning action text (CAP `instruction`), whitespace folded.
    Never composed: no instruction in the feed, no instruction here."""
    text = props.get('instruction')
    if not isinstance(text, str):
        return None
    text = ' '.join(text.split())
    return text or None


def _rings(geometry):
    """Outer rings as [[lon, lat], ...] lists (holes dropped: warnings have none)."""
    if not isinstance(geometry, dict):
        return []
    kind, coords = geometry.get('type'), geometry.get('coordinates')
    polygons = [coords] if kind == 'Polygon' else coords if kind == 'MultiPolygon' else []
    rings = []
    for polygon in polygons if isinstance(polygons, list) else ():
        if not isinstance(polygon, list) or not polygon or not isinstance(polygon[0], list):
            continue
        ring = []
        for point in polygon[0]:
            if (isinstance(point, list) and len(point) >= 2
                    and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in point[:2])
                    and -180 <= point[0] <= 180 and -90 <= point[1] <= 90):
                ring.append([float(point[0]), float(point[1])])
        if len(ring) >= 3:
            rings.append(ring)
    return rings


def _perp(p, a, b):
    if a == b:
        return math.hypot(p[0]-a[0], p[1]-a[1])
    dx, dy = b[0]-a[0], b[1]-a[1]
    t = max(0.0, min(1.0, ((p[0]-a[0])*dx + (p[1]-a[1])*dy) / (dx*dx + dy*dy)))
    return math.hypot(p[0]-a[0]-t*dx, p[1]-a[1]-t*dy)


def _dp(points, tolerance):
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        i, j = stack.pop()
        far, index = -1.0, None
        for k in range(i + 1, j):
            d = _perp(points[k], points[i], points[j])
            if d > far:
                far, index = d, k
        if index is not None and far > tolerance:
            keep[index] = True
            stack.extend(((i, index), (index, j)))
    return [p for p, k in zip(points, keep) if k]


def _closed(points):
    dedup = [p for i, p in enumerate(points) if i == 0 or p != points[i-1]]
    if dedup[0] != dedup[-1]:
        dedup.append(list(dedup[0]))
    return dedup


def simplify(ring, limit=MAX_RING_VERTICES):
    """Closed ring -> at most `limit` (>= 4) vertices, closing point included,
    rounded to 0.01 degree, the precision NWS publishes (finer when that
    would collapse a tiny ring). Douglas-Peucker with a growing tolerance, so
    small storm polygons pass through untouched; a ring is never simplified
    away: below a triangle it keeps evenly spaced original vertices."""
    limit = max(4, limit)
    out = []
    for digits in (2, 4):
        out = _closed([[round(x, digits), round(y, digits)] for x, y in ring])
        if len({tuple(p) for p in out}) >= 3:
            break
    if len({tuple(p) for p in out}) < 3:
        return []
    dedup, tolerance = out, 0.0
    while len(out) > limit:
        tolerance = tolerance * 2 if tolerance else 0.005
        candidate = _dp(dedup, tolerance)
        if len(candidate) < 4:
            body = dedup[:-1]
            step = len(body) / (limit - 1)
            out = [list(body[int(i * step)]) for i in range(limit - 1)] + [list(body[0])]
            break
        out = candidate
    return out


def _published(ring):
    """Back to GeoJSON longitudes (-180..180); the page re-wraps per vertex."""
    return [[x if -180 <= x <= 180 else round((x + 180) % 360 - 180, 4), y] for x, y in ring]


def point_in_ring(lon, lat, ring):
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _record(feature):
    """The fields parse() needs from one feature, or None when it is not an
    Actual message. Validates shapes; raises nothing for well-typed junk."""
    if not isinstance(feature, dict):
        return None
    props = feature.get('properties')
    if not isinstance(props, dict) or str(props.get('status') or '').lower() != 'actual':
        return None
    ident = props.get('id') if isinstance(props.get('id'), str) else feature.get('id')
    event = props.get('event') if isinstance(props.get('event'), str) else None
    references = props.get('references')
    refs = tuple(r['identifier'] for r in (references if isinstance(references, list) else ())
                 if isinstance(r, dict) and isinstance(r.get('identifier'), str))
    vtec = _vtec(props)
    phenomenon = EVENT_PHENOMENON.get(event)
    own = next((v for v in vtec if v[2] == phenomenon and v[3] == 'W'), vtec[0] if vtec else None)
    geocode = props.get('geocode')
    ugc = geocode.get('UGC') if isinstance(geocode, dict) else None
    return dict(feature=feature, props=props, event=event, refs=refs, vtec=vtec,
                ugc=frozenset(v for v in ugc if isinstance(v, str)) if isinstance(ugc, list) else frozenset(),
                rings=_rings(feature.get('geometry')),
                ident=ident if isinstance(ident, str) else None,
                message=str(props.get('messageType') or '').lower(),
                key=tuple(own[1:]) if own else None, action=own[0] if own else None,
                sent=_epoch(props.get('sent')) or 0.0)


def _ring_covered(ring, boundary):
    """Full containment, including edges crossing a concave boundary and wrap.

    Split each edge at boundary intersections; every resulting interval must
    be inside or on the boundary. Vertex-only containment misses concavities.
    """
    ring = unwrap(ring, ring[0][0])
    boundary = unwrap(boundary, ring[0][0])
    edges = list(zip(boundary, boundary[1:] + boundary[:1]))

    def inside(p):
        return point_in_ring(*p, boundary) or any(_perp(p, a, b) < 1e-9 for a, b in edges)

    if not all(inside(p) for p in ring):
        return False
    for a, b in zip(ring, ring[1:] + ring[:1]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        cuts = [0.0, 1.0]
        for c, d in edges:
            ex, ey = d[0] - c[0], d[1] - c[1]
            den = dx * ey - dy * ex
            if abs(den) < 1e-15:
                continue
            cx, cy = c[0] - a[0], c[1] - a[1]
            t, u = (cx * ey - cy * ex) / den, (cx * dy - cy * dx) / den
            if 0 <= t <= 1 and 0 <= u <= 1:
                cuts.append(t)
        cuts.sort()
        if any(not inside([a[0] + dx * (lo + hi) / 2, a[1] + dy * (lo + hi) / 2])
               for lo, hi in zip(cuts, cuts[1:])):
            return False
    return True


def _supersession_candidate(new, old):
    """Scope eligibility only; never sufficient evidence to remove a polygon."""
    if new is old or new['message'] not in ('alert', 'update', 'cancel') or new['sent'] < old['sent']:
        return False
    referenced = old['ident'] in new['refs']
    actions = [action for action, *key in new['vtec'] if tuple(key) == old['key']]
    if new['refs'] and not referenced:
        return False
    if not referenced and (not actions or new['sent'] == old['sent']):
        return False
    if new['ugc'] and old['ugc'] and not old['ugc'].intersection(new['ugc']):
        return False
    return True


class _GeometryBudget:
    """Deterministic, fail-closed work allowance; exhaustion is sticky."""

    def __init__(self, limit=None):
        self.remaining = SUPERSESSION_WORK_BUDGET if limit is None else limit

    def spend(self, work):
        if work > self.remaining or self.remaining == 0:
            self.remaining = 0
            return False
        self.remaining -= work
        return True


def _point_in_or_on_ring(lon, lat, ring):
    # No distance/area tolerance: even a tiny notch at the station matters.
    return point_in_ring(lon, lat, ring) or any(
        (lon - a[0]) * (b[1] - a[1]) == (lat - a[1]) * (b[0] - a[0])
        and min(a[0], b[0]) <= lon <= max(a[0], b[0])
        and min(a[1], b[1]) <= lat <= max(a[1], b[1])
        for a, b in zip(ring, ring[1:] + ring[:1]))


def _supersedes(new, old, station=None, budget=None):
    """Single updates obey the same scope AND geometry rule as split updates."""
    return _supersession_candidate(new, old) and _replacement_covers([new], old, station, budget)


def _rings_cover(rings, boundaries, station=None, budget=None):
    """Containment in a polygon union, including gaps enclosed by that union.

    Between vertex/intersection latitudes, edge ordering is fixed and every
    horizontal interval endpoint is linear. Midpoint integration gives the
    exact planar covered/uncovered areas, including interior gaps. Apply the
    rounding allowance per component so a large mainland cannot hide loss of
    a small island. The station must be covered exactly before any allowance.
    Dense predecessor rings use enclosing rectangles with ZERO allowance:
    enlarging the denominator of a relative tolerance would be unsafe.
    Updates always retain their original geometry. Missing/degenerate
    geometry or exhausted work fails safe.
    """
    if not rings or not boundaries:
        return False
    budget = budget if budget is not None else _GeometryBudget()
    vertices = sum(map(len, rings)) + sum(map(len, boundaries))
    if not budget.spend(vertices * 4):  # unwrap, station containment, outer bounds
        return False
    anchor = station[0] if station is not None else rings[0][0][0]
    rings = [unwrap(r, anchor) for r in rings]
    boundaries = [unwrap(r, anchor) for r in boundaries]
    if (station is not None and any(_point_in_or_on_ring(*station, r) for r in rings)
            and not any(_point_in_or_on_ring(*station, r) for r in boundaries)):
        return False
    allowances = []
    bounded = []
    for ring in rings:
        if len(ring) > SUPERSESSION_RING_VERTICES:
            xs, ys = [p[0] for p in ring], [p[1] for p in ring]
            w, e, s, n = min(xs), max(xs), min(ys), max(ys)
            bounded.append([[w, s], [e, s], [e, n], [w, n]])
            allowances.append(0.0)
        else:
            bounded.append(ring)
            allowances.append(SUPERSESSION_AREA_TOLERANCE)
    rings = bounded
    vertices = sum(map(len, rings)) + sum(map(len, boundaries))
    # Reserve BEFORE constructing/testing pairs. A single dense update must
    # not monopolize the worker even when its predecessor has only four edges.
    if not budget.spend(vertices * (vertices - 1) // 2):
        return False
    edges = [(a, b) for r in rings + boundaries for a, b in zip(r, r[1:] + r[:1])]
    cuts = {p[1] for r in rings + boundaries for p in r}
    for i, (a, b) in enumerate(edges):
        dx, dy = b[0] - a[0], b[1] - a[1]
        for c, d in edges[i + 1:]:
            ex, ey = d[0] - c[0], d[1] - c[1]
            den = dx * ey - dy * ex
            if abs(den) < 1e-15:
                continue
            cx, cy = c[0] - a[0], c[1] - a[1]
            t, u = (cx * ey - cy * ex) / den, (cx * dy - cy * dx) / den
            if 0 < t < 1 and 0 < u < 1:
                cuts.add(a[1] + t * dy)

    def intervals(polygons, y):
        result = []
        for r in polygons:
            xs = sorted(a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
                        for a, b in zip(r, r[1:] + r[:1]) if (a[1] > y) != (b[1] > y))
            result.extend(zip(xs[::2], xs[1::2]))
        merged = []
        for lo, hi in sorted(result):
            if merged and lo <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        return merged

    if not budget.spend(len(cuts) * len(cuts).bit_length()):
        return False
    cuts = sorted(cuts)
    areas, missing = [0.0] * len(rings), [0.0] * len(rings)
    old_vertices, new_vertices = sum(map(len, rings)), sum(map(len, boundaries))
    # Upper bound for edge scans/sorts and all old/new interval overlaps in
    # one slab. Intersections can create quadratically many slabs themselves.
    slab_work = vertices * (2 + vertices.bit_length()) + old_vertices * new_vertices
    for lo, hi in zip(cuts, cuts[1:]):
        if not budget.spend(slab_work):
            return False
        y = (lo + hi) / 2
        covering = intervals(boundaries, y)
        for i, ring in enumerate(rings):
            for a, b in intervals([ring], y):
                width = b - a
                covered = sum(max(0.0, min(b, d) - max(a, c)) for c, d in covering)
                areas[i] += width * (hi - lo)
                missing[i] += max(0.0, width - covered) * (hi - lo)
    return all(area > 0 and gap <= area * allowance
               for area, gap, allowance in zip(areas, missing, allowances))


def _replacement_covers(updates, old, station=None, budget=None):
    """UGCs may veto removal, but only the scoped polygon union can allow it.

    Keep the whole predecessor when coverage is incomplete or unknown. CAN,
    EXP, CON and EXT all need the same geometric evidence; an unrelated
    continuation cannot turn a partial cancellation into a complete one.
    """
    if budget is not None and budget.remaining == 0:
        return False
    updates = [r for r in updates if r['rings']]
    ugc = frozenset().union(*(r['ugc'] for r in updates))
    if old['ugc'] and ugc and not old['ugc'] <= ugc:
        return False
    return _rings_cover(old['rings'], [ring for r in updates for ring in r['rings']], station, budget)


def _referenced_supersedes(updates, old, station=None, budget=None):
    """Resolve a predecessor against all its eligible referencing segments.

    Each contributing update must apply to THIS predecessor. Combined county
    lists never bypass geometry, even when a continuation is present.
    """
    updates = [r for r in updates if old['ident'] in r['refs'] and _supersession_candidate(r, old)]
    return _replacement_covers(updates, old, station, budget)


def parse(features, now, reach, until_text=None, home=None):
    """NWS GeoJSON features -> the drawable warnings, most important first.

    Keeps status Actual storm-based warnings with a polygon that is in force
    (neither `expires` nor `ends` passed) and that the camera can reach.

    Supersession is chronological and scoped by CAP references, segment UGCs
    and original geometry, including VTEC ending actions on upgrades.

    Station coverage and reach are computed from the ORIGINAL geometry
    (unwrapped across the antimeridian), before any simplification; components
    the camera cannot reach are dropped, the one over the station always
    leads. Every reachable warning/component is retained, in local-first order.
    The geometry budget scales with coverage, and a byte ceiling rejects an
    oversized refresh rather than silently publishing incomplete coverage.
    A malformed feature is skipped on its own; it never costs the rest.
    `home` (the station's automatic view) marks warnings `_near` for cadence."""
    records = []
    for feature in features if isinstance(features, list) else ():
        try:
            record = _record(feature)
        except (TypeError, ValueError, AttributeError, KeyError, IndexError):
            record = None
        if record is not None:
            records.append(record)
    # CAP references can name the immediate predecessor rather than every
    # ancestor. Follow that chain, but never backwards through a newer message.
    identified = {r['ident']: r for r in records}
    for r in records:
        pending, ancestors = list(r['refs']), set(r['refs'])
        while pending:
            previous = identified.get(pending.pop())
            if previous is None or previous['sent'] > r['sent']:
                continue
            for ref in previous['refs']:
                if ref not in ancestors and ref != r['ident']:
                    ancestors.add(ref)
                    pending.append(ref)
        r['refs'] = tuple(ancestors)
    by_key, by_ref = {}, {}
    for r in records:
        for action, *key in r['vtec']:
            by_key.setdefault(tuple(key), []).append(r)
        for ref in r['refs']:
            by_ref.setdefault(ref, []).append(r)
    items = []
    budget = _GeometryBudget()
    station = (reach.lon, reach.lat)
    for r in records:
        if (r['event'] not in EVENT_STYLE or r['message'] not in ('alert', 'update')
                or r['ident'] is None or r['action'] in ENDING_ACTIONS
                or any(_supersedes(new, r, station, budget)
                       for new in by_ref.get(r['ident'], []) + by_key.get(r['key'], []))
                or _referenced_supersedes(by_ref.get(r['ident'], []), r, station, budget)):
            continue
        try:
            item = _item(r, now, reach, until_text, home)
        except (TypeError, ValueError, AttributeError, KeyError, IndexError, ZeroDivisionError, OverflowError):
            item = None
        if item is not None:
            items.append(item)
    return _select(items)


def _item(r, now, reach, until_text, home):
    props, event = r['props'], r['event']
    expires, ends = _epoch(props.get('expires')), _epoch(props.get('ends'))
    deadlines = [t for t in (expires, ends) if t is not None]
    removal = min(deadlines) if deadlines else None      # the message stops being valid here
    if removal is not None and removal <= now:
        return None
    end = ends if ends is not None else expires          # when the event ends: "until 3:45 PM"
    components = []
    for ring in r['rings']:
        ring = unwrap(ring, reach.lon)
        covers = point_in_ring(reach.lon, reach.lat, ring)
        if covers or reach.reaches(ring):
            components.append((not covers, reach.distance_m(ring), -len(ring), ring))
    if not components:
        return None
    components.sort(key=lambda c: c[:3])
    affects = not components[0][0]
    kind, color, rank = EVENT_STYLE[event]
    threat = _threat(event, props)
    headline = _param(props, 'NWSheadline') or props.get('headline') or event
    rings = [c[3] for c in components]
    return dict(
        id=r['ident'], event=event, label=_label(event, threat), kind=kind, threat=threat,
        level='emergency' if threat == 'emergency' else 'warning', color=color,
        onset=int(_epoch(props.get('onset')) or _epoch(props.get('effective')) or now),
        expires=int(removal) if removal is not None else None,
        ends=int(end) if end is not None else None,
        until=until_text(end) if (until_text and end is not None) else None,
        headline=str(headline)[:HEADLINE_MAX], detail=_detail(props), instruction=_instruction(props),
        sender=str(props.get('senderName') or '')[:60] or None,
        affectsStation=affects, polygon=None,
        _rings=rings,
        _near=affects or bool(home and any(home.reaches(ring) for ring in rings)),
        _rank=rank + (8 if threat == 'emergency' else 3 if threat else 0),
        _distance=components[0][1])


def _select(items):
    """Local first, complete reachable coverage, bounded serialized payload.

    The usual vertex target is allocated local-first. During an outbreak it
    grows to preserve the local detail allowance plus a closed triangle for
    every distant component; the 4 MiB input
    and output ceilings bound wx.json even with complete official instructions.
    """
    def order(i):
        return (not i['affectsStation'], not i['_near'], i['_distance'], -i['_rank'],
                i['expires'] if i['expires'] is not None else 9e18, i['id'])
    items.sort(key=order)
    chosen = items
    floors = [max(MIN_RING_VERTICES * len(i['_rings']), MAX_ITEM_VERTICES if i['_near'] else 0)
              for i in chosen]
    reserved = sum(floors)
    left = max(TARGET_TOTAL_VERTICES, reserved)
    for item, floor in zip(chosen, floors):
        rings = item.pop('_rings')
        reserved -= floor
        budget = min(max(MAX_ITEM_VERTICES, floor), left - reserved)
        shapes = []
        for k, ring in enumerate(rings):
            limit = min(MAX_RING_VERTICES, budget - MIN_RING_VERTICES * (len(rings) - k - 1))
            shape = simplify(ring, limit)
            if shape:
                shapes.append(_published(shape))
                budget -= len(shape)
        item['polygon'] = shapes
        left -= sum(len(s) for s in shapes)
        del item['_rank'], item['_distance']
    if len(json.dumps([public(i) for i in chosen], ensure_ascii=True, separators=(',', ':')).encode('utf-8')) > MAX_PAYLOAD_BYTES:
        raise ValueError('warnings payload too large; complete coverage unavailable')
    return chosen


def public(item):
    return {k: v for k, v in item.items() if not k.startswith('_')}


# --------------------------------------------------------------- the tracker
class Tracker:
    """Fetch schedule, last-good data and freshness. Thread-safe: the worker
    thread reports results, the emit tick reads `payload`.

    Freshness is published as a deadline (`staleAt`), not only a flag, so a
    page holding a frozen payload ages it on its own clock: the next refresh
    is due one cadence after the last success, and lands within
    STALE_GRACE_SEC of that; past it the data is stale. A failed refresh does
    not shorten that deadline: it is published as `refreshFailedAt` (the page
    keeps drawing last-good warnings, dimmed, and says the refresh failed) and
    the deadline from the last success still decides when they stop being shown."""

    def __init__(self):
        self._lock = threading.Lock()
        self.items = []            # last good, processed (with private _near)
        self.fetched = None        # epoch of the last successful (or known-empty) answer
        self.attempted = None      # epoch of the last attempt, success or not
        self.planned = None        # cadence (s) the last successful attempt was scheduled under
        self._attempt_interval = None
        self.failures = 0
        self.retry_after = None    # server-requested earliest retry (epoch)
        self.etag = None
        self.query = None          # the URL the last good answer was for
        self.coverage = None       # None unknown, False no NWS area in reach, True
        self.error = None
        self.failed_at = None      # epoch of the last failed attempt since the last success

    # ---- schedule ----
    def interval(self, fast):
        return FAST_SEC if fast else SLOW_SEC

    def next_due(self, now, fast, query):
        with self._lock:
            if self.attempted is None:
                return now
            if self.failures:
                # 2, 4, 8, 16, then every 30 minutes, whatever the cadence
                due = self.attempted + min(BACKOFF_MAX_SEC, RETRY_BASE_SEC * 2 ** (self.failures - 1))
                if self.retry_after is not None:
                    due = max(due, min(self.retry_after, self.attempted + BACKOFF_MAX_SEC))
                return due
            if query != self.query:
                return now                  # the reachable area changed: ask now
            return self.attempted + self.interval(fast)

    def due(self, now, fast, query):
        return now >= self.next_due(now, fast, query)

    # ---- results ----
    def began(self, now, fast=False):
        with self._lock:
            self.attempted = now
            self._attempt_interval = self.interval(fast)

    def no_coverage(self, now):
        with self._lock:
            self.items, self.fetched, self.attempted = [], now, now
            self.failures, self.retry_after, self.error, self.failed_at = 0, None, None, None
            self.coverage, self.query, self.etag = False, None, None

    def _success(self, now):
        self.fetched = self.attempted = now
        self.planned = self._attempt_interval or SLOW_SEC
        self.failures, self.retry_after, self.error, self.failed_at = 0, None, None, None

    def succeeded(self, now, items, query, etag=None):
        with self._lock:
            self._success(now)
            self.items = list(items)
            self.coverage, self.query, self.etag = True, query, etag

    def not_modified(self, now):
        with self._lock:
            self._success(now)

    def failed(self, now, error, retry_after=None):
        with self._lock:
            self.attempted = self.failed_at = now
            self.failures += 1
            self.error = str(error)[:200]
            self.retry_after = now + retry_after if retry_after else None

    # ---- reads ----
    def _stale_at(self, fast):
        if self.coverage is False or self.fetched is None:
            return None
        cadence = max(self.planned or SLOW_SEC, self.interval(fast))
        return int(math.ceil(min(self.fetched + STALE_MAX_SEC, self.fetched + cadence + STALE_GRACE_SEC)))

    def _stale(self, now, fast):
        if self.coverage is False:
            return False
        if self.fetched is None:
            return True
        return now >= self._stale_at(fast)

    def stale(self, now, fast=False):
        with self._lock:
            return self._stale(now, fast)

    def _live(self, now):
        return [i for i in self.items if i['expires'] is None or i['expires'] > now]

    def current(self, now):
        """Last-good items still in force at `now`."""
        with self._lock:
            return [public(i) for i in self._live(now)]

    def near(self, now):
        """In force at `now` and within the station's own (automatic) view."""
        with self._lock:
            return [public(i) for i in self._live(now) if i.get('_near')]

    def payload(self, now, fast=False):
        with self._lock:
            return dict(available=self.coverage is not False,
                        fetchedTs=int(self.fetched) if self.fetched is not None else None,
                        staleAt=self._stale_at(fast), stale=self._stale(now, fast),
                        refreshFailedAt=int(self.failed_at) if self.failed_at is not None else None,
                        items=[public(i) for i in self._live(now)])

    def health(self, now):
        with self._lock:
            return dict(coverage=self.coverage, fetchedTs=self.fetched, attemptedTs=self.attempted,
                        failures=self.failures, error=self.error, query=self.query, count=len(self.items))
