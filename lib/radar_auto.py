"""Auto, the only radar source policy: settled-camera site/mosaic choice and
spherical viewport coverage."""
import math

from lib.radar_geometry import EARTH_RADIUS_METERS, world_point, world_inverse

UP_ZOOM = 8
DOWN_ZOOM = 6
MIN_COVERAGE = .85
STAY_COVERAGE = .70
SWITCH_GUARD_SEC = 10


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
