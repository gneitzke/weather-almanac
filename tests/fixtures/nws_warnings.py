""" api.weather.gov `alerts/active` GeoJSON features, shaped after live responses
(2026-10-09: two Tornado Warnings and three Special Marine Warnings in Florida,
Freeze Warning updates in Washington). Places and polygons are moved to a
generic Puget Sound test station; identifiers are synthetic. Times are given
relative to a caller's `now` so expiry is deterministic.
"""
from datetime import datetime, timezone

STATION = (47.60, -122.30)            # a generic Puget Sound point, not a real station


def iso(epoch, offset_hours=-7):
    from datetime import timedelta
    tz = timezone(timedelta(hours=offset_hours))
    return datetime.fromtimestamp(epoch, tz).isoformat(timespec='seconds')


def _ident(n):
    return f'urn:oid:2.49.0.1.840.0.{n:040x}.001.1'


def feature(now, n=1, event='Tornado Warning', ring=None, geometry=None, message='Alert',
            references=(), status='Actual', sent_ago=120, ends_in=1500, expires_in=None,
            vtec_action='NEW', etn=51, office='KSEW', phen='TO', params=None):
    """ One feature. `ring` is a list of [lon, lat]; closed if not already. """
    if geometry is None and ring is not None:
        ring = list(ring)
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        geometry = {'type': 'Polygon', 'coordinates': [ring]}
    ident = _ident(n)
    parameters = {
        'AWIPSidentifier': ['TORSEW'], 'WMOidentifier': ['WFUS56 KSEW 092018'],
        'VTEC': [f'/O.{vtec_action}.{office}.{phen}.W.{etn:04d}.261009T2018Z-261009T2045Z/'],
        'eventEndingTime': [iso(now + ends_in)],
        'maxHailSize': ['0.00'], 'tornadoDetection': ['RADAR INDICATED'],
    }
    parameters.update(params or {})
    return {
        'id': f'https://api.weather.gov/alerts/{ident}', 'type': 'Feature', 'geometry': geometry,
        'properties': {
            '@id': f'https://api.weather.gov/alerts/{ident}', '@type': 'wx:Alert', 'id': ident,
            'areaDesc': 'King, WA', 'geocode': {'SAME': ['053033'], 'UGC': ['WAC033']},
            'affectedZones': ['https://api.weather.gov/zones/county/WAC033'],
            'references': [{'@id': f'https://api.weather.gov/alerts/{_ident(r)}', 'identifier': _ident(r),
                            'sender': 'w-nws.webmaster@noaa.gov', 'sent': iso(now - 900)} for r in references],
            'sent': iso(now - sent_ago), 'effective': iso(now - sent_ago), 'onset': iso(now - sent_ago),
            'expires': iso(now + (ends_in if expires_in is None else expires_in)),
            'ends': iso(now + ends_in) if ends_in is not None else None,
            'status': status, 'messageType': message, 'category': 'Met', 'severity': 'Extreme',
            'certainty': 'Observed', 'urgency': 'Immediate', 'event': event,
            'sender': 'w-nws.webmaster@noaa.gov', 'senderName': 'NWS Seattle WA',
            'headline': f'{event} issued October 9 at 1:18PM PDT until October 9 at 1:45PM PDT by NWS Seattle WA',
            'description': 'At 118 PM PDT, a severe thunderstorm capable of producing a tornado was located '
                           'near the test point, moving northeast at 25 mph.\n\nHAZARD...Tornado.',
            'instruction': 'TAKE COVER NOW!', 'response': 'Shelter', 'note': None,
            'parameters': parameters,
        },
    }


# A small storm polygon over the station (5 vertices, as the live TOR had).
OVER_STATION = [[-122.40, 47.55], [-122.20, 47.55], [-122.18, 47.68], [-122.38, 47.70]]
# One a few tens of km north-east, not covering the station.
NEARBY = [[-122.05, 47.85], [-121.85, 47.85], [-121.82, 47.98], [-122.03, 47.99]]
# Far outside the reachable disc (Florida's Big Bend, the live TOR's real shape).
FAR = [[-82.99, 29.58], [-83.12, 29.53], [-83.30, 29.72], [-83.04, 29.80]]


def multipolygon(*rings):
    return {'type': 'MultiPolygon', 'coordinates': [[list(r) + [r[0]]] for r in rings]}


def zone_based(now, n=90):
    """ A Freeze Warning update as live: zone-based, geometry null. """
    f = feature(now, n=n, event='Freeze Warning', geometry=None, message='Update', phen='FZ',
                references=(n + 1,))
    return f


def collection(*features):
    return {'@context': {'@version': '1.1'}, 'type': 'FeatureCollection', 'features': list(features),
            'title': 'Current watches, warnings, and advisories for Washington, Oregon',
            'updated': '2026-10-09T20:24:20+00:00'}


def big_ring(center_lon, center_lat, vertices=400, radius=0.4):
    import math
    return [[round(center_lon + radius * math.cos(2 * math.pi * i / vertices), 4),
             round(center_lat + radius * 0.7 * math.sin(2 * math.pi * i / vertices), 4)] for i in range(vertices)]
