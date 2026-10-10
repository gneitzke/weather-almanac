"""Radar acquisition, cache lifecycle, scheduling policy and publication.

RadarEngine owns every radar and storm-polygon mutable value. It receives
live config/forecast readers and an alerts-refresh callback, never an emitter
or screen. ProviderRuntime supplies the shared Clock registry and daemon
admission, preserving the original thread model and lifecycle fence.

Lock order (outermost first):
    runtime.lock (_life_lock) -> HostHealth.lock (either health tracker)
    -> _lock (formerly _radar_lock) -> RadarSession pool condition
    -> Attempt.lock.
Never call HostHealth while holding _lock: request admission and hedge claims
take these locks in the other order. Snapshot health before taking _lock.
Running native futures can outlive their frame; the ordering applies to them
as well as the coordinator and Clock thread.

Public boundary: start/stop; before_emit; payload_snapshot; tick;
health_snapshot and write_health. Clock/timer and worker details stay here.

Extracted from almanac_emit.py. Copyright (C) 2018-2025 Peter Davis
(classic console) / almanac add-on. Licensed under the GNU General Public
License, version 3 or later; provided without warranty. See LICENSE.
"""

from collections import Counter, OrderedDict, deque, namedtuple
from concurrent.futures import Future, ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timedelta, timezone
import json
import io
import hashlib
from functools import lru_cache
from pathlib import Path
from statistics import median
import math
import os
import re
import sys
from threading import Event as _Event, Thread as _InventoryThread, RLock as _RLock, BoundedSemaphore as _BoundedSemaphore
import time

from lib.radar_geometry import (world_point, world_inverse, parse_center,
                                circle_intersects_bounds, distance_meters)
from lib.radar_http import failure_class, RadarSession, is_transport_error, LocalTransportError, AmbiguousTransportError
from lib.radar_fetch import HostHealth, CircuitOpen, Attempt, AttemptCancelled, tile_race
from lib.radar_discovery import DiscoverySchedule
from lib.radar_attention import Attention, Signals, GlanceHistory, RANK, WARM_HOLD_SEC
from lib import nws_warnings
from lib import radar_auto
from lib.radar_native_budget import NativeBudget, native_allowed
import logging
# Pillow's PNG reader logs every chunk at DEBUG ("STREAM b'IDAT' ..."), and Kivy's
# root logger passes DEBUG through to its file handler: on the Pi that was ~800 SD-card
# writes per radar pass, serialising the four tile workers on the log lock (measured
# 1.7 s of a 2.4 s zoom pass). Third-party chatter never belongs in the console log.
logging.getLogger('PIL').setLevel(logging.INFO)

from lib.almanac_shared import (
    ALERTS_UA_FALLBACK, _cfg, _clock, _clock_style, _num, _json_safe,
    _snapshot_field, _station_tz,
)

RADAR_FAILURE_LOG_SEC = 5 * DiscoverySchedule.BACKOFF  # ten-minute outage reminders
RADAR_RETRY_SEC = 120
RADAR_ENABLED = os.environ.get('WFP_RADAR', '1') != '0'  # a kiosk with no way to show radar (tabs off) runs none of it
RADAR_ATTENTION_MODE = os.environ.get('WFP_RADAR_ATTENTION', 'active')  # 'active' applies the tiers; 'shadow' only publishes them
RADAR_SENTINEL_ZOOM = 7  # four tiles ≈ 425 km across at 47.6 N (zoom 5 spanned ~1,700 km: weather that never arrives)
RADAR_ECHO_MIN_SHARE = 0.001  # weather pixels (>= 25 dBZ) as a share of the footprint before it counts as echo;
                              # a dry night's KATX frame measured 0.04 % at 25 dBZ, real showers 2 %
RADAR_ATTENTION_FORCE_TTL = 7200
RADAR_VIEWING_LAPSE_SEC = 60  # the page clears radar_viewing when it leaves the tab; silence alone must last this long
RADAR_STARTING_MAX_SEC = 300  # after this, a radar that never produced a result is unavailable, not starting
RADAR_LOCAL_RETRY_MAX_SEC = 60  # ceiling for the doubling retry after consecutive local failures
RADAR_HISTORY_SEC = 3600
RADAR_IEM_FRAME_INTERVAL_SEC = 120
RADAR_RAINVIEWER_FRAME_INTERVAL_SEC = 600
RADAR_IEM_STALE_SEC = 600
RADAR_RAINVIEWER_STALE_SEC = 1200
# Politeness cap for one console against a public tile cache. The window fits
# the eight-frame loop and adjacent newest tiles; deep history uses only capacity
# above their headroom floor. The native-tile LRU avoids spending it on re-zooms.
RADAR_REQUESTS_PER_MIN = 240
# Two worst-case frames (5x3 mosaic tiles + metadata + archive probe each): history
# backfill must leave the NEXT interaction's newest frame able to start at once even
# while the previous zoom's history is still streaming. One frame's worth left a
# zoom's first echoes waiting 3-4 s for headroom on the Pi.
RADAR_HISTORY_RESERVE = 34
RADAR_MAX_FRAME_BUILDS_PER_PASS = 20
RADAR_HTTP_TIMEOUT_SEC = 8
RADAR_TILE_TIMEOUT_SEC = 6
RADAR_HEDGE_SEC = 2
RADAR_SOURCE_DEADLINE_SEC = 16
# Preserve the tight primary deadline; persistent IPv4 TLS and readiness lag
# address acquisition cost rather than hiding it behind a longer timeout.
RADAR_PRIMARY_DEADLINE_SEC = 25
RADAR_BUILD_DEADLINE_SEC = RADAR_PRIMARY_DEADLINE_SEC
RADAR_TILE_CACHE_SIZE = 400
RADAR_TILE_WORKERS = 4
RADAR_NEWEST_TILE_WORKERS = 6
RADAR_PREFETCH_HEADROOM = 60
RADAR_LOOP_FRAMES = 8
RADAR_DEEP_VIEW_SEC = 20
RADAR_VIEW_POLL_GAP_SEC = 5  # normal wx.json polling is every two seconds
RADAR_INTENT_CHECK_SEC = .1
RADAR_GEO_QUANTUM_SEC = .25
RADAR_GEO_UNVIEWED_SLEEP_SEC = .05
RADAR_NEGATIVE_CACHE_SEC = 120
RADAR_CACHE_GRACE_SEC = 120
RADAR_IEM_METADATA_URL = "https://mesonet.agron.iastate.edu/data/gis/images/4326/mrms/lcref.json"
RADAR_IEM_TILE_TEMPLATE = "https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/mrms::lcref-{stamp}/{z}/{x}/{y}.png"
RADAR_IEM_ARCHIVE_TEMPLATE = "https://mesonet.agron.iastate.edu/archive/data/%Y/%m/%d/GIS/mrms/lcref_%Y%m%d%H%M.png"
RADAR_SITE_MIN_ZOOM = 7
RADAR_SITE_RANGE_METERS = 230000
RADAR_SITE_MAX_COUNT = 4
RADAR_SITE_MAX_AGE_SEC = 900
# The oldest stamp any view can show: a newest frame up to its source's
# acceptance age (site 15 min, RainViewer 20 min, MRMS 10 min) plus the hour of
# history behind it. Older cached stamps are deleted at boot without opening.
RADAR_CACHE_RETENTION_SEC = RADAR_HISTORY_SEC + max(RADAR_SITE_MAX_AGE_SEC, RADAR_IEM_STALE_SEC, RADAR_RAINVIEWER_STALE_SEC)
RADAR_CACHE_RETRY_SEC = 5          # first retry of a failed cache boot, doubling
RADAR_CACHE_RETRY_MAX_SEC = 300
RADAR_SITE_STALE_MIN_SEC = 480     # a site frame is never stale sooner than 8 minutes
RADAR_SITE_STALE_MAX_SEC = 1200    # ... and always stale after 20 (latency + two intervals, bounded)
RADAR_SITE_LATENCY_DEFAULT_SEC = 300   # publication latency assumed before a site has measured samples
RADAR_SITE_LATENCY_SAMPLES = 12        # first-seen latency samples kept per site
RADAR_SITE_LATENCY_MIN_SAMPLES = 3     # fewer than this: the default applies
RADAR_SITE_LATENCY_GAP_SEC = 180       # a sample counts only when the previous listing was this recent
RADAR_SITE_LATENCY_MAX_AGE_SEC = 7200  # samples older than this no longer count (12 scans at a 600 s cadence)
RADAR_HEALTH_WRITE_SEC = 15        # radar-health.json cadence (sooner on a state change)
RADAR_MIN_ZOOM = 4
RADAR_MAX_ZOOM = 9
RADAR_TARGET_METERS = 200000
RADAR_VIEW_TTL = 900
RADAR_VIEWPORT_W = 956
RADAR_VIEWPORT_H = 490
RADAR_VIEWPORT_PX = RADAR_VIEWPORT_H  # short-axis scale compatibility
# MRMS on-demand tiles routinely trail metadata: predict their publication window.
RADAR_IEM_READY_LAG_SEC = 300  # readiness prediction only; never suppress an advertised scan
RADAR_SITE_LIST_URL = "https://mesonet.agron.iastate.edu/json/radar.py"
# Intent-record fields of the retired manual source choice (serve.py strips the
# same set): readers drop them from records written before the upgrade.
RADAR_RETIRED_INTENT_FIELDS = frozenset(('source', 'sourceAcceptedAt'))
RADAR_SITE_TILE_TEMPLATE = "https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/ridge::{site}-N0B-{stamp}/{z}/{x}/{y}.png"
# v2: NOAA's own Level III product, public on AWS (NOAA Open Data Dissemination).
RADAR_LEVEL3_BUCKET = "https://unidata-nexrad-level3.s3.amazonaws.com/"
RADAR_LEVEL3_TRANSPORT = 'noaa-level3-n0b'  # required reflectivity dependency
RADAR_N0H_TRANSPORT = 'noaa-level3-n0h'    # optional QC cannot hold reflectivity
RADAR_LEVEL3_TRANSPORTS = (RADAR_LEVEL3_TRANSPORT, RADAR_N0H_TRANSPORT)
# Decoded scans (~1.3 MB each). One loop is every contributing site's scan for
# each frame, plus the next scan per site as it lands; a smaller LRU walked in
# frame order misses on every access, and each zoom step re-downloaded
# products (measured on the Pi 4 at 24: 1.3 MB per step).
RADAR_LEVEL3_SCAN_CACHE = RADAR_SITE_MAX_COUNT * (RADAR_LOOP_FRAMES + 1)
RADAR_N0H_FRAME_BUDGET_SEC = 2.5
RADAR_N0H_UPGRADE_SEC = 180
# Two products, two boundary hours, plus two hours of headroom.
RADAR_LEVEL3_LISTING_CACHE = RADAR_SITE_MAX_COUNT * len(RADAR_LEVEL3_TRANSPORTS) * 4
RADAR_LEVEL3_UNPUBLISHED_LOG_SEC = 600  # allow IEM-to-S3 publication lag
RADAR_LEVEL3_FALLBACK_SEC = 120   # draw IEM site tiles before retrying Level III
RADAR_LEVEL3_COOLDOWN_MAX_SEC = 300  # cap provider Retry-After; retry native within five minutes
RADAR_LEVEL3_RETRY_SEC = 60       # a scan S3 lacks is not re-requested per tile
RADAR_RAINVIEWER_COLOR = 2
RADAR_RAINVIEWER_TILE_OPTS = "0_0"
RADAR_RAINVIEWER_MANIFEST_URL = "https://api.rainviewer.com/public/weather-maps.json"
RADAR_DIR = os.environ.get("WFP_RADAR_DIR", os.path.expanduser("~/almanac_web/radar"))
from lib.radar_palette import _RADAR_RAMP, _RADAR_LUT, _RADAR_DISPLAY_RAMP, REMAP_REVISION, SMOOTH_REVISION, remap, smooth_remap, source_palette
_RADAR_SOURCES = {
    'iem-nexrad-n0b': dict(provider='iem', attribution='IEM / NOAA',
        attribution_url='https://mesonet.agron.iastate.edu/GIS/ridge.phtml',
        cadence=300, stale_sec=900, legend=_RADAR_DISPLAY_RAMP, max_zoom=10),
    'iem-mrms-lcref': dict(provider='iem', attribution='IEM / NOAA MRMS',
        attribution_url='https://mesonet.agron.iastate.edu/ogc/',
        cadence=RADAR_IEM_FRAME_INTERVAL_SEC, stale_sec=RADAR_IEM_STALE_SEC,
        legend=_RADAR_DISPLAY_RAMP, max_zoom=9),
    'rainviewer': dict(provider='rainviewer', attribution='RainViewer',
        attribution_url='https://www.rainviewer.com/',
        cadence=RADAR_RAINVIEWER_FRAME_INTERVAL_SEC, stale_sec=RADAR_RAINVIEWER_STALE_SEC,
        legend=_RADAR_DISPLAY_RAMP, max_zoom=7),
}
# Coarse station-center CONUS land mask (lon, lat), deliberately independent of
# the nearest-NEXRAD caption. Coast/border detail is approximate, not geocoding.
_RADAR_CONUS = ((-124.73,48.4), (-123.1,48.3), (-123.1,49), (-95.16,49),
    (-95.16,49.38), (-94.8,49.38), (-94.6,48.7), (-92.7,48.55), (-90.9,48.25),
    (-89.5,48), (-88.4,48.3), (-84.9,46.9), (-84.5,46.45), (-84.1,46.5),
    (-83.5,45.8), (-82.5,45.3), (-82.1,43.6), (-82.42,43), (-82.42,42.5),
    (-83.1,42.3), (-83.15,42.05), (-83,41.7), (-82.7,41.7), (-80.5,42.3),
    (-79.1,42.85), (-79.05,43.45), (-76.8,43.6), (-76.45,44.1), (-74.7,45),
    (-71.5,45), (-71.4,45.25), (-70.7,45.4), (-70,46.4), (-69.2,47.45),
    (-68.3,47.35), (-67.8,47), (-67.8,45.7), (-67,44.8), (-70,43),
    (-69.8,41.5), (-72,41), (-74,40.5), (-75.5,35.2), (-80.5,32),
    (-80,26.7), (-80.05,25.6), (-80.4,25.1), (-81.2,24.5), (-82,26), (-82.8,28),
    (-84,30), (-89,29), (-90,29), (-93.8,29.7), (-97.15,25.95),
    (-97.5,25.85), (-99.1,26.4), (-99.5,27.5), (-100.3,28.3),
    (-101.4,29.8), (-102.8,29.2), (-103,29), (-104.5,29.65),
    (-106.5,31.78), (-108.2,31.78), (-108.2,31.33), (-111.07,31.33),
    (-114.72,32.72), (-117.13,32.54), (-120.6,34.5), (-122.5,37.7),
    (-124.4,40.4), (-124.6,42), (-124,46.3))
# Live original PNG was 7000 x 3500, .01 degrees/cell, world-file upper-left
# center (-129.995, 54.995): outer edges west/east -130/-60, south/north 20/55.
_RADAR_IEM_DOMAIN = dict(w=-130, e=-60, s=20, n=55)



# NOAA NCEI HOMR station inventory, verified 2026-09-12:
# https://www.ncei.noaa.gov/access/homr/file/nexrad-stations.txt
# 160 WSR-88D sites; excludes TDWR, test radars KCRI/KOUN, retired KLIX (KHDC replaces it).
_NEXRAD_SITES = {
    'KABR': (45.455833, -98.413333, 'Aberdeen'),
    'KABX': (35.149722, -106.82388, 'Albuquerque'),
    'KAKQ': (36.98405, -77.007361, 'Norfolk Rich'),
    'KAMA': (35.233333, -101.70927, 'Amarillo'),
    'KAMX': (25.611083, -80.412667, 'Miami'),
    'KAPX': (44.90635, -84.719533, 'Gaylord'),
    'KARX': (43.822778, -91.191111, 'La Crosse'),
    'KATX': (48.194611, -122.49569, 'Camano Island'),
    'KBBX': (39.495639, -121.63161, 'Beale Afb'),
    'KBGM': (42.199694, -75.984722, 'Binghamton'),
    'KBHX': (40.498583, -124.29216, 'Eureka'),
    'KBIS': (46.770833, -100.76055, 'Bismarck'),
    'KBLX': (45.853778, -108.6068, 'Billings'),
    'KBMX': (33.172417, -86.770167, 'Birmingham'),
    'KBOX': (41.955778, -71.136861, 'Boston'),
    'KBRO': (25.916, -97.418967, 'Brownsville'),
    'KBUF': (42.948789, -78.736781, 'Buffalo'),
    'KBYX': (24.5975, -81.703167, 'Key West'),
    'KCAE': (33.948722, -81.118278, 'Columbia'),
    'KCBW': (46.03925, -67.806431, 'Houlton'),
    'KCBX': (43.490217, -116.23603, 'Boise'),
    'KCCX': (40.923167, -78.003722, 'State College'),
    'KCLE': (41.413217, -81.859867, 'Cleveland'),
    'KCLX': (32.655528, -81.042194, 'Charleston'),
    'KCRP': (27.784017, -97.51125, 'Corpus Christi'),
    'KCXX': (44.511, -73.166431, 'Burlington'),
    'KCYS': (41.151919, -104.80603, 'Cheyenne'),
    'KDAX': (38.501111, -121.67783, 'Sacramento'),
    'KDDC': (37.760833, -99.968889, 'Dodge City'),
    'KDFX': (29.273139, -100.28033, 'Laughlin Afb'),
    'KDGX': (32.279944, -89.984444, 'Jackson Brandon'),
    'KDIX': (39.947089, -74.410731, 'Philadelphia'),
    'KDLH': (46.836944, -92.209722, 'Duluth'),
    'KDMX': (41.7312, -93.722869, 'Des Moines'),
    'KDOX': (38.825767, -75.440117, 'Dover Afb'),
    'KDTX': (42.7, -83.471667, 'Detroit'),
    'KDVN': (41.611667, -90.580833, 'Davenport'),
    'KDYX': (32.5385, -99.254333, 'Dyess Afb'),
    'KEAX': (38.81025, -94.264472, 'Kansas City'),
    'KEMX': (31.89365, -110.63025, 'Tucson'),
    'KENX': (42.586556, -74.064083, 'Albany'),
    'KEOX': (31.460556, -85.459389, 'Fort Rucker'),
    'KEPZ': (31.873056, -106.698, 'El Paso'),
    'KESX': (35.70135, -114.89165, 'Las Vegas'),
    'KEVX': (30.565033, -85.921667, 'Eglin Afb'),
    'KEWX': (29.704056, -98.028611, 'Austin San Antonio'),
    'KEYX': (35.09785, -117.56075, 'Edwards'),
    'KFCX': (37.0244, -80.273969, 'Roanoke'),
    'KFDR': (34.362194, -98.976667, 'Altus Afb'),
    'KFDX': (34.634167, -103.61888, 'Cannon Afb'),
    'KFFC': (33.36355, -84.56595, 'Atlanta'),
    'KFSD': (43.587778, -96.729444, 'Sioux Falls'),
    'KFSX': (34.574333, -111.19844, 'Flagstaff'),
    'KFTG': (39.786639, -104.5458, 'Denver Front Range Ap'),
    'KFWS': (32.573, -97.30315, 'Dallas'),
    'KGGW': (48.206361, -106.62469, 'Glasgow'),
    'KGJX': (39.062169, -108.21376, 'Grand Junction'),
    'KGLD': (39.366944, -101.70027, 'Goodland'),
    'KGRB': (44.498633, -88.111111, 'Green Bay'),
    'KGRK': (30.721833, -97.382944, 'Fort Hood'),
    'KGRR': (42.893889, -85.544889, 'Grand Rapids'),
    'KGSP': (34.883306, -82.219833, 'Greer'),
    'KGWX': (33.896917, -88.329194, 'Columbus Afb'),
    'KGYX': (43.891306, -70.256361, 'Portland'),
    'KHDC': (30.5193, -90.4074, 'Hammond Municipal Airport'),
    'KHDX': (33.077, -106.12003, 'Holloman Afb'),
    'KHGX': (29.4719, -95.078733, 'Houston'),
    'KHNX': (36.314181, -119.63213, 'San Joaquin Valley'),
    'KHPX': (36.736972, -87.285583, 'Fort Campbell'),
    'KHTX': (34.930556, -86.083611, 'Huntsville'),
    'KICT': (37.654444, -97.443056, 'Wichita'),
    'KICX': (37.59105, -112.86218, 'Cedar City'),
    'KILN': (39.420483, -83.82145, 'Cincinnati'),
    'KILX': (40.1505, -89.336792, 'Lincoln'),
    'KIND': (39.7075, -86.280278, 'Indianapolis'),
    'KINX': (36.175131, -95.564161, 'Tulsa'),
    'KIWA': (33.289233, -111.66991, 'Phoenix'),
    'KIWX': (41.358611, -85.7, 'Fort Wayne'),
    'KJAX': (30.484633, -81.7019, 'Jacksonville'),
    'KJGX': (32.675683, -83.350833, 'Robins Afb'),
    'KJKL': (37.590833, -83.313056, 'Jackson'),
    'KLBB': (33.654139, -101.81416, 'Lubbock'),
    'KLCH': (30.125306, -93.215889, 'Lake Charles'),
    'KLGX': (47.116944, -124.10666, 'Langley Hill'),
    'KLNX': (41.957944, -100.57622, 'North Platte'),
    'KLOT': (41.604444, -88.084444, 'Chicago'),
    'KLRX': (40.73955, -116.8027, 'Elko'),
    'KLSX': (38.698611, -90.682778, 'St Louis'),
    'KLTX': (33.98915, -78.429108, 'Wilmington'),
    'KLVX': (37.975278, -85.943889, 'Louisville'),
    'KLWX': (38.976111, -77.4875, 'Sterling'),
    'KLZK': (34.8365, -92.262194, 'Little Rock'),
    'KMAF': (31.943461, -102.18925, 'Midland Odessa'),
    'KMAX': (42.081169, -122.71736, 'Medford'),
    'KMBX': (48.393056, -100.86444, 'Minot Afb'),
    'KMHX': (34.775908, -76.876189, 'Morehead City'),
    'KMKX': (42.9679, -88.550667, 'Milwaukee'),
    'KMLB': (28.113194, -80.654083, 'Melbourne'),
    'KMOB': (30.679444, -88.24, 'Mobile'),
    'KMPX': (44.848889, -93.565528, 'Minneapolis'),
    'KMQT': (46.531111, -87.548333, 'Marquette'),
    'KMRX': (36.168611, -83.401944, 'Knoxville'),
    'KMSX': (47.041, -113.98622, 'Missoula'),
    'KMTX': (41.262778, -112.44777, 'Salt Lake City'),
    'KMUX': (37.155222, -121.89844, 'San Francisco'),
    'KMVX': (47.527778, -97.325556, 'Grand Forks'),
    'KMXX': (32.53665, -85.78975, 'Maxwell Afb'),
    'KNKX': (32.919017, -117.0418, 'San Diego'),
    'KNQA': (35.344722, -89.873333, 'Memphis'),
    'KOAX': (41.320369, -96.366819, 'Omaha'),
    'KOHX': (36.247222, -86.5625, 'Nashville'),
    'KOKX': (40.865528, -72.863917, 'New York City'),
    'KOTX': (47.680417, -117.62677, 'Spokane'),
    'KPAH': (37.068333, -88.771944, 'Paducah'),
    'KPBZ': (40.531717, -80.217967, 'Pittsburgh'),
    'KPDT': (45.69065, -118.85293, 'Pendleton'),
    'KPOE': (31.155278, -92.976111, 'Fort Polk'),
    'KPUX': (38.45955, -104.18135, 'Pueblo'),
    'KRAX': (35.665519, -78.48975, 'Raleigh Durham'),
    'KRGX': (39.754056, -119.46202, 'Reno'),
    'KRIW': (43.066089, -108.4773, 'Riverton'),
    'KRLX': (38.311111, -81.722778, 'Charleston'),
    'KRTX': (45.715039, -122.965, 'Portland'),
    'KSFX': (43.1056, -112.68613, 'Pocatello'),
    'KSGF': (37.235239, -93.400419, 'Springfield'),
    'KSHV': (32.450833, -93.84125, 'Shreveport'),
    'KSJT': (31.371278, -100.4925, 'San Angelo'),
    'KSOX': (33.817733, -117.636, 'Santa Ana Mountains'),
    'KSRX': (35.290417, -94.361889, 'Fort Smith'),
    'KTBW': (27.7055, -82.401778, 'Tampa'),
    'KTFX': (47.459583, -111.38533, 'Great Falls'),
    'KTLH': (30.397583, -84.328944, 'Tallahassee'),
    'KTLX': (35.333361, -97.277761, 'Oklahoma City'),
    'KTWX': (38.99695, -96.23255, 'Topeka'),
    'KTYX': (43.755694, -75.679861, 'Fort Drum'),
    'KUDX': (44.124722, -102.83, 'Rapid City'),
    'KUEX': (40.320833, -98.441944, 'Hastings'),
    'KVAX': (30.890278, -83.001806, 'Moody Afb'),
    'KVBX': (34.83855, -120.39791, 'Vandenberg Afb'),
    'KVNX': (36.740617, -98.127717, 'Vance Afb'),
    'KVTX': (34.412017, -119.17875, 'Los Angeles'),
    'KVWX': (38.26025, -87.724528, 'Evansville'),
    'KYUX': (32.495281, -114.65671, 'Yuma'),
    'LPLA': (38.73028, -27.32167, 'Lajes Ab'),
    'PABC': (60.791944, -161.87638, 'Bethel Faa'),
    'PACG': (56.852778, -135.52916, 'Sitka'),
    'PAEC': (64.511389, -165.295, 'Nome'),
    'PAHG': (60.725914, -151.35146, 'Anchorage'),
    'PAIH': (59.460767, -146.30344, 'Middleton Island'),
    'PAKC': (58.679444, -156.62944, 'King Salmon'),
    'PAPD': (65.035114, -147.50143, 'Fairbanks'),
    'PGUA': (13.455833, 144.811111, 'Andersen Afb Agana'),
    'PHKI': (21.893889, -159.5525, 'South Kauai'),
    'PHKM': (20.125278, -155.77777, 'Kamuela'),
    'PHMO': (21.132778, -157.18027, 'Molokai'),
    'PHWA': (19.095, -155.56888, 'South Shore'),
    'RKJK': (35.924167, 126.622222, 'Kunsan'),
    'RKSG': (37.207569, 127.285561, 'Camp Humphreys'),
    'RODN': (26.3078, 127.903469, 'Kadena'),
    'TJUA': (18.115667, -66.078167, 'San Juan'),
}


@lru_cache(maxsize=128)
def _radar_iem_eligible(lat, lon):
    if not (math.isfinite(lat) and math.isfinite(lon) and 20 < lat < 55 and -130 < lon < -60):
        return False
    inside = False
    x0, y0 = _RADAR_CONUS[-1]
    for x1, y1 in _RADAR_CONUS:
        if (y0 > lat) != (y1 > lat) and lon < (x1 - x0) * (lat - y0) / (y1 - y0) + x0:
            inside = not inside
        x0, y0 = x1, y1
    return inside


class _RadarSuperseded(Exception):
    """The current preference generation no longer owns this pass."""


class _RadarRevalidate(Exception):
    """Remembered newest tiles failed; rediscover the source in this pass."""


class _RadarUnchanged(Exception):
    """A discovery-only pass found the current complete scan."""


class _RadarClassificationPending(Exception):
    """This optional target can be retried without aborting other targets."""


class _RadarBudget(Exception):
    """Yield unfinished history to a later pass; never bypass the shared limit."""


def _radar_zoom_for(lat):
    """Target 200 km across the short axis; the adapter applies its source bounds."""
    zoom = round(math.log2(156543.03392 * math.cos(math.radians(lat)) /
                          (RADAR_TARGET_METERS / RADAR_VIEWPORT_PX)))
    return max(RADAR_MIN_ZOOM, min(RADAR_MAX_ZOOM, zoom))


def _radar_viewport(lat, lon, zoom, size, height=None):
    """Pure Web Mercator viewport geometry; offsets refer to unwrapped world pixels."""
    lat = max(-85.05112878, min(85.05112878, lat))
    n = 2 ** zoom
    world = n * 256
    cx, cy = world_point(lat, lon, zoom)
    height = size if height is None else height
    left, top = cx - size / 2, cy - height / 2
    tiles = [(tx % n, ty, int(tx * 256 - left), int(ty * 256 - top))
             for ty in range(max(0, math.floor(top / 256)),
                             min(n, math.ceil((top + height) / 256)))
             for tx in range(math.floor(left / 256), math.ceil((left + size) / 256))]
    def latitude(y):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / world))))
    def longitude(x):
        return (x / world * 360) % 360 - 180
    bounds = dict(n=latitude(top), s=latitude(top + height),
                  e=longitude(left + size), w=longitude(left))
    return tiles, 156543.03392 * math.cos(math.radians(lat)) / n, bounds, (cx - left, cy - top)


def _radar_distance_unit(config):
    # Same Units/Distance setting used by observation_format.units for lightning.
    return 'mi' if str(_cfg(config, 'Units', 'Distance') or '').lower() in ('mi', 'miles') else 'km'


def _radar_scale(mpp, size, unit, max_fraction=.4):
    factor = 1609.344 if unit == 'mi' else 1000
    choices = [d for d in (5, 10, 20, 25, 50, 100, 150, 200, 250)
               if d * factor / mpp <= size * max_fraction]
    if not choices:  # extreme polar latitudes: no listed distance fits
        return dict(distDisp='0 ' + unit, meters=0, pixels=0, unit=unit), ()
    dist = max(choices)
    pixels = dist * factor / mpp
    bar = dict(distDisp=f'{dist} {unit}', meters=dist * factor, pixels=pixels, unit=unit)
    rings = tuple(dict(label=f'{dist * multiple} {unit}', px=pixels * multiple)
                  for multiple in (1, 2) if pixels * multiple <= size / math.sqrt(2))
    return bar, rings


def _radar_nexrad(lat, lon, unit):
    """Nearest WSR-88D and unrounded eligibility distance; station -> radar bearing."""
    nearest = None
    a = math.radians(lat)
    for ident, (site_lat, site_lon, name) in _NEXRAD_SITES.items():
        b, dl = math.radians(site_lat), math.radians(site_lon - lon)
        h = math.sin((b - a) / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(dl / 2) ** 2
        meters = 6371008.8 * 2 * math.asin(math.sqrt(min(1, h)))
        if nearest is None or meters < nearest[0]:
            bearing = math.degrees(math.atan2(math.sin(dl) * math.cos(b),
                                  math.cos(a) * math.sin(b) - math.sin(a) * math.cos(b) * math.cos(dl))) % 360
            nearest = meters, ident, name, bearing
    if nearest is None or nearest[0] > 285 * 1609.344:
        return None
    meters, ident, name, bearing = nearest
    dist = round(meters / (1609.344 if unit == 'mi' else 1000))
    return dict(id=ident, name=name, distanceMeters=meters, distanceDisp=f'{dist} {unit}',
                bearing=('N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW')[int((bearing + 22.5) / 45) % 8])


def _radar_sites(station, bounds):
    """Cap by viewport distance; station distance only chooses the primary."""
    center = (math.degrees(math.atan(math.sinh((math.asinh(math.tan(math.radians(bounds['n'])))+math.asinh(math.tan(math.radians(bounds['s']))))/2))),
              (bounds['w']+(bounds['e']-bounds['w']) % 360/2+180) % 360-180)
    sites = [dict(id=ident, lat=lat, lon=lon, distanceMeters=distance_meters(*station, lat, lon),
                  viewportDistanceMeters=distance_meters(*center, lat, lon))
             for ident, (lat, lon, _) in _NEXRAD_SITES.items()
             if circle_intersects_bounds(lat, lon, RADAR_SITE_RANGE_METERS, bounds)]
    sites.sort(key=lambda s: (s['viewportDistanceMeters'], s['id']))
    return list(reversed(sites[:RADAR_SITE_MAX_COUNT])), len(sites)


@lru_cache(maxsize=512)
def _radar_covered_tiles(lat, lon, zoom, radius, tiles):
    def intersects(tile):
        x, y = tile[:2]
        n, w = world_inverse(x*256, y*256, zoom)
        south, e = world_inverse((x+1)*256, (y+1)*256, zoom)
        return circle_intersects_bounds(lat, lon, radius, dict(n=n, s=south, w=w, e=e))
    return tuple(tile for tile in tiles if intersects(tile))


def _radar_site_tiles(ctx, site):
    if site is None or site.startswith('M'):
        return ctx['tiles']
    lat, lon, _ = _NEXRAD_SITES[site]
    return _radar_covered_tiles(lat,lon,ctx['zoom'],RADAR_SITE_RANGE_METERS,tuple(ctx['tiles']))


@lru_cache(maxsize=1)
def _radar_native_table_revision():
    from lib.radar_palette import _FILES
    return hashlib.sha256(b''.join((Path(__file__).parent/'data'/name).read_bytes()
                                  for name in sorted(_FILES.values()))).hexdigest()


@lru_cache(maxsize=8)
def _radar_revision_digest(remap_revision,basemap_revision,native_revision):
    # Render constants are code, fixed for a process. Compute their identity once
    # per revision, rather than serializing three palettes for every inventory stat.
    pixels=repr([(source,source_palette(source)) for source in sorted(_RADAR_SOURCES)])
    return hashlib.sha256(('tile-wire-visible-v41-1'+remap_revision+repr(_RADAR_RAMP)+
                          repr(_RADAR_DISPLAY_RAMP)+pixels+native_revision+basemap_revision).encode()).hexdigest()[:12]


def _radar_variant_revision(variant):
    """PNG identity for plain/smoothed IEM and plain/smoothed Level III."""
    from lib.radar_level3 import NATIVE_REVISION, NATIVE_SMOOTH_REVISION
    return (NATIVE_SMOOTH_REVISION if variant == 'native-smooth' else
            NATIVE_REVISION if variant == 'native' else SMOOTH_REVISION if variant else REMAP_REVISION)


def _radar_variant(ctx, source):
    # v2 mosaics native NEXRAD gates; Region keeps its existing renderer.
    return ('native-smooth' if ctx.get('smooth') else 'native') if (source == 'iem-nexrad-n0b' and native_allowed(
        ctx.get('native'), ctx.get('attention'), ctx.get('native_ceiling', 'normal'))) else bool(ctx.get('smooth', False))


def _radar_render_revision(smooth=False):
    from lib.radar_basemap import version
    suffix = "" if smooth is False else _radar_variant_revision(smooth)
    return _radar_revision_digest(REMAP_REVISION + suffix,version(),_radar_native_table_revision())


RADAR_RENDER_VARIANTS = (False, True, 'native', 'native-smooth')


def _radar_is_native(variant):
    return variant in ('native', 'native-smooth')


def _radar_sites_revision():
    return hashlib.sha256(json.dumps(_NEXRAD_SITES,sort_keys=True).encode()).hexdigest()[:12]


def _radar_tile_path(source, site, stamp, zoom, x, y, smooth=False):
    if isinstance(stamp, (int, float)):
        stamp = datetime.fromtimestamp(stamp, timezone.utc).strftime('%Y%m%d%H%M')
    return Path(RADAR_DIR) / 't' / _radar_render_revision(smooth) / source / (site or '-') / stamp / str(zoom) / str(x % 2**zoom) / f'{y}.png'


def _radar_disk_key(source, site, stamp, zoom, x, y, smooth=False):
    key = (source, site, _radar_stamp_text(stamp), zoom, x % 2**zoom, y)
    return key + (smooth,) if smooth else key


def _radar_tile_metadata(path, source):
    from PIL import Image
    from lib.radar_palette import weather_pixels
    with Image.open(path) as image:
        variant = next((v for v in RADAR_RENDER_VARIANTS[1:] if len(path.parents) > 5 and
                        path.parents[5].name == _radar_render_revision(v)), False)
        if image.format != 'PNG' or image.size != (256,256):
            raise ValueError('invalid cached tile')
        image.load()
        meta=json.loads(image.info['radarRemap'])
        if meta['revision'] != _radar_variant_revision(variant) or type(meta['remapped']) is not bool:
            raise ValueError('cached tile revision')
        for key in ('unmatchedColors','opaqueColors','unmatchedPixels','opaquePixels','ambiguousPixels'):
            if type(meta[key]) is not int or meta[key]<0: raise ValueError('cached tile counts')
        if (meta['opaquePixels']>65536 or meta['unmatchedColors']>meta['opaqueColors'] or
                meta['unmatchedPixels']>meta['opaquePixels'] or meta['ambiguousPixels']>meta['opaquePixels'] or
                meta['remapped'] != (meta['unmatchedPixels']<=.02*meta['opaquePixels'] and not meta['ambiguousPixels'])):
            raise ValueError('cached tile honesty')
        allowed={color[:3] for _,color in source_palette(source)}
        with image.convert('RGBA') as rgba:
            colors=rgba.getcolors(image.width*image.height)
            if any(color[3] and color[:3] not in allowed for _,color in colors): raise ValueError('cached tile palette')
            visible=sum(n for n,color in colors if color[3])
            if int(image.info['radarVisiblePixels'])!=visible:raise ValueError('cached tile visibility count')
            if variant is not True and visible>meta['opaquePixels']-meta['unmatchedPixels']:
                raise ValueError('cached tile visibility')
        if _radar_is_native(variant):
            # A visible pixel is a measured one: the two counts cannot overlap.
            uncovered = image.info['radarUncoveredPixels']
            if not re.fullmatch(r'[0-9]{1,5}', uncovered) or int(uncovered)+visible > 65536:
                raise ValueError('cached tile coverage count')
            grid = image.info['radarMeasuredGrid']
            _radar_check_grid(grid, int(uncovered))
            meta = dict(meta, uncoveredPixels=int(uncovered), measuredGrid=grid)
        return dict(meta, weatherPixels=weather_pixels(image))


def _radar_check_grid(grid, uncovered):
    """A native tile's radarMeasuredGrid must agree with its uncovered count:
    a fully measured cell holds no uncovered pixel, and a tile with none
    uncovered has every cell measured. Raises ValueError."""
    from lib.radar_mosaic import grid_cells
    cells = int(grid_cells(grid).sum())
    if cells*256 > 65536-uncovered or (uncovered == 0) != (cells == 256):
        raise ValueError('cached tile coverage grid')


@lru_cache(maxsize=256)
def _radar_disc_grid(site, zoom, x, y):
    """Cells of tile (zoom, x, y) wholly inside a site's range disc, for site
    layers drawn from IEM tiles, which carry no measured grid. A cell counts
    when all four of its corners are in range: the disc is convex at this
    scale, so its corners in range put the whole cell in range. Hex as
    radarMeasuredGrid; '0'*64 for an unknown site."""
    from lib.radar_mosaic import GRID_CELLS
    import numpy as np
    if site not in _NEXRAD_SITES:
        return '0' * (GRID_CELLS*GRID_CELLS//4)
    lat, lon, _ = _NEXRAD_SITES[site]
    edge = np.arange(GRID_CELLS+1) * (256 // GRID_CELLS)
    n = 2**zoom * 256
    lons = np.radians((x*256 + edge) / n * 360 - 180)
    lats = np.arctan(np.sinh(np.pi * (1 - 2 * (y*256 + edge) / n)))
    a, b = math.radians(lat), math.radians(lon)
    la, lo = lats[:, None], lons[None, :]
    hav = np.sin((la-a)/2)**2 + math.cos(a)*np.cos(la)*np.sin((lo-b)/2)**2
    inside = 2*6371008.8*np.arcsin(np.minimum(1, np.sqrt(hav))) <= RADAR_SITE_RANGE_METERS
    cells = inside[:-1, :-1] & inside[1:, :-1] & inside[:-1, 1:] & inside[1:, 1:]
    return np.packbits(cells.ravel()).tobytes().hex()


def _radar_view_measured(snap, records):
    """Whether every 16-pixel cell of the snapshot's view, at its acquisition
    zoom, was measured in the newest frame: True, False, or None while a tile
    of it is not on disk yet. Native mosaic tiles answer from their
    radarMeasuredGrid; IEM site layers from their range discs (a layer covers
    a cell if any layer does); Region and RainViewer tiles count as measured
    (MRMS's domain edge is partialCoverage's job)."""
    from lib.radar_mosaic import grid_cells, GRID_CELLS
    import numpy as np
    frame = next((f for f in reversed(snap.frames) if f['ts'] == snap.ts_frame), None)
    if frame is None or snap.bounds is None or snap.zoom is None:
        return None
    if snap.source_id != 'iem-nexrad-n0b':
        return True
    zoom, bounds = snap.zoom, snap.bounds
    # Bounds came from these pixel edges; round away float noise so a view
    # edge on a tile edge does not reach a sliver into the next tile.
    left, top = (round(v, 6) for v in world_point(bounds['n'], bounds['w'], zoom))
    right, bottom = (round(v, 6) for v in world_point(bounds['s'], bounds['e'], zoom))
    world = 2**zoom * 256
    if right <= left:
        right += world
    side = 256 // GRID_CELLS
    for ty in range(max(0, math.floor(top/256)), min(2**zoom, math.ceil(bottom/256))):
        for tx in range(math.floor(left/256), math.ceil(right/256)):
            measured = np.zeros((GRID_CELLS, GRID_CELLS), bool)
            for site, stamp in _radar_frame_pairs(frame):
                if frame.get('mosaicKey'):
                    record = records.get(_radar_disk_key('iem-nexrad-n0b', site, stamp, zoom, tx, ty, (snap.tiles or {}).get('variant', 'native')))
                    if record is None or 'measuredGrid' not in (record[2] or {}):
                        return None
                    measured |= grid_cells(record[2]['measuredGrid'])
                else:
                    measured |= grid_cells(_radar_disc_grid(site, zoom, tx % 2**zoom, ty))
            x0, x1 = max(left, tx*256) - tx*256, min(right, (tx+1)*256) - tx*256
            y0, y1 = max(top, ty*256) - ty*256, min(bottom, (ty+1)*256) - ty*256
            crop = measured[int(y0//side):math.ceil(y1/side), int(x0//side):math.ceil(x1/side)]
            if crop.size and not crop.all():
                return False
    return True


def _radar_grid(ctx, zoom=None, margin=0):
    zoom = ctx['zoom'] if zoom is None else zoom
    tiles, _, _, _ = _radar_viewport(ctx['center']['lat'], ctx['center']['lon'], zoom,
                                   RADAR_VIEWPORT_W * 2**(zoom-ctx.get('camera_zoom', zoom)) + margin*512,
                                   RADAR_VIEWPORT_H * 2**(zoom-ctx.get('camera_zoom', zoom)) + margin*512)
    return tiles


def _radar_site_stale_sec(cadence, latency=None):
    """A site frame is stale once a scan we should have received is missing:
    the site's publication latency (scan time -> first seen in the IEM listing,
    see _radar_scan_latency) plus two scan intervals, whole minutes, between 8
    and 20 minutes. Modelling cadence alone (the old 2.5 x cadence) ignored
    latency, so the normal age just before the next scan arrives (latency +
    one interval, 840-870 s live at a 300 s cadence) crossed it and the panel
    flashed Stale between healthy scans. Unknown cadence assumes the nominal
    5-minute volume; unknown latency a conservative 5 minutes."""
    cadence = cadence or _RADAR_SOURCES['iem-nexrad-n0b']['cadence']
    latency = RADAR_SITE_LATENCY_DEFAULT_SEC if latency is None else max(0, latency)
    return int(min(RADAR_SITE_STALE_MAX_SEC, max(RADAR_SITE_STALE_MIN_SEC,
                                                math.ceil((latency + 2*cadence)/60)*60)))


def _radar_neighbour_limit_sec(cadence):
    """How old a neighbour's scan may be and still blend into a frame: about
    2.5 of the primary's scan intervals (one in flight, one and a half missed),
    whole minutes, 8 to 15 minutes; unknown cadence 15. Deliberately tighter
    than the display threshold: blending is a choice to mix times, staleness
    is a judgement that data went missing."""
    if not cadence:
        return RADAR_SITE_MAX_AGE_SEC
    return int(min(RADAR_SITE_MAX_AGE_SEC, max(RADAR_SITE_STALE_MIN_SEC, math.ceil(2.5*cadence/60)*60)))


def _radar_scan_latency(samples):
    """Robust high estimate of a site's publication latency: the 90th
    percentile of its recent first-seen samples, None before three exist."""
    values = sorted(v for v in samples if isinstance(v, (int, float)) and math.isfinite(v))
    if len(values) < RADAR_SITE_LATENCY_MIN_SAMPLES:
        return None
    return int(values[min(len(values)-1, math.ceil(0.9*len(values))-1)])


def _radar_contributors(frame):
    """The scans a frame's pixels came from: acquired layers, else its pairs."""
    return frame.get('acquiredSites') or frame.get('siteScans') or ()


def _radar_observed_range(frame):
    """[oldest, newest] contributing scan stamp (UTC epoch s), None if single-source."""
    stamps = [p['ts'] for p in _radar_contributors(frame) if type(p.get('ts')) in (int, float)]
    return [min(stamps), max(stamps)] if stamps else None


def _radar_freshness(snap, now):
    """Age/staleness of what the newest frame shows. A site frame is as old as
    its OLDEST contributing scan, not its anchor: neighbours can be minutes
    older than the primary that clocks the frame."""
    newest = next((f for f in reversed(snap.frames) if f['ts'] == snap.ts_frame), None)
    observed = _radar_observed_range(newest) if newest is not None else None
    oldest = observed[0] if observed else snap.ts_frame
    age = int(now-oldest) if oldest is not None else None
    stale_sec = (_radar_site_stale_sec(snap.scan_cadence_sec, snap.scan_latency_sec) if snap.source_mode == 'site'
                 else snap.stale_sec or RADAR_IEM_STALE_SEC)
    return dict(age=age, observed=observed, stale_sec=stale_sec,
                stale=age is not None and age >= stale_sec)


def _radar_expected_sites(ctx):
    """The in-view radars a site frame should draw from, decided before any
    contributor is chosen: every listed site that reports, plus every site
    whose listing failed. A freshness or time-window rule, a failed product or
    a failed listing can drop a site from the contributors, never from this
    set. Sites with fresh evidence that they are not reporting are not
    expected; their range simply shows as unmeasured in the tile grids."""
    return sorted(s['id'] for s in ctx.get('sites', ())
                  if s.get('reporting') or s.get('reason') == 'scan unavailable')


def _radar_partial_coverage(source, frame, ctx):
    """DATA_CONTRACT partialCoverage. MRMS: the view reaches past its domain.
    Site frames: an expected contributor is missing - its listing failed, it
    had no scan in the frame's window, it aged past the freshness limit, or
    its requested scan was not acquired. Geometry (view beyond every
    contributor's range, missing gates) is not this flag: native tiles carry
    their measured grid and the page and health read it."""
    bounds = ctx['bounds']
    if source == 'iem-mrms-lcref':
        return any(bounds[k] < _RADAR_IEM_DOMAIN[k] if k in ('w', 's') else
                   bounds[k] > _RADAR_IEM_DOMAIN[k] for k in ('w', 'e', 's', 'n'))
    if source != 'iem-nexrad-n0b' or frame is None:
        return False
    acquired = {(p['id'], p['ts']) for p in _radar_contributors(frame)}
    requested = {tuple(p) for p in frame.get('requestedPairs', ())}
    expected = set(frame.get('expectedSites', ())) | {site for site, _ in requested}
    return bool(requested - acquired) or bool(expected - {site for site, _ in acquired})


def _radar_site_pairs(ctx, ts, now=None):
    """Contributors of the frame anchored at ts (the primary's scan).

    Relative window: v1 keeps its 15-minute rule; v2 accepts -8 minutes
    through +60 s. Absolute limit: a neighbour must still be fresh (younger
    than the site stale threshold) as of the moment the frame stops being the
    newest - now for the newest frame, the primary's next scan for an older
    one - so a stale neighbour never blends into a current frame, and an
    older frame's set does not change as the clock runs on.
    """
    now = time.time() if now is None else now
    primary = ctx.get('site_id')
    timeline = ctx['site_scans'].get(primary, ()) if primary else ()
    later = next((t for t in timeline if t > ts), None)
    as_of = now if later is None else min(now, later)
    limit = _radar_neighbour_limit_sec(_radar_scan_cadence(timeline)['scan_cadence_sec'])
    pairs = []
    for site in ctx['sites']:
        stamps = ctx['site_scans'].get(site['id'], ()) if site['reporting'] else ()
        native = _radar_is_native(_radar_variant(ctx, 'iem-nexrad-n0b'))
        stamp = next((t for t in reversed(stamps) if t <= ts + (60 if native else 0)), None)
        if native and site['id'] == ctx.get('site_id'):
            stamp = ts if ts in stamps else None  # the primary clocks this frame
        if (stamp is not None and ts - stamp <= (480 if native else RADAR_SITE_MAX_AGE_SEC)
                and (site['id'] == primary or as_of - stamp < limit)):
            pairs.append((site['id'], stamp))
    return tuple(pairs)


def _radar_frame_pairs(frame):
    """Storage layers, distinct from the meteorological contributor metadata."""
    if frame.get('mosaicKey'):
        return [(frame['mosaicKey'], frame['ts'])]
    return [(p['id'], p['ts']) for p in frame.get('siteScans', ())] or [(None, frame['ts'])]


def _radar_frame(source, ts, ctx, pairs=None):
    frame = dict(ts=ts, stamp=datetime.fromtimestamp(ts, timezone.utc).strftime('%Y%m%d%H%M'),
                 complete=False, levels={}, siteScans=[dict(id=site, ts=scan) for site,scan in pairs or ()])
    if source == 'iem-nexrad-n0b' and pairs is not None:
        frame['expectedSites'] = _radar_expected_sites(ctx)
    return frame


@lru_cache(maxsize=512)
def _radar_stamp_text(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).strftime('%Y%m%d%H%M') if isinstance(stamp,(int,float)) else stamp


def _radar_present(ctx, source, site, stamp, zoom, x, y):
    inventory = ctx['inventory']  # missing ownership is a bug, never a disk probe
    return _radar_disk_key(source,site,stamp,zoom,x,y,_radar_variant(ctx,source)) in inventory


def _radar_tile_manifest(source, frames, ctx):
    """Exact disk availability; frame booleans use the viewport at each level.

    The newest mask is the union of available site tiles. Site scans retain their
    own stamps and are fetched/drawn in farthest-to-nearest order on the page.
    """
    levels = [z for z in (ctx['zoom']-1,ctx['zoom'],ctx['zoom']+1)
              if RADAR_MIN_ZOOM <= z <= _RADAR_SOURCES[source]['max_zoom']]
    px,py=world_point(ctx['center']['lat'],ctx['center']['lon'],ctx['zoom'])
    scale = 2**(ctx['zoom']-ctx.get('camera_zoom',ctx['zoom']))
    width, height = RADAR_VIEWPORT_W*scale, RADAR_VIEWPORT_H*scale
    x0,y0=math.floor((px-width/2)/256),max(0,math.floor((py-height/2)/256))
    grid=dict(x0=x0,y0=y0,w=math.ceil((px+width/2)/256)-x0,
              h=min(2**ctx['zoom'],math.ceil((py+height/2)/256))-y0)
    def inventory(frame,z,tiles):
        pairs=_radar_frame_pairs(frame)
        cache = ctx.get('manifest_cache')
        index = ctx.get('inventory')
        key = (source, tuple(pairs), z, tuple(tiles), _radar_variant(ctx,source))
        versions = tuple(index.groups.get((source,site,_radar_stamp_text(stamp),z),0) for site,stamp in pairs) if hasattr(index,'groups') else None
        if cache is not None and versions is not None:
            cached = cache.get(key)
            if cached is not None and cached[0] == versions:
                cache.move_to_end(key)
                return cached[1]
        availability=expected=complete=0
        for i,(x,y) in enumerate(tiles):
            required=[]
            for site,stamp in pairs:
                if site and not site.startswith('M'):
                    lat,lon,_=_NEXRAD_SITES[site]
                    n,w=world_inverse(x*256,y*256,z);south,e=world_inverse((x+1)*256,(y+1)*256,z)
                    if not circle_intersects_bounds(lat,lon,RADAR_SITE_RANGE_METERS,dict(n=n,s=south,w=w,e=e)):continue
                required.append(_radar_present(ctx,source,site,stamp,z,x,y))
            if required:expected|=1<<i
            if any(required):availability|=1<<i
            if required and all(required):complete|=1<<i
        result = availability,expected,complete
        if cache is not None and versions is not None:
            cache[key] = (versions, result)
            while len(cache) > 1024: cache.popitem(last=False)
        return result
    result=[]
    for frame in frames:
        present={}
        for z in levels:
            tiles=[(x,y) for x,y,_,_ in _radar_grid(ctx,z)]
            _,expected,complete=inventory(frame,z,tiles)
            present[str(z)]=bool(expected) and complete==expected
        result.append(dict(frame,levels=present,complete=present[str(ctx['zoom'])]))
    grid_tiles=[(x,y) for y in range(y0,y0+grid['h']) for x in range(x0,x0+grid['w'])]
    mask,expected,complete=inventory(result[-1],ctx['zoom'],grid_tiles) if result else (0,0,0)
    width=math.ceil(grid['w']*grid['h']/4)
    variant = _radar_variant(ctx,source)
    return dict(base='radar/t/',revision=_radar_render_revision(variant),
                remapRevision=_radar_variant_revision(variant),
                smooth=variant in (True, 'native-smooth'), variant=variant, tileSize=256,
                source=source,site=ctx.get('site_id') or '-',z=ctx['zoom'],levels=levels,grid=grid,
                camera=dict(ctx['center'], zoom=ctx.get('camera_zoom', ctx['zoom'])),
                geometry=[list(ctx.get('station', ctx['center'].values())), ctx['center'], ctx.get('camera_zoom',ctx['zoom']), ctx['zoom'], source, ctx.get('site_id')],
                intent=dict(ctx.get('intent', {})), publishedAt=time.time(),
                newest=dict(stamp=result[-1]['stamp'] if result else None,mask=f'{mask:0{width}x}',
                            expectedMask=f'{expected:0{width}x}',completeMask=f'{complete:0{width}x}'),frames=result)


_RadarResult = namedtuple('_RadarResult',
    'available reason frames ts_frame center zoom mpp bounds scalebar rings nexrad ts_fetch '
    'source_id provider attribution attribution_url cadence stale_sec legend partial_coverage '
    'max_zoom zoom_desired zoom_auto_level geo source_mode site_id sources scanning_slowly sites sites_considered tiles units scan_cadence_sec scan_mode scan_mode_source scan_latency_sec',
    defaults=('rainviewer', 'rainviewer', 'RainViewer', 'https://www.rainviewer.com/',
              RADAR_RAINVIEWER_FRAME_INTERVAL_SEC, RADAR_RAINVIEWER_STALE_SEC, _RADAR_DISPLAY_RAMP, False,
              7, None, 7, None, 'mosaic', None, (), False, (), 0, None, 'mi', None, None, None, None))
_RADAR_NONE = _RadarResult(False, 'no data yet', (), None, None, None, None, None,
                           None, None, None, None)

def _radar_tile_snapshot(snapshot):
    """One inventory observation owns both frame flags and the wire manifest.

    A tile batch can publish its final tile before _radar_fill_frame returns.
    Its original pending frame flag must not override measured completeness.
    Preserve historical identities and metadata without mutating prior snapshots.
    """
    complete = {f['ts']: f['complete'] for f in snapshot.tiles['frames']}
    return snapshot._replace(frames=tuple(dict(f, complete=complete[f['ts']]) for f in snapshot.frames))

def _radar_scan_cadence(stamps):
    """Infer only from the primary listing, independent of fetched tile history."""
    recent = stamps[-4:]
    gaps = [b-a for a,b in zip(recent, recent[1:])]
    cadence = median(gaps) if gaps else None
    mode = None
    if len(gaps) == 3:
        if cadence <= 390:
            mode = 'precipitation'
        elif 540 <= cadence <= 900:
            mode = 'clear-air'
    return dict(scan_cadence_sec=cadence, scan_mode=mode,
                scan_mode_source='cadence' if mode else None,
                scanning_slowly=cadence is not None and cadence > 900)



class _RadarInputExecutor(ThreadPoolExecutor):
    """Bound running + queued flights; saturation never blocks the coordinator.

    Keep admission until a worker consumes even a cancelled job, so repeatedly
    cancelling frames cannot accumulate dead work items in the executor queue.
    """
    def __init__(self, workers, name):
        super().__init__(max_workers=workers, thread_name_prefix=name)
        self._admission = _BoundedSemaphore(workers)

    def submit(self, fn, /, *args, **kwargs):
        result = Future()
        if not self._admission.acquire(blocking=False):
            result.set_exception(TimeoutError('radar input workers occupied'))
            return result

        def run():
            if not result.set_running_or_notify_cancel():
                self._admission.release()
                return
            try:
                value = fn(*args, **kwargs)
            except BaseException as error:
                self._admission.release()
                result.set_exception(error)
            else:
                # Publish completion after returning admission, so the next
                # frame cannot see a finished flight still occupying a slot.
                self._admission.release()
                result.set_result(value)

        try:
            work = super().submit(run)
        except RuntimeError as error:
            self._admission.release()
            result.set_exception(error)
            return result

        def retired(work):
            if work.cancelled():  # shutdown cancelled a wrapper before dequeue
                result.cancel()
                self._admission.release()
        work.add_done_callback(retired)
        return result


class _RadarScanUnpublished(ValueError):
    """IEM advertises a scan before its Level III product reaches S3."""


class RadarEngine:
    """Owner of radar work; callbacks read only the inputs it actually needs."""

    def __init__(self, output_path, *, runtime, config, forecast_updated,
                 refresh_alerts, logger):
        self.output_path = output_path
        self._runtime = runtime
        self._config = config
        self._forecast_updated = forecast_updated
        self._refresh_alerts = refresh_alerts
        self._logger = logger
        self._result = _RADAR_NONE
        self._result_stamp = None
        self._request_times = []  # ALL attempts, shared across sources and retries
        self._negative = {}
        self._newest = {}  # (source, site) -> (validated monotonic, knowledge)
        self._archive_positive = set()  # immutable successful archive URLs
        self._tiles = OrderedDict()
        self._native_groups = {}
        self._start_input_pools()
        self._n0h_health = HostHealth()  # isolate optional product failures
        self._level3_scans = OrderedDict()   # (site, stamp) -> decoded Scan, v2 only
        self._level3_flights = {}            # (site, stamp) -> shared completion and verdict
        self._level3_failed = {}             # (site, stamp) -> (retry at, error text)
        self._level3_listings = {}           # (site, hour prefix) -> (listed at, keys)
        self._native_requested = True
        self._level3_outage = None  # until, since, reason, kind and one recovery wakeup
        # The primary's current run of unpublished Level III scans: site, the
        # first unpublished scan (since) and warning rate limiting. It ends
        # only when that site publishes a product at or after `since`.
        self._level3_stall = None
        self._unpublished_until = 0  # monotonic readiness floor for watch publication lag
        self._qc_failures = 0
        self._qc_last_error = None
        self._qc_logged = set()  # (site, reason) already logged
        self._level3_site_errors = {}
        self._native_budget = NativeBudget(Path(output_path).with_name('radar_native_bytes.json'), clock=lambda: time.time())
        self._auto_switch = None
        # The mode Auto itself selected, from positive Site evidence, for the
        # drawn pixels. None when the fallback chain or failed/unknown Site
        # evidence put Region on screen: unattended watch holds only this.
        self._auto_chosen = None
        self._auto_evidence = {}
        self._target_source = None
        self._auto_due = None
        self._policy_ceiling = None
        self._disk_files = 0
        self._disk_bytes = 0
        self._idle_context = None
        self._warm_pending = False
        self._prefetched = {}  # (source, zoom, centre) -> completed scan set
        self._was_viewed = False
        self._view_pending = False
        self._view_session = None
        self._view_geometry = None
        self._geometry_since = 0.
        self._geo_state = None
        self._geo_idle = None
        # LOCK ORDER (outermost first). A thread may take a later lock while
        # holding an earlier one, never the reverse:
        #   _runtime.lock (_life_lock) -> HostHealth.lock (_health or
        #   _n0h_health) -> _lock -> RadarSession pool condition
        #   -> Attempt.lock.
        # Request admission (_request) and hedge claims hold a host-health
        # lock while the rate gate takes _lock, so code holding
        # _lock must never call into HostHealth (snapshot, record,
        # admit, probe_delay, ...). Native input workers outlive their frame
        # (a running future cannot be cancelled), so any inversion here is a
        # reachable deadlock, not a theoretical one. Snapshot health first,
        # then take _lock (see _log_pass).
        self._lock = _RLock()
        self._session = None
        self._provider = None
        self._emit_pending = None
        self._cooldowns = {}
        self._transport_failures = {}
        self._local_failure_streak = 0  # consecutive local-failure passes, for retry backoff
        self._source_since = time.monotonic()
        self._switch_reason = None
        self._transport_retries = 0
        self._stale_first_byte_retries = 0
        self._health = HostHealth()
        self._coverage_cache = OrderedDict()
        self._site_status = {}  # last listing evidence, independent of tile validity
        self._latency = {}      # site -> dict(checked, stamps, samples): publication latency evidence
        self._warnings = nws_warnings.Tracker()  # NWS storm-based warning polygons for the radar map
        self._warnings_seen = frozenset()        # ids already known to cover the station (strip refresh)
        self._warnings_session = None            # its own deadline-bounded transport (see _warnings_open)
        self._warnings_query_cache = None        # ((lat, lon), (reach, home, codes, url), home codes)
        self._discovery = DiscoverySchedule()
        self._discovery_event = None
        self._discovery_pending = False
        self._probe_reuse = {}
        self._metadata = {}
        self._zoom_stamp = None
        self._refresh = dict(state='idle', frameIndex=0, frameTotal=0)
        self._pending = {}
        self._acquisition_pending = False
        from lib.radar_cache import TileInventory
        self._disk_inventory = TileInventory(RADAR_DIR)  # caps sized to the disk it lives on
        self._cache_ready = _Event()      # set only by a successful boot scan
        self._cache_done = _Event()       # the current boot attempt finished (either way)
        self._cache_error = None          # last failed boot attempt; cleared on success
        self._cache_deferred = False      # a pass found the cache not ready
        self._health_key = None           # radar-health.json: last written summary state
        self._health_written = -math.inf
        self._health_warned = False
        self._boot_mono = time.monotonic()  # the 'starting' state is bounded from here
        self._attention = Attention()
        self._glances = GlanceHistory(os.path.join(os.path.dirname(output_path) or '.', 'radar_glances.json'))
        self._bytes_by_tier = Counter()
        self._sentinel = None
        self._quiet_at = None
        self._echo_pixels = None
        self._viewing_prev = False
        self._waking_since = None
        self._local_hour = None
        self._discovery_floor_until = None
        self._cache_thread = None
        self._manifest_cache = OrderedDict()
        self._bad_stamp = None
        self._phase_metrics = []
        self._request_metrics = []
        self._failure_logs = {}
        self._log_retry_at = None
        self._next_retry = None
        self._retry_reason = None
        self._begin_log_pass()
        self._metadata_at = {}
        self._restart = False

    _available = _snapshot_field('_result', 'available')
    _reason = _snapshot_field('_result', 'reason')
    _frames = _snapshot_field('_result', 'frames')
    _ts_frame = _snapshot_field('_result', 'ts_frame')
    _center = _snapshot_field('_result', 'center')
    _zoom = _snapshot_field('_result', 'zoom')
    _mpp = _snapshot_field('_result', 'mpp')
    _bounds = _snapshot_field('_result', 'bounds')
    _scalebar = _snapshot_field('_result', 'scalebar')
    _rings = _snapshot_field('_result', 'rings')
    _nexrad = _snapshot_field('_result', 'nexrad')
    _ts_fetch = _snapshot_field('_result', 'ts_fetch')

    def start(self):
        """Arm radar's original boot/intent/geography/warnings cadence.

        The emitter holds runtime.lock and has enabled the shared runtime.
        """
        self._start_input_pools()
        if RADAR_ENABLED:
            self._start_inventory()
            self._runtime.schedule(self._check, 60)
            self._zoom_stamp = self._preference_stamp()
            self._runtime.schedule(self._check_zoom, RADAR_INTENT_CHECK_SEC, interval=True)
            self._runtime.schedule(self._check_geo, RADAR_GEO_QUANTUM_SEC, interval=True)
            self._runtime.schedule(self._check_warnings, 45)
            self._runtime.schedule(self._check_warnings, nws_warnings.TICK_SEC, interval=True)
        else:
            self._logger.info('almanac_emit: radar disabled (WFP_RADAR=0): no acquisition, cache scan, geography or listings')

    def stop(self):
        """Release owned resources under the shared lifecycle lock.

        Runtime is already fenced; the emitter cancels the shared registry.
        Running requests retain their original completion/deadline cleanup.
        """
        with self._runtime.lock:
            self._input_pool.shutdown(wait=False, cancel_futures=True)
            self._hca_pool.shutdown(wait=False, cancel_futures=True)
            self._clear_retry()
            self._emit_pending = None
            self._discovery_event = None
            self._discovery_pending = False
            if self._session is not None and 'radar' not in self._runtime.inflight:
                self._session.close()
                self._session = None
            if self._warnings_session is not None and 'warnings' not in self._runtime.inflight:
                self._warnings_session.close()
                self._warnings_session = None

    def _spawn(self, key, worker):
        self._runtime.spawn(key, worker,
                            on_complete=self._worker_finished if key == 'radar' else None)

    def _worker_finished(self):
        # Called under runtime.lock, after its in-flight key has been removed.
        if self._restart:
            self._runtime.schedule(self._check_zoom, 0)
        elif self._acquisition_pending:
            self._runtime.schedule(self._check, 0)

    def before_emit(self):
        """Consume any pending publication handle at the normal emit tick."""
        with self._runtime.lock:
            if self._emit_pending is not None:
                self._emit_pending.cancel()
                if self._emit_pending in self._runtime.events:
                    self._runtime.events.remove(self._emit_pending)
                self._emit_pending = None

    def payload_snapshot(self, now, tz, style='24 hr'):
        """Snapshot wx.json's radar fields before the attention tick."""
        with self._lock:
            snap = self._result
            if snap.nexrad:
                nearest = dict(snap.nexrad)
                nearest.update(self._site_status.get(nearest['id'], {}))
                nearest['nextCheckTs'] = self._discovery.due
                snap = snap._replace(nexrad=nearest)
            refresh = self._refresh
        return dict(self._payload(snap, now, tz, refresh, style),
                    nativeBudget=self._native_budget.snapshot(),
                    nativeFallback=self._native_fallback(snap),
                    starting=self._starting(snap),
                    warnings=self._warnings.payload(now, self._warnings_fast(now)))

    def tick(self, payload, now, tz):
        """Apply attention after observation carry-forward, as on the emit tick."""
        if RADAR_ENABLED:
            self._attention_tick(payload, now, tz)
        else:
            payload['radar'] = dict(available=False, reason='radar off', enabled=False, starting=None, attention=None)

    def health_snapshot(self):
        """Full diagnostics snapshot used by radar-health.json."""
        return self._health_payload()

    def write_health(self, now):
        """Publish health at its existing cadence with failure-log suppression."""
        try:
            self._write_health(now)
        except Exception as error:  # noqa: BLE001
            if not self._health_warned:
                self._logger.warning(f'almanac_emit: radar health write failed - {error}')
                self._health_warned = True

    def _schedule_retry(self, key, callback, timeout, retry_reason="provider"):
        """ Arm the ONE pending retry a provider is allowed. Without this, every
        failure of a periodic poll starts its own retry chain and the chains
        multiply for as long as the network is down. """
        def _retry(dt):
            with self._runtime.lock:
                if self._runtime.retries.get(key) is not handle:
                    return  # a cancelled/replaced callback cannot consume its successor
                self._runtime.retries.pop(key, None)
                if key == 'radar':
                    self._clear_retry()
            callback(dt)

        with self._runtime.lock:
            if not self._runtime.running or self._runtime.retries.get(key) is not None:
                return
            handle = self._runtime.schedule(_retry, timeout)
            if handle is not None:
                self._runtime.retries[key] = handle
                if key == 'radar':
                    with self._lock:
                        self._log_retry_at = self._next_retry = time.time()+timeout
                        self._retry_reason = retry_reason
                        self._refresh = dict(self._refresh,
                            nextRetry=self._next_retry, retryReason=retry_reason)

    def _start_input_pools(self):
        # One frame uses at most four slots. A second frame has four spare
        # slots while a preceding frame's transports finish their deadlines.
        self._input_pool = _RadarInputExecutor(2*RADAR_SITE_MAX_COUNT, 'radar-input')
        self._hca_pool = _RadarInputExecutor(2*RADAR_SITE_MAX_COUNT, 'radar-hca')

    def _check(self, _dt=None):
        with self._runtime.lock:
            if 'radar' in self._runtime.inflight:
                self._acquisition_pending = True
                return
            self._acquisition_pending = False
            if self._discovery.due is not None and self._discovery.due <= time.time():
                self._check_discovery(_dt)
                return
            self._spawn('radar', lambda: self._acquire(intent_triggered=False))

    def _arm_discovery(self, min_delay=0, prompt=False):
        """One readiness wakeup, separate from repair/warming retries and emit."""
        with self._runtime.lock:
            old = self._discovery_event
            if old is not None:
                old.cancel()
                if old in self._runtime.events:
                    self._runtime.events.remove(old)
            self._discovery_event = None
            if not self._runtime.running:
                return
            now = time.time()
            plan = self._discovery
            plan.observe(self._result, now, RADAR_IEM_READY_LAG_SEC)
            due = plan.due if plan.due is not None else now + RADAR_RETRY_SEC
            delay = max(1, min_delay, due-now)
            source = self._result.source_id
            probe = self._probe_delay()
            if probe is not None:
                # Preserve recovery of a failed preferred source while on fallback.
                delay = max(min_delay, 1, probe)
            delay = max(delay, self._headroom_delay(source, 1), self._local_backoff(),
                        self._unpublished_until-time.monotonic())
            plan.due = now + delay
            # The attention floor holds the WAKEUP back, never the schedule's own
            # due: a tier rise re-arms at the natural due. On entering a quiet tier
            # the first quiet pass (listing, sentinel) runs promptly, then the floor.
            # DiscoverySchedule owns `due` and re-derives it from the newest frame,
            # so a caller cannot move it: a prompt wake is asked for here instead
            # (a tier rise, or the first quiet check on a fall into rest/dormant).
            wake = 1 if prompt else max(delay, self._attention_floor())
            self._discovery_floor_until = now + wake
            self._discovery_event = self._runtime.schedule(self._check_discovery, wake)

    def _check_discovery(self, _dt=None):
        with self._runtime.lock:
            # A repair/history retry can reach the same deadline first. It
            # becomes discovery and consumes the pending readiness handle too.
            old = self._discovery_event
            if old is not None:
                old.cancel()
                if old in self._runtime.events:
                    self._runtime.events.remove(old)
            self._discovery_event = None
            if 'radar' in self._runtime.inflight:
                self._discovery_pending = True
                self._arm_discovery(min_delay=5)
                return
            self._discovery_pending = False
            self._discovery.started(time.time())
            self._spawn('radar', lambda: self._acquire(intent_triggered=False, discovery=True))

    def _discovery_unchanged(self, source, newest, ctx, validated=None):
        snap = self._result
        if (source == 'iem-nexrad-n0b' and not self._primary_only(ctx)
                and any(f.get('primaryOnly') for f in snap.frames)):
            return  # attendance expands history even where only one radar covers the view
        if _radar_is_native(_radar_variant(ctx, source)) and any(self._hca_due(f) for f in snap.frames[-RADAR_LOOP_FRAMES:]):
            return
        target = min(ctx.get('frames_target') or (RADAR_LOOP_FRAMES if ctx.get('viewed') else 1), len(snap.frames))
        if source == 'iem-nexrad-n0b' and (snap.site_id != ctx.get('site_id') or
                not snap.frames or set(map(tuple, snap.frames[-1].get('requestedPairs', [(p['id'], p['ts']) for p in snap.frames[-1]['siteScans']])))
                != set(_radar_site_pairs(ctx, newest))):
            return  # a secondary layer may advance between primary volumes
        if (ctx.get('discovery') and snap.source_id == source and snap.ts_frame == newest
                and (snap.tiles or {}).get('variant', False) == _radar_variant(ctx, source)
                and self._result_stamp == ctx.get('preference_stamp')
                and snap.frames and snap.frames[-1]['complete']
                and not any(self._pending.get(k) for k in ('newest','four','eight'))
                and all(f['complete'] for f in snap.frames[-target:])):
            if validated is not None:
                # The complete current scan has already passed tile validation.
                # Refresh intent/prefetch knowledge even though no build runs.
                self._newest[(source, None)] = (time.monotonic(), validated)
            raise _RadarUnchanged()

    def _marker(self, name):
        return Path(self.output_path).with_name(name)

    def _marker_age(self, name, now, content=True):
        """ Seconds since a marker was written: from its numeric content when the
        writer stores an epoch, else from its mtime. None when absent/invalid. """
        try:
            path = self._marker(name)
            if content:
                with open(path) as f:
                    stamp = float(f.read(128).strip().split()[0])
            else:
                stamp = path.stat().st_mtime
            if not math.isfinite(stamp):
                return None
            age = now - stamp
            return age if age >= 0 else 0.0
        except (OSError, ValueError, IndexError):
            return None

    def _viewing_now(self, now):
        try:
            with open(self._marker('radar_viewing')) as f:
                record = json.load(f)
            return 0 <= now - float(record['last']) < RADAR_VIEWING_LAPSE_SEC
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def _forecast_age(self, now):
        """Seconds since the forecast's last successful update (lib/forecast.py
        stamps Met['UpdatedTs']); None while no forecast has ever succeeded."""
        updated = _num(self._forecast_updated())
        return None if updated is None else now - updated

    def _attention_signals(self, payload, now, tz):
        snap = self._result
        newest = snap.frames[-1] if snap.frames else None
        local = datetime.fromtimestamp(now, tz) if tz else datetime.fromtimestamp(now)
        self._local_hour = local.hour + local.minute / 60
        lightning_since = payload.get('lightningSinceSec')
        sentinel = self._sentinel or {}
        return Signals(now,
            local_hour=self._local_hour,
            viewing=self._viewing_now(now),
            viewed_age=self._marker_age('radar_viewed', now),
            touch_age=self._marker_age('presence', now),
            lan_viewer_age=self._marker_age('last_viewer', now, content=False),
            obs_age=payload.get('obsAgeSec') if payload.get('obsTs') is not None else None,
            rain_rate_mm=payload.get('rainRateMm'),
            rain_starting=payload.get('rainStatus') == 'Rain Starting',
            rain_wet=payload.get('rainStatus') in ('Rain Starting', 'Very Light Rain', 'Light Rain', 'Moderate Rain',
                'Heavy Rain', 'Very Heavy Rain', 'Extreme Rain', 'Snow Likely'),
            lightning_age=lightning_since if isinstance(lightning_since, (int, float)) else None,
            precip_pct=payload.get('fcPrecipPct'),
            conditions=payload.get('conditions'),
            forecast_age=self._forecast_age(now),
            echo=newest.get('echo') if newest else None,
            echo_age=(now - snap.ts_frame) if newest and snap.ts_frame else None,
            sentinel_echo=sentinel.get('echo'),
            sentinel_age=(now - sentinel['stamp']) if sentinel.get('stamp') else None,
            expected_glance=self._glances.expected(local))

    def _attention_tick(self, payload, now, tz):
        """ Runs with every emit (2 s). Decides the tier, records glances, wakes
        acquisition on a rise, and publishes radar.attention. Never raises. """
        try:
            attention = self._attention
            force = None
            age = self._marker_age('radar_attention_force', now, content=False)
            if age is not None and age < RADAR_ATTENTION_FORCE_TTL:
                try:
                    force = self._marker('radar_attention_force').read_text().strip()
                except OSError:
                    force = None
            attention.forced = force if force in ('dormant', 'rest', 'watch', 'warm', 'live') else None
            before_knobs = self._attention_knobs()
            signals = self._attention_signals(payload, now, tz)
            if signals.viewing and not self._viewing_prev:
                self._glances.record(datetime.fromtimestamp(now, tz) if tz else datetime.fromtimestamp(now))
            self._viewing_prev = signals.viewing
            before = attention.tier
            tier = attention.decide(signals)
            if tier != before:
                self._logger.info(f'almanac_emit: radar attention {before} -> {tier}; {attention.reason}')
            self._attention_changed(before_knobs, now)
            if self._waking_since is not None:
                fresh = self._current_complete(now)
                if fresh or now - self._waking_since > 90 or attention.tier not in ('warm', 'live'):
                    self._waking_since = None
            knobs = attention.knobs(self._local_hour)
            payload['radar']['attention'] = dict(tier=attention.tier, reason=attention.reason, since=attention.since,
                weather=attention.weather(now), mode=RADAR_ATTENTION_MODE, waking=self._waking_since is not None,
                unattended=attention.unattended,
                frames=knobs['frames'], tiles=knobs['tiles'],
                waiting=self._attention_active() and self._quiet_at is not None
                    and not self._result.available and self._result.reason == 'no data yet')
        except Exception as error:                                       # noqa: BLE001
            self._logger.warning(f'almanac_emit: radar attention tick failed - {error}')

    def _attention_knobs(self):
        return self._attention.knobs(self._local_hour)

    @staticmethod
    def _loop_target(ctx):
        """Frames this pass acquires for the loop: the attention tier's target
        when active (warm 4, live 8, watch 8 by day / 1 by night), one for a
        primary-only or newest-only site, otherwise 8 while the tab was viewed
        recently, 4 while staging a source change, else the newest alone.
        Published as radar.loopFrames: the page is never told to expect more."""
        if ctx.get('loop_target'):
            return ctx['loop_target']
        limit = ctx.get('frames_target')
        return limit if limit else (RADAR_LOOP_FRAMES if ctx.get('viewed') else 4 if ctx.get('staging_source') else 1)

    def _effective_tier(self):
        return self._attention.tier if self._attention_active() else 'live'

    def _site_policy(self, ctx=None):
        target = ctx.get('target_source') if ctx is not None else self._target_source
        if ctx is not None and target is not None:
            return target == 'iem-nexrad-n0b'
        return self._result.source_mode == 'site' or target == 'iem-nexrad-n0b'

    def _attention_active(self):
        return RADAR_ATTENTION_MODE == 'active'

    def _current_complete(self, now):
        snap = self._result
        return bool(snap.frames and snap.frames[-1]['complete'] and snap.ts_frame is not None
            and 0 <= now - snap.ts_frame and not _radar_freshness(snap, now)['stale']
            and self._result_stamp == self._preference_stamp()
            and (snap.source_mode != 'site' or (snap.tiles or {}).get('variant', False) == _radar_variant(dict(
                native=self._native_requested, attention=self._effective_tier(),
                native_ceiling=self._native_budget.snapshot()['ceilingState'],
                smooth=(snap.tiles or {}).get('smooth', False)), snap.source_id)))

    def _attention_changed(self, before, now, schedule=True):
        """Apply changed demand, including weather wakes and day/night targets.

        A quiet floor must not survive a promotion. A wake while a worker is
        running is retained by the existing single-flight pending mechanism.
        """
        after = self._attention_knobs()
        if all(after[k] == before[k] for k in after if k != 'prefetch'):
            return
        if (after['tier'] in ('warm', 'live') and RANK[after['tier']] > RANK[before['tier']]
                and not self._current_complete(now)):
            self._waking_since = now
        if not self._attention_active():
            return
        variant_changed = self._site_policy() and self._native_requested and ((before['tier'] in ('live', 'warm')) !=
                                                            (after['tier'] in ('live', 'warm')))
        more = variant_changed or after['frames'] > before['frames'] or after['tiles'] and not before['tiles']
        prompt = variant_changed or after['listing'] < before['listing'] or (not after['tiles'] and before['tiles'])
        if not after['tiles']:
            self._pending = {}
            self._clear_retry()
        self._arm_discovery(prompt=prompt)  # a rise wakes now; a fall runs its first quiet check now
        if more and schedule:
            self._runtime.schedule(lambda dt: self._check(), .1)

    def _attention_demand(self):
        """The 100 ms intent watcher can beat the 2 s emit tick. Promote real
        presence before its pass; a changed file stamp alone is not a person.
        The force override remains authoritative, even for a visible tab.
        """
        if not self._attention_active() or self._attention.forced:
            return
        now = time.time()
        ages = [a for a in (self._marker_age('radar_viewed', now),
                           self._marker_age('presence', now)) if a is not None]
        want = 'live' if self._viewing_now(now) else 'warm' if ages and min(ages) < WARM_HOLD_SEC else None
        if want and RANK[want] > RANK[self._attention.tier]:
            before = self._attention_knobs()
            self._attention._move(now, want, 'radar tab open' if want == 'live' else 'recent attention')
            self._attention_changed(before, now, schedule=False)

    def _attention_floor(self):
        """ Discovery may not fire sooner than the tier's listing interval. """
        if not self._attention_active():
            return 0
        interval = self._attention_knobs()['listing']
        return max(0, self._quiet_at + interval - time.time()) if self._quiet_at is not None else 0

    def _frame_echo(self, ctx, source, pairs, ts):
        """Positive precipitation evidence wins; clear needs complete coverage.
        Inventory counts exclude suppressed reflectivity and site clear air.
        """
        try:
            inventory = self._disk_inventory
            seen = False
            unknown = False
            pixels = tiles = 0
            for site, scan in (pairs or [(None, ts)]):
                for x, y, _, _ in _radar_site_tiles(ctx, site):
                    record = inventory.records.get(_radar_disk_key(source, site, scan, ctx['zoom'], x, y, _radar_variant(ctx, source)))
                    if record is None or 'weatherPixels' not in (record[2] or {}) or not record[2].get('remapped'):
                        unknown = True
                        continue
                    seen = True
                    tiles += 1
                    pixels += int(record[2]['weatherPixels'] or 0)
            self._echo_pixels = dict(pixels=pixels, tiles=tiles, unknown=unknown)
            if pixels >= RADAR_ECHO_MIN_SHARE * max(1, tiles) * 65536:
                return True
            return False if seen and not unknown else None
        except (KeyError, TypeError, ValueError):
            return None

    def _quiet_pass(self, ctx, knobs, site, site_ok):
        """ A resting or dormant tier: refresh the closest site's listing (so
        the picker's evidence stays honest), run the sentinel when due, fetch
        no frame tiles. The pass ends 'quiet'; discovery re-arms at the floor. """
        now = time.time()
        self._pending = {}
        self._warm_pending = False
        self._clear_retry()
        if self._attention_floor() > 0:
            self._pass['outcome'] = 'quiet'
            self._retained_refresh('idle')
            return
        self._quiet_at = now
        local_failures = self._health.failure_counts('iem-nexrad-n0b', 'iem-mrms-lcref')['local']
        try:
            if self._session is None or self._provider != 'iem':
                if self._session is not None:
                    self._session.close()
                self._session = RadarSession()
                self._provider = 'iem'
            self._session.begin_pass(ctx['deadline'])
            self._session.on_retry = lambda end, first_byte=False: self._transport_retry('iem-nexrad-n0b', end, first_byte=first_byte)
            if site_ok:
                check = dict(ctx)
                try:
                    self._site_listing(check, dict(site))
                except _RadarBudget:
                    pass
            sentinel_every = knobs['sentinel']
            due = sentinel_every and (self._sentinel is None or now - self._sentinel['at'] >= sentinel_every)
            if (due and self._health.failure_counts('iem-nexrad-n0b', 'iem-mrms-lcref')['local'] == local_failures
                    and _radar_iem_eligible(ctx['station'][0], ctx['station'][1])):
                self._sentinel_pass(ctx)
        except (_RadarBudget, CircuitOpen, TimeoutError, OSError, ValueError) as error:
            self._note_yield(error)
        finally:
            self._local_failure_streak = (self._local_failure_streak + 1
                if self._health.failure_counts('iem-nexrad-n0b', 'iem-mrms-lcref')['local'] > local_failures else 0)
            with self._lock:
                if self._pass['outcome'] not in ('failed',):
                    self._pass['outcome'] = 'quiet'
            self._retained_refresh('idle')

    def _sentinel_pass(self, ctx):
        """ Four MRMS tiles at zoom 7 around home (~425 km across at 47.6 N): does
        anything echo out there while the local gauge is dry? Feeds the 'echo'
        hold so rain approaching is noticed within an hour while resting. """
        from PIL import Image
        from lib.radar_palette import weather_pixels
        source = 'iem-mrms-lcref'
        deadline = min(ctx['deadline'], time.monotonic() + RADAR_SOURCE_DEADLINE_SEC)
        retry = self._session.on_retry
        self._session.on_retry = lambda end, first_byte=False: self._transport_retry(source, end, first_byte=first_byte)
        try:
            stamp, _, _ = self._iem_scan(dict(ctx, intent_triggered=False, deadline=deadline))
        except Exception:
            self._session.on_retry = retry
            raise
        px, py = world_point(ctx['station'][0], ctx['station'][1], RADAR_SENTINEL_ZOOM)
        tx, ty = int(px // 256), int(py // 256)
        xs = (tx - 1, tx) if px % 256 < 128 else (tx, tx + 1)
        ys = (ty - 1, ty) if py % 256 < 128 else (ty, ty + 1)
        pixels = complete = 0
        try:
            for x in xs:
                for y in ys:
                    self._checkpoint(ctx)
                    if y < 0 or y >= 2 ** RADAR_SENTINEL_ZOOM:
                        continue
                    url = RADAR_IEM_TILE_TEMPLATE.format(stamp=_radar_stamp_text(stamp), z=RADAR_SENTINEL_ZOOM, x=x % 2 ** RADAR_SENTINEL_ZOOM, y=y)
                    try:
                        raw = self._request(source, url, deadline)
                        self._validate_tile(raw, source)
                        with Image.open(io.BytesIO(raw)) as native, remap(native, source, source_palette(source)) as mapped:
                            pixels += weather_pixels(mapped)
                            complete += bool(mapped.info['remapped'])
                    except (OSError, ValueError, TimeoutError):
                        continue
        finally:
            self._session.on_retry = retry
            self._sentinel = dict(at=time.time(), stamp=stamp, pixels=pixels, complete=complete == 4,
                echo=True if pixels >= RADAR_ECHO_MIN_SHARE * 4 * 65536 else False if complete == 4 else None)
        self._logger.info(f'almanac_emit: radar sentinel stamp={_radar_stamp_text(stamp)} echoPixels={pixels}')

    def _starting(self, snap):
        """ The engine has no radar result yet because it is still booting: the
        tile cache is being validated (about 200 tiles/s on the Pi 4) or the first
        pass has not concluded. Distinct from "no radar here": the page keeps the
        Radar tab and says so instead of hiding it. None once a result or a
        conclusive failure exists, or after RADAR_STARTING_MAX_SEC. """
        if snap.available or snap.reason != 'no data yet':
            return None
        since = time.monotonic() - self._boot_mono
        if since > RADAR_STARTING_MAX_SEC:
            return None
        scanning = not self._cache_ready.is_set()
        return dict(phase='cache' if scanning else 'acquire', sinceSec=int(since),
                    cacheFiles=len(self._disk_inventory))

    def _health_payload(self):
        health = self._health.snapshot()
        health['enabled'] = RADAR_ENABLED
        health['native'] = self._native_budget.snapshot()
        health['nativeFallback'] = self._level3_fallback_health()
        newest = self._result.frames[-1] if self._result.frames else {}
        health['classification'] = self._n0h_health.snapshot()
        with self._lock:
            health['classification']['qcFailures'] = self._qc_failures
            health['classification']['lastQcError'] = self._qc_last_error
            site_failures = {site: dict(entry) for site, entry in self._level3_site_errors.items()}
        health['mosaic'] = dict(key=newest.get('mosaicKey'),
                                unfilteredSites=list(newest.get('unfilteredSites', ())),
                                siteFailures=site_failures)
        health['phases'] = list(self._phase_metrics)
        health['requests'] = list(self._request_metrics)
        health['pending'] = dict(self._pending)
        health['discovery'] = self._discovery.telemetry(time.time(), self._result.ts_frame)
        now = time.time()
        health['attention'] = self._attention_health(now, _station_tz(self._config() or {}))
        health['warnings'] = self._warnings.health(now)
        cache = self._disk_inventory
        failure = self._cache_error
        health['cache'] = dict(files=len(cache), bytes=cache.bytes, maxFiles=cache.MAX_FILES,
            maxBytes=cache.MAX_BYTES, ready=self._cache_ready.is_set(), startup=dict(cache.startup),
            initError=dict(error=failure['error'], attempts=failure['attempts'], ts=failure['ts'],
                           retryTs=failure['retryTs']) if failure is not None else None)
        return health

    def _health_summary(self, now):
        """The few fields a monitor reads first (DATA_CONTRACT: radar-health.json).
        Radar never changes the engine's own /health status."""
        if not RADAR_ENABLED:
            return dict(state='off', newestObservationTs=None, newestObservationAgeSec=None, lastSuccessTs=None,
                        source=None, fallbackReason=None, coverage='unknown', nextAttemptTs=None,
                        attentionTier=None, attentionReason=None, initError=None)
        snap = self._result
        failure = self._cache_error
        fresh = _radar_freshness(snap, now) if snap.available else None
        observed = fresh['observed'][0] if fresh and fresh['observed'] else snap.ts_frame if snap.available else None
        native = self._native_fallback(snap)
        retry = self._next_retry if self._next_retry is not None and self._next_retry > now else None
        attempts = [t for t in (retry, self._discovery.due,
                                failure['retryTs'] if failure else None) if t is not None]
        attention = self._attention
        state = ('error' if failure is not None and not self._cache_ready.is_set() else
                 'starting' if self._starting(snap) is not None else
                 ('stale' if fresh['stale'] else 'current') if snap.available else 'unavailable')
        return dict(state=state, newestObservationTs=observed,
            newestObservationAgeSec=max(0, int(now-observed)) if observed is not None else None,
            lastSuccessTs=self._health.last_success,
            source=snap.source_id if snap.available else None,
            fallbackReason=native['reason'] if native['active'] else None,
            coverage=self._health_coverage(snap),
            nextAttemptTs=min(attempts) if attempts else None,
            attentionTier=attention.tier, attentionReason=attention.reason,
            initError=failure['error'] if failure is not None and not self._cache_ready.is_set() else None)

    def _health_coverage(self, snap):
        """summary.coverage (DATA_CONTRACT): 'partial' when an expected
        contributor is missing or a cell of the acquired view was not measured
        in the newest frame; 'full' when neither, proven from the tiles' own
        grids; 'unknown' with no radar or while a view tile is not on disk."""
        if not snap.available:
            return 'unknown'
        if snap.partial_coverage:
            return 'partial'
        try:
            measured = _radar_view_measured(snap, self._disk_inventory.records)
        except (KeyError, TypeError, ValueError):
            measured = None
        return 'unknown' if measured is None else 'full' if measured else 'partial'

    def _write_health(self, now, force=False):
        """radar-health.json beside wx.json: the diagnostics wx.json used to
        carry on every poll. At most every RADAR_HEALTH_WRITE_SEC, and at once
        when the summary's state changes. Atomic (tmp + replace); no fsync, it
        is diagnostics on tmpfs."""
        summary = self._health_summary(now)
        key = tuple(summary[k] for k in ('state', 'source', 'coverage', 'fallbackReason', 'attentionTier', 'initError'))
        mono = time.monotonic()
        if (not force and key == self._health_key
                and mono - self._health_written < RADAR_HEALTH_WRITE_SEC):
            return False
        if RADAR_ENABLED:
            health = self._health_payload()
        else:
            health = dict(enabled=False, lastSuccessTs=None, breaker='closed', cache=None, attention=None)
        health.update(summary=summary, writtenTs=now)
        directory = os.path.dirname(self.output_path) or '.'
        target = os.path.join(directory, 'radar-health.json')
        tmp_path = f'{target}.tmp.{os.getpid()}'
        try:
            with open(tmp_path, 'w') as tmp_file:
                json.dump(_json_safe(health), tmp_file, allow_nan=False, separators=(',', ':'))
            os.replace(tmp_path, target)
        finally:
            try: os.unlink(tmp_path)
            except FileNotFoundError: pass
        self._health_key, self._health_written = key, mono
        self._health_warned = False
        return True

    def _attention_health(self, now, tz):
        local = datetime.fromtimestamp(now, tz or timezone.utc)
        with self._lock:
            byte_counts = dict(self._bytes_by_tier)
        return dict(self._attention.telemetry(now), mode=RADAR_ATTENTION_MODE,
            knobs=self._attention_knobs(), bytesByTier=byte_counts, wakeupTs=self._discovery_floor_until,
            byteAccounting='response bodies read; excludes headers and transport overhead',
            sentinel=self._sentinel, frameEcho=self._echo_pixels, waking=self._waking_since is not None,
            glances=self._glances.telemetry(now, local))

    def _probe_delay(self):
        # Recover the active/preferred chain. An expired breaker belonging to
        # an unused fallback must not turn healthy discovery into 1-second polls.
        snap = self._result
        sources = {snap.source_id}
        if snap.source_id == 'rainviewer':
            sources.add('iem-mrms-lcref')
        zoom = snap.zoom_desired if snap.zoom_desired is not None else snap.zoom_auto_level
        site_in_play = snap.source_mode == 'site' or self._target_source == 'iem-nexrad-n0b'
        if site_in_play or (zoom or 0) >= radar_auto.UP_ZOOM:
            sources.add('iem-nexrad-n0b')
        sources = {dependency for source in sources for dependency in self._transport_sources(source)}
        if not site_in_play:
            # Auto on Region at zoom >= 8 recovers Site through the closest-site
            # listing, which Region discovery sends and which clears IEM's
            # breaker. Only the site adapter ever contacts Level III, so its
            # breaker cannot clear from Region: counting it here would pin the
            # probe delay at 0 and wake discovery every second.
            sources.discard(RADAR_LEVEL3_TRANSPORT)
        probe = self._health.probe_delay(sources)
        now = time.monotonic()
        delays = [until-now for source, until in self._cooldowns.items()
                  if source in sources and until > now]
        if probe is not None:
            delays.append(probe)
        return min(delays) if delays else None

    def _read_intent(self):
        try:
            record = json.loads(Path(os.path.join(os.path.dirname(self.output_path), 'radar_intent')).read_text())
            if not isinstance(record, dict): return None  # legacy startup marker
            seq, zoom, center = (record[k] for k in ('seq','zoom','center'))
            if type(seq) is not int or not 0 <= seq <= 999999999999: return None
            if zoom != 'auto' and (type(zoom) is not int or not RADAR_MIN_ZOOM <= zoom <= 10): return None
            if center != 'station':
                if (not isinstance(center,dict) or type(center.get('lat')) not in (int,float)
                        or type(center.get('lon')) not in (int,float)
                        or not -85.05112878 <= center['lat'] <= 85.05112878 or not -180 <= center['lon'] <= 180): return None
            # A record written before Auto became the only source still names a
            # manual choice. It controls nothing, and must not reach a payload.
            return {k: v for k, v in record.items() if k not in RADAR_RETIRED_INTENT_FIELDS}
        except (OSError, ValueError, KeyError, TypeError): return None

    def _stamp_names(self):
        """Which marker files carry intent right now (one JSON parse)."""
        record = self._read_intent()
        return ('radar_intent', 'radar_smooth') if record is not None else ('radar_zoom', 'radar_center', 'radar_intent', 'radar_smooth')

    def _preference_stamp(self, names=None):
        # Supersede checkpoints run at every tile boundary. A pass hands them the file
        # set decided at its start so each checkpoint is one stat per file, not a JSON
        # parse: on the Pi the parse-per-checkpoint was ~90 SD-card reads, 1.7 s of a
        # 2.4 s zoom pass. Callers without a pass context still parse (watcher, publish).
        stamps = []
        for name in names or self._stamp_names():
            try:
                stat = os.stat(os.path.join(os.path.dirname(self.output_path), name))
                stamps.append((stat.st_ino, stat.st_mtime_ns, stat.st_size))
            except OSError:
                stamps.append(None)
        return tuple(stamps)

    def _check_zoom(self, _dt=None):
        # Geometry can publish while the single-flight transport worker drains.
        # Keep only the newest intent for network work; share all request budgets.
        self._consume_bad_tiles()
        stamp = self._preference_stamp()
        if self._native_budget.failed:
            self._native_budget.persist(wait=False)  # writer also owns trailing flush and retry
        if self._site_policy() and self._policy_ceiling is not None:
            if self._native_budget.snapshot()['ceilingState'] != self._policy_ceiling:
                self._restart = True
        if self._auto_due is not None and time.monotonic() >= self._auto_due:
            self._auto_due = None
            self._restart = True
        outage = self._level3_outage
        if outage is not None and outage['wake'] and time.monotonic() >= outage['until']:
            outage['wake'] = False
            self._restart = True
        viewed = self._is_viewed()
        # The demand hint lasts 15 minutes; the live session also catches a
        # return inside that window, without making every poll a new event.
        session = None
        try:
            record = json.loads(Path(self.output_path).with_name('radar_viewing').read_text())
            if 0 <= time.time()-record['last'] < RADAR_VIEW_POLL_GAP_SEC:
                session = record['since']
        except (OSError, ValueError, TypeError, KeyError):
            pass
        if viewed and (not self._was_viewed or
                       session is not None and session != self._view_session):
            self._view_pending = True
        self._was_viewed = viewed
        self._view_session = session
        if not viewed:
            self._view_pending = False
        if self._runtime.running and (stamp != self._zoom_stamp or self._view_pending or self._restart):
            if 'radar' not in self._runtime.inflight:
                view_started = self._view_pending
                self._view_pending = False
                self._zoom_stamp = stamp
                self._spawn('radar', lambda: self._acquire(intent_triggered=True, view_started=view_started))
        elif self._runtime.running and self._acquisition_pending and 'radar' not in self._runtime.inflight:
            self._check()
        elif self._runtime.running and viewed and self._warm_pending and 'radar' not in self._runtime.inflight:
            self._warm_pending = False
            self._spawn('radar', self._resume_warm)

    def _resume_warm(self):
        if self._idle_context is None:
            return
        self._begin_log_pass()
        source, warm = self._idle_context
        self._pass.update(source=source, site=warm.get('site_id'))
        warm = dict(warm, viewed=True, refresh=dict(state='idle'),
                    deadline=time.monotonic()+RADAR_BUILD_DEADLINE_SEC)
        try:
            if self._session is not None:
                self._session.begin_pass(warm['deadline'])
            self._prefetch(source, warm)
        except _RadarSuperseded:
            self._pass['outcome'] = 'superseded'  # the watcher owns the newer camera/source
        finally:
            self._log_pass(warm['deadline']-RADAR_BUILD_DEADLINE_SEC)

    def _check_geo(self, _dt=None):
        if not self._runtime.running or 'geo' in self._runtime.inflight:
            return
        from lib.radar_basemap import version
        config = self._config() or {}
        try:
            stat = Path(self.output_path).with_name('radar_activity').stat()
            activity_stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        except OSError:
            activity_stamp = None
        token = (_cfg(config, 'Station', 'Latitude'), _cfg(config, 'Station', 'Longitude'),
                 version(), activity_stamp, self._is_viewed())
        if token != self._geo_idle:
            self._spawn('geo', lambda: self._geo_work(token))

    def _geo_work(self, token=None):
        # No radar/session/result lock or transport context: home warming starts
        # with the engine, including before its first scheduled radar fetch.
        from lib.radar_basemap import WarmState, warm
        config = self._config() or {}
        station = (_num(_cfg(config, 'Station', 'Latitude')),
                   _num(_cfg(config, 'Station', 'Longitude')))
        lat, lon = station
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            self._geo_idle = token
            return
        activity = {}
        try:
            record = json.loads(Path(self.output_path).with_name('radar_activity').read_text())
            if isinstance(record, dict):
                activity = record
        except (OSError, ValueError):
            pass
        # Motion suppresses both queues, even if the last report is old. Only a
        # subsequent settled report (or removal of the marker) clears it.
        if activity.get('moving') and 0 <= time.time()-activity.get('at', 0) < 5:
            self._geo_idle = token
            return
        center, zoom = None, None
        theme = activity.get('theme', 'paper')
        if theme not in ('paper', 'night'):
            theme = 'paper'
        viewed = self._is_viewed()
        try:
            candidate, level = activity['center'], activity['zoom']
            if (viewed and 0 <= time.time()-activity['at'] < 5
                    and type(level) is int and 4 <= level <= 10
                    and isinstance(candidate, dict)
                    and type(candidate.get('lat')) in (int, float)
                    and type(candidate.get('lon')) in (int, float)
                    and -85.05112878 <= candidate['lat'] <= 85.05112878
                    and -180 <= candidate['lon'] <= 180):
                center, zoom = candidate, level
        except (KeyError, TypeError):
            pass
        if self._geo_state is None:
            self._geo_state = WarmState()
        try:
            made = warm(RADAR_DIR, station, center, zoom, _radar_zoom_for(lat), theme,
                        state=self._geo_state)
            if not made and not self._geo_state.home:
                self._geo_idle = token
            if made and center is None:
                time.sleep(RADAR_GEO_UNVIEWED_SLEEP_SEC)
        except (OSError, ValueError, ImportError) as error:
            self._logger.warning(f'almanac_emit: geography warming failed - {error}')

    def _inventory_valid(self,snap):
        grid=(snap.tiles or {}).get('grid')
        if not grid:return False
        for frame in snap.frames[-8:]:
            pairs=_radar_frame_pairs(frame)
            expected=False
            for y in range(grid['y0'],grid['y0']+grid['h']):
                for x in range(grid['x0'],grid['x0']+grid['w']):
                    for site,stamp in pairs:
                        if site and not site.startswith('M'):
                            lat,lon,_=_NEXRAD_SITES[site];n,w=world_inverse(x*256,y*256,snap.zoom);south,e=world_inverse((x+1)*256,(y+1)*256,snap.zoom)
                            if not circle_intersects_bounds(lat,lon,RADAR_SITE_RANGE_METERS,dict(n=n,s=south,w=w,e=e)):continue
                        expected=True
                        if _radar_disk_key(snap.source_id,site,stamp,snap.zoom,x,y,snap.tiles.get('variant',False)) not in self._disk_inventory:return False
            if not expected:return False
        return True

    def _checkpoint(self, ctx):
        if self._site_policy(ctx) and ctx.get('native_ceiling') is not None and ctx['native_ceiling'] != self._native_budget.snapshot()['ceilingState']:
            raise _RadarSuperseded('native daily budget changed')
        if 'preference_stamp' in ctx and ctx['preference_stamp'] != self._preference_stamp(ctx.get('stamp_names')):
            raise _RadarSuperseded('radar intent changed')
        if self._attention_active() and 'attention_knobs' in ctx:
            before, after = ctx['attention_knobs'], self._attention_knobs()
            if (any(before[k] != after[k] for k in ('frames', 'tiles')) or
                    (self._site_policy(ctx) and ctx.get('native') and
                     (before['tier'] in ('warm', 'live')) != (after['tier'] in ('warm', 'live')))):
                raise _RadarSuperseded('radar attention demand changed')
            # Optional demand never invalidates the visible loop. Yield only
            # optional work, and let the foreground finish with current knobs.
            ctx['attention_knobs'] = after
            if not after['prefetch'] and (ctx.get('prefetch') or ctx.get('deep_history')):
                raise _RadarBudget('radar optional work no longer requested')
        if self._discovery_pending and (ctx.get('prefetch') or ctx.get('request_reserve')):
            raise _RadarBudget('radar warming yielded to readiness discovery')
        if ctx.get('deep_history') and self._deep_view_delay(ctx) != 0:
            raise _RadarBudget('radar continuous view ended')
        if ctx.get('prefetch') and not self._is_viewed():
            raise _RadarBudget('radar tab no longer viewed')

    def _is_viewed(self):
        try:
            with open(os.path.join(os.path.dirname(self.output_path), 'radar_viewed')) as marker:
                age = time.time() - float(marker.read(128))
            return 0 <= age < RADAR_VIEW_TTL
        except (OSError, ValueError, UnicodeError):
            return False

    def _deep_view_delay(self, ctx):
        """Recent continuous viewing (panel, or an attended LAN page) AND residence at this geometry."""
        try:
            marker = Path(self.output_path).with_name('radar_viewing')
            viewing = json.loads(marker.read_text())
            since, last = viewing['since'], viewing['last']
            now = time.time()
            if not (0 <= now-last < RADAR_VIEW_POLL_GAP_SEC and since <= last):
                return None
        except (OSError, ValueError, TypeError, KeyError):
            return None
        geometry = (ctx['identity'], ctx['preference_stamp'])
        with self._lock:
            if geometry != self._view_geometry:
                return None
            duration = min(now-since, time.monotonic()-self._geometry_since)
        return max(0, RADAR_DEEP_VIEW_SEC-duration)

    def _emit_now(self):
        # Worker publications replace immutable snapshots. wx.json is built by
        # the normal two-second emit tick, not once per tile or cache hit.
        return

    def _publish_refresh(self, ctx, snapshot=None, **changes):
        self._checkpoint(ctx)
        loop = self._loop_target(ctx)
        if snapshot is not None:
            snapshot = _radar_tile_snapshot(snapshot)
            # Progress toward the loop the engine is building, never toward
            # retained slots it will not fetch (published frames: _radar_payload).
            changes['frameIndex'] = sum(f['complete'] for f in snapshot.frames[-loop:])
        refresh = dict(state='newest', frameIndex=0, frameTotal=1)
        refresh.update(ctx.get('refresh', {}))
        refresh.update({k:v for k,v in changes.items() if k in refresh})
        refresh.update(loopFrames=loop, frameTotal=min(refresh['frameTotal'], loop))
        phase = refresh['state']
        marks = ctx.setdefault('milestones', set())
        for name, reached in (('listings', refresh['frameTotal']>1), ('fourServerFrames', refresh['frameIndex']>=4), ('eightServerFrames', refresh['frameIndex']>=8)):
            if reached and name not in marks:
                marks.add(name)
                self._phase_metrics.append(dict(phase=name,at=time.monotonic(),cpuSec=time.process_time(),
                    intent=dict(ctx.get('intent',{})),requests=len(self._request_times),bytes=getattr(self,'_received_bytes',0)))
        if ctx.get('metric_phase') != phase:
            ctx['metric_phase'] = phase
            self._phase_metrics.append(dict(phase=phase, at=time.monotonic(), cpuSec=time.process_time(),
                intent=dict(ctx.get('intent', {})), requests=len(self._request_times),
                bytes=getattr(self, '_received_bytes', 0)))
            self._phase_metrics = self._phase_metrics[-128:]
        refresh.update(targetMode='site' if ctx.get('staging_source') == 'iem-nexrad-n0b' else 'mosaic' if ctx.get('staging_source') else None,
                       intent=dict(ctx.get('intent', {})), pending=dict(self._pending))
        with self._lock:
            refresh.pop('nextRetry', None)
            refresh.pop('retryReason', None)
            refresh.update(self._retry_fields())
            ctx['refresh'] = refresh
            if snapshot is not None:
                self._result = snapshot
            self._refresh = dict(refresh)
        self._emit_now()

    def _transport_sources(self, source, ctx=None):
        if source != 'iem-nexrad-n0b':
            return (source,)  # Region admission never evaluates native policy
        native = (_radar_is_native(_radar_variant(ctx, source)) if ctx is not None else
                  native_allowed(self._native_requested, self._effective_tier(),
                                 self._native_budget.snapshot()['ceilingState']))
        return (source, RADAR_LEVEL3_TRANSPORT) if native else (source,)

    def _headroom_delay(self, source, needed, ctx=None):
        with self._lock:
            now = time.monotonic()
            self._request_times = sorted(t for t in self._request_times if now-t < 60)
            count = len(self._request_times)
            missing = count + needed - RADAR_REQUESTS_PER_MIN
            window = self._request_times[min(missing, count)-1]+60-now if missing > 0 and count else 0
            cooldown = max(self._cooldowns.get(s, 0) for s in self._transport_sources(source, ctx))
            return max(0, window, cooldown-now)

    def _retry_fields(self):
        # Caller holds the publication lock; expiry can precede timer dispatch.
        if self._next_retry is not None and self._next_retry > time.time():
            return dict(nextRetry=self._next_retry, retryReason=self._retry_reason)
        return {}

    def _clear_retry(self):
        """Consume/cancel the timer and its publication as one lifecycle operation."""
        with self._runtime.lock:
            old = self._runtime.retries.pop('radar', None)
            if old is not None:
                old.cancel()
                if old in self._runtime.events:
                    self._runtime.events.remove(old)
            with self._lock:
                self._log_retry_at = self._next_retry = None
                self._retry_reason = None
                self._refresh = {k: v for k, v in self._refresh.items()
                                       if k not in ('nextRetry', 'retryReason')}

    def _note_yield(self, error):
        """ The pass log's error= names the yield that ended a pass: which
        _RadarBudget/TimeoutError, with its text. _radar_budget_retry adds the
        call site and the headroom asked for. """
        with self._lock:
            if not self._pass.get('error'):
                self._pass['error'] = f'deferred: {type(error).__name__}: {error}'

    def _budget_retry(self, source, needed, min_delay=0, reason='budget'):
        if self._pass["outcome"] != "failed":
            self._pass["outcome"] = "deferred"
            # Name the yield: a deferred pass with error=None hid a read-only
            # cache for an evening and a 2 s one-request loop for an hour.
            caller = sys._getframe(1)
            where = f'{reason} needed={needed} at={caller.f_code.co_name}:{caller.f_lineno}'
            error = self._pass.get("error")
            self._pass["error"] = f'{error} ({where})' if error and error.startswith('deferred:') else error or f'deferred: {where}'
        delay = max(min_delay, self._headroom_delay(source, needed), self._local_backoff())
        # Build/deadline yields with free transport resume on the next watcher.
        delay = delay if delay > 0 else 2
        with self._runtime.lock:
            self._clear_retry()
            self._schedule_retry('radar', self._check, delay, retry_reason=reason)
            with self._lock:
                self._refresh = dict(self._refresh, pending=dict(self._pending))

    def _local_backoff(self):
        """Retry floor while consecutive passes fail locally (dead route or
        exhausted client resources): 2, 4, 8 ... RADAR_LOCAL_RETRY_MAX_SEC. Local failures never
        open a host breaker, so nothing else slows the loop during an outage.
        It is a floor under every scheduled radar retry, not the failed pass's
        own delay: a partial-frame pass calls _radar_failed_pass and then
        re-arms its own budget retry, which would otherwise land 2 s later
        right over the backoff. Zero when the last pass was not a local failure."""
        streak = self._local_failure_streak
        return min(RADAR_LOCAL_RETRY_MAX_SEC, 2 ** min(streak, 6)) if streak else 0

    def _note_source(self, source, ctx):
        old = self._result
        self._note_auto_choice(ctx, source)
        if (old.frames and
                old.source_mode != ('site' if source == 'iem-nexrad-n0b' else 'mosaic')):
            self._auto_switch = (time.monotonic(), ctx['camera_zoom'])
        if old.available and old.source_id != source:
            self._source_since = time.monotonic()
            self._switch_reason = ctx.get('switch_reason', 'initial source selection')
            self._logger.info(f'almanac_emit: radar source SWITCH {old.source_id} -> {source}; '
                        f'reason={self._switch_reason}')

    def _begin_log_pass(self):
        # The radar lane is single-flight; tile/listing workers share its lock.
        # These counters never depend on the size or retention of /health history.
        self._pass = dict(counts=Counter(), source=None, site=None,
            outcome='idle', error=None, failures=set(), recovered=set(), validated=set(),
            hedges=self._health.hedges)

    @staticmethod
    def _log_text(value, limit=240):
        # Bound the encoded field, including quotes, controls and Unicode escapes.
        text = json.dumps(str(value), ensure_ascii=True)[1:-1]
        return text if len(text) <= limit else text[:limit-3]+'...'

    def _log_failure(self, source, error, scope='pass'):
        key = (source, type(error).__name__, str(error) or type(error).__name__)
        now = time.monotonic()
        with self._lock:
            self._pass['failures'].add(key)
            self._pass['error'] = key[1]+': '+key[2]
            prior = self._failure_logs.get(key)
            if prior is not None:
                prior['scopes'].add(scope)
            if prior is not None and now-prior['at'] < RADAR_FAILURE_LOG_SEC:
                prior['suppressed'] += 1
                return
            suppressed = prior['suppressed'] if prior else 0
            self._failure_logs[key] = dict(at=now, suppressed=0,
                scopes=prior['scopes'] if prior else {scope})
            self._logger.warning(f'almanac_emit: radar {source} failed: '
                f'{self._log_text(key[1]+": "+key[2])}; suppressed={suppressed}')

    def _count_request(self, outcome, error=None):
        with self._lock:
            self._pass['counts'][outcome] += 1
            if error is not None:
                self._pass['error'] = type(error).__name__+': '+str(error)

    def _log_pass(self, started):
        # Host-health state is read BEFORE _radar_lock, never inside it: the
        # documented order is health -> _radar_lock (see __init__). A late
        # native input worker can be inside request admission (health held,
        # waiting for the rate gate's _radar_lock) while this pass logs.
        with self._health.lock:
            states = {self._health.state(s) for s in self._health.hosts.values()}
            breaker = 'open' if 'open' in states else 'half' if 'half' in states else 'closed'
            hedges_now = self._health.hedges
        with self._lock:
            p = self._pass
            # Only verified source success ends an episode. Cached/budget-only
            # passes and successful fallback requests cannot recover its primary.
            failed_sources = {key[0] for key in p['failures']}
            for source in sorted({source for source, _ in p['recovered']} - failed_sources):
                scopes = {scope for src, scope in p['recovered'] if src == source}
                keys = [key for key, prior in self._failure_logs.items()
                        if key[0] == source and ('pass' in scopes or prior['scopes'] <= scopes)]
                if keys:
                    suppressed = sum(self._failure_logs.pop(key)['suppressed'] for key in keys)
                    self._logger.info(f'almanac_emit: radar {source} recovered; suppressed={suppressed}')
            # A changed error starts a new episode; retire obsolete signatures,
            # reporting their pending repeats once instead of retaining history.
            for key in list(self._failure_logs):
                if key[0] in failed_sources and key not in p['failures']:
                    prior = self._failure_logs.pop(key)
                    if prior['suppressed']:
                        self._logger.warning(f'almanac_emit: radar {key[0]} failure changed; '
                            f'previous={self._log_text(key[1]+": "+key[2])}; '
                            f'suppressed={prior["suppressed"]}')
            counts = p['counts']
            failed = {k: v for k, v in sorted(counts.items()) if k != 'ok'}
            retry_at = self._log_retry_at if 'radar' in self._runtime.retries else None
            retry_times = [t for t in (retry_at, self._discovery.due) if t is not None]
            retry = max(0, min(retry_times)-time.time()) if retry_times else None
            # Deliberately do not build/serialize the rolling health payload here.
            hedges = hedges_now-p['hedges']
            source = p['source'] or self._result.source_id
            site = p['site'] if p['source'] is not None else self._result.site_id
            outcome = p['outcome']
            if outcome == 'idle' and counts:
                outcome = 'partial' if failed else 'ok'
            self._logger.info('almanac_emit: radar pass '
                f'outcome={outcome} source={self._log_text(source, 40)} '
                f'site={self._log_text(site, 12)} elapsed={time.monotonic()-started:.3f}s '
                f'requests={sum(counts.values())} ok={counts["ok"]} failed={sum(failed.values())} '
                f'classes={json.dumps(failed, separators=(",", ":"))} hedges={hedges} '
                f'breaker={breaker} nextRetrySec={round(retry, 3) if retry is not None else None} '
                f'error={self._log_text(p["error"]) if p["error"] else "None"}')

    def _retained_refresh(self, state):
        snap = self._result
        with self._lock:
            # A pass that ends without a new target keeps the last one in force.
            loop = self._refresh.get('loopFrames')
            window = snap.frames[-loop:] if loop else snap.frames
            self._refresh = dict(state=state, frameIndex=sum(f['complete'] for f in window),
                                      frameTotal=len(window), intent=dict(self._refresh.get('intent', {})),
                                      pending=dict(self._pending), **self._retry_fields(),
                                      **({'loopFrames': loop} if loop else {}))
        self._emit_now()

    def _request_gate(self, source, deadline, reserve=0):
        with self._lock:
            now = time.monotonic()
            self._request_times = [t for t in self._request_times if now - t < 60]
            if now >= deadline:
                raise TimeoutError('radar acquisition deadline')
            if (now < self._cooldowns.get(source, 0) or
                    len(self._request_times) >= RADAR_REQUESTS_PER_MIN - reserve):
                raise _RadarBudget('radar request budget/cooldown')
            self._request_times.append(now)
        return now

    def _transport_retry(self, source, deadline, reserve=0, first_byte=False):
        self._request_gate(source, deadline, reserve)
        with self._lock:
            self._transport_retries += 1
            self._stale_first_byte_retries += int(first_byte)
            count = self._transport_retries
            first_byte_count = self._stale_first_byte_retries
        with self._health.lock:
            self._health.retries += 1
        self._logger.info(f'almanac_emit: radar {source} stale connection retry; transport_retries={count}; '
                    f'stale_first_byte_retries={first_byte_count}')

    def _request(self, source, url, deadline, method='GET', metadata=False, reserve=0, attempt=None, retry=False, validate=None, health=None):
        """Validated transport; every attempt uses one monotonic rate/cooldown gate."""
        health = self._health if health is None else health
        import urllib.request
        import urllib.error
        from email.utils import parsedate_to_datetime
        if source in RADAR_LEVEL3_TRANSPORTS and self._native_budget.snapshot()['ceilingState'] == 'paused':
            raise _RadarSuperseded('native daily data limit')
        if metadata:
            with self._lock:
                probed = self._probe_reuse.pop(url, None)
            if probed is not None:
                if validate is not None:
                    validate(probed)
                return probed  # this pass already paid for the half-open probe
        # Host and rate admission are atomic; an open host costs no budget and
        # a denied rate slot must not strand a half-open probe.
        with health.lock:
            if attempt is not None:
                attempt.check()
            probe = health.admit(source, url, metadata)
            try:
                self._request_gate(source, deadline, reserve)
            except Exception:
                if probe:
                    health._host(source, url)['probe'] = False
                raise
        if retry:
            with health.lock:
                if attempt is not None and attempt.hedged:
                    attempt.issued = True
                    health.issue_hedge(stall=attempt.stall_hedge)
                else:
                    health.retries += 1
        headers = {'User-Agent': 'WeatherAlmanac'}
        cached = self._metadata.get(url)
        if metadata:
            headers['Cache-Control'] = 'no-cache'
            if cached:
                headers.update(cached[1])
        request_started, request_cpu = time.monotonic(), time.thread_time()
        request_tier = self._attention.tier
        outcome, byte_count = 'success', 0
        count_outcome = 'ok'
        req = urllib.request.Request(url, headers=headers, method=method)
        def retry_failure(error):
            health.record(source, url, False, error)
            self._count_request(failure_class(error), error)
        req.radar_retry_failure = retry_failure
        req.radar_retry_check = lambda: health.admit(source, url)
        if attempt is not None or probe:
            req.radar_attempt = attempt or Attempt(fresh=True)
        try:
            timeout = RADAR_HTTP_TIMEOUT_SEC if metadata or method == "HEAD" else RADAR_TILE_TIMEOUT_SEC
            with self._session.open(req, timeout=min(timeout, deadline - time.monotonic())) as response:
                if getattr(response, 'status', 200) != 200:
                    raise ValueError('unexpected radar HTTP status')
                # Retain completed chunks on a late read failure. Counts are
                # response-body bytes, deliberately not an ISP traffic meter.
                chunks = []
                while method == 'GET' and byte_count <= 2 * 1024 * 1024:
                    try:
                        chunk = response.read(min(65536, 2 * 1024 * 1024 + 1 - byte_count))
                    except Exception as error:
                        partial = getattr(error, 'partial', b'')
                        if isinstance(partial, bytes):
                            byte_count += len(partial)
                        raise
                    if not chunk:
                        break
                    chunks.append(chunk)
                    byte_count += len(chunk)
                    if time.monotonic() >= deadline:
                        raise TimeoutError('radar response exceeded deadline')
                raw = b''.join(chunks)
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError('oversized radar response')
                if time.monotonic() >= deadline:
                    raise TimeoutError('radar response exceeded deadline')
                if validate is not None:
                    validate(raw)
                if metadata:
                    validators = {}
                    response_headers = getattr(response, 'headers', {})
                    for name, request_name in [('ETag', 'If-None-Match'), ('Last-Modified', 'If-Modified-Since')]:
                        if response_headers.get(name):
                            validators[request_name] = response_headers[name]
                    self._metadata[url] = (raw, validators)
                    self._metadata_at[url] = time.monotonic()
                    while len(self._metadata) > 128:
                        victim = next(iter(self._metadata))
                        self._metadata.pop(victim, None); self._metadata_at.pop(victim, None)
                if attempt is not None:
                    attempt.check()
                    self._validate_tile(raw, source)
                health.record(source, url, True, probe=probe)
                return raw
        except urllib.error.HTTPError as error:
            outcome = 'http-'+str(error.code)
            count_outcome = 'http'
            if error.code == 304 and metadata and cached:
                if validate is not None:
                    validate(cached[0])
                count_outcome = 'ok'
                health.record(source, url, True, probe=probe)
                return cached[0]
            with self._lock:
                self._pass['error'] = type(error).__name__+': '+str(error)
            health.record(source, url, error.code == 404, error, probe=probe)
            if error.code == 429:
                retry = error.headers.get('Retry-After', '') if error.headers else ''
                try:
                    delay = float(retry)
                except ValueError:
                    try:
                        delay = parsedate_to_datetime(retry).timestamp() - time.time()
                    except (ValueError, TypeError, OverflowError):
                        delay = RADAR_RETRY_SEC
                if not math.isfinite(delay):
                    delay = RADAR_RETRY_SEC
                if source in RADAR_LEVEL3_TRANSPORTS:
                    delay = min(delay, RADAR_LEVEL3_COOLDOWN_MAX_SEC)
                self._cooldowns[source] = time.monotonic() + max(1, delay)
                if source == RADAR_LEVEL3_TRANSPORT:
                    self._level3_fallback(error)
                raise _RadarBudget('radar provider rate limited') from error
            raise
        except Exception as error:
            outcome = failure_class(error)
            if attempt is not None and attempt.cancelled.is_set():
                if attempt.discarded:
                    error = AttemptCancelled("discarded radar attempt")
                elif not attempt.waiting_response:
                    error = LocalTransportError("radar connection setup exceeded tile deadline")
                else:
                    error = TimeoutError("radar response exceeded tile deadline")
            if not getattr(req, "radar_gate_failed", False):
                health.record(source, url, False, error, probe=probe)
            count_outcome = 'cancelled' if isinstance(error, AttemptCancelled) else failure_class(error)
            with self._lock:
                self._pass['error'] = type(error).__name__+': '+str(error)
            # Validation errors occur after open(); discard that untrusted pool.
            # Transport errors already discarded their own lease only.
            if isinstance(error, ValueError):
                self._session.discard(url)
            raise
        finally:
            with self._lock:
                if not getattr(req, 'radar_gate_failed', False):
                    self._count_request(count_outcome)
                    if count_outcome == 'ok':
                        self._pass['validated'].add(source)
                self._received_bytes = getattr(self, '_received_bytes', 0)+byte_count
                self._bytes_by_tier[request_tier] += byte_count
                self._request_metrics.append(dict(at=request_started,elapsedSec=time.monotonic()-request_started,
                    cpuSec=time.thread_time()-request_cpu,source=source,method=method,bytes=byte_count,tier=request_tier,
                    failureClass=outcome,queueWaitSec=getattr(req,'radar_queue_wait',0)))
                self._request_metrics=self._request_metrics[-128:]
            if source in RADAR_LEVEL3_TRANSPORTS:
                self._native_budget.add(byte_count)

    @staticmethod
    def _validate_tile(raw, source):
        from PIL import Image
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("radar response is not PNG")
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "PNG" or image.size != (256, 256):
                raise ValueError("unexpected radar tile size/format")
            image.load()
            if source.startswith("iem-"):
                with image.convert("RGBA") as rgba:
                    if rgba.getextrema() == ((255,255),(0,0),(0,0),(255,255)):
                        raise ValueError("IEM placeholder tile")

    def _tile_batch(self, source, stamp, ctx, deadline, url, site):
        """Six newest / four background requests; retain successes on cancellation."""
        from PIL import Image
        workers = ctx.get('tile_workers', RADAR_TILE_WORKERS)
        variant = _radar_variant(ctx, source)
        foreground_tiles = ({(x, y) for x, y, _, _ in _radar_grid(ctx)}
                            if _radar_is_native(variant) and not ctx.get('prefetch') else set())
        interactive = workers == RADAR_NEWEST_TILE_WORKERS and not ctx.get('prefetch')
        hedge_budget = ctx.setdefault('hedge_budget', dict(count=0, limit=len(ctx['tiles'])//2))
        def claim_hedge(tile_url):
            # Same lock order as wire admission: health -> rate gate -> pool.
            with self._health.lock, self._lock:
                reserve = ctx.get('request_reserve', 0)
                if (not self._health.hedge_allowed() or
                        hedge_budget['count'] >= hedge_budget['limit'] or
                        self._headroom_delay(source, reserve+1)):
                    return False
                lease = self._session.reserve_hedge(tile_url)
                if lease is None:
                    return False
                hedge_budget['count'] += 1
                return lease
        def discarded(count):
            self._health.discard_hedges(count)

        def fetch(tile):
            tx, ty, _, _ = tile
            target = _radar_tile_path(source, site, stamp, ctx['zoom'], tx, ty, variant)
            disk_key = _radar_disk_key(source,site,stamp,ctx['zoom'],tx,ty,variant)
            if disk_key in self._disk_inventory:
                return tile, None  # bytes/metadata already validated; page decodes
            if _radar_is_native(variant):
                from lib.radar_mosaic import render_mosaic
                scans = ctx['mosaic_scans']
                self._checkpoint(ctx)
                drawn, visible = render_mosaic(scans, ctx['zoom'], tx, ty, source_palette(source),
                    RADAR_SITE_RANGE_METERS, filtered=ctx.get('mosaic_filtered'), deadline=deadline,
                    smooth=variant == 'native-smooth',
                    cache_geometry=(not ctx.get('prefetch') and
                        ctx['zoom'] == ctx.get('camera_zoom', ctx['zoom']) and
                        (tx, ty) in foreground_tiles))
                with drawn:
                    colours = sum(1 for _, index in drawn.getcolors(256) if index)
                    # Gates are measured values, never matched colours: nothing is unmatched or ambiguous.
                    return tile, store(target, disk_key, drawn, visible, dict(remapped=True, unmatchedColors=0,
                        opaqueColors=colours, unmatchedPixels=0, opaquePixels=visible, ambiguousPixels=0,
                        revision=_radar_variant_revision(variant)), uncovered=drawn.info['radarUncoveredPixels'],
                        grid=drawn.info['radarMeasuredGrid'])
            # Native IEM bytes have no dependency on our remapping revision.
            # RainViewer's server-side colour scheme/options DO affect raw bytes.
            native = (RADAR_RAINVIEWER_COLOR, RADAR_RAINVIEWER_TILE_OPTS) if source == 'rainviewer' else None
            key = (source, site, native, stamp, ctx['zoom'], tx, ty)
            with self._lock:
                raw = self._tiles.get(key)
                if raw is not None:
                    self._tiles.move_to_end(key)
            self._checkpoint(ctx)
            reserve = ctx.get('request_reserve', RADAR_HISTORY_RESERVE if ctx.get('prefetch') else 0)
            options = dict(reserve=reserve) if reserve else {}
            if raw is None:
                switch_work = bool(ctx.get('intent', {}).get('camera')) and not ctx.get('prefetch') and not ctx.get('deep_history')
                tile_deadline = min(deadline, time.monotonic()+(2 if switch_work else 2*RADAR_TILE_TIMEOUT_SEC))
                def request(control, retry):
                    # Count hedges at wire admission, independently from retries.
                    attempt_end = min(tile_deadline, time.monotonic()+(.75 if switch_work and not retry else RADAR_TILE_TIMEOUT_SEC))
                    return self._request(source, url(tx, ty), attempt_end,
                        attempt=control, retry=retry, **options)
                raw = tile_race(request, tile_deadline,
                                (.5 if switch_work else RADAR_HEDGE_SEC) if interactive else None,
                                lambda: claim_hedge(url(tx, ty)), discarded)
            self._validate_tile(raw, source)
            with self._lock:
                self._tiles[key] = raw
                self._native_groups.setdefault((source,site,stamp),set()).add(key)
                self._tiles.move_to_end(key)
                while len(self._tiles) > RADAR_TILE_CACHE_SIZE:
                    victim, _ = self._tiles.popitem(last=False)
                    group = (victim[0],victim[1],victim[3])
                    members = self._native_groups.get(group,set())
                    members.discard(victim)
                    if not members: self._native_groups.pop(group,None)
            with Image.open(io.BytesIO(raw)) as native_tile:
                with (smooth_remap if variant else remap)(native_tile, source, source_palette(source)) as mapped:
                    metadata = {k:mapped.info[k] for k in ('remapped','unmatchedColors','opaqueColors',
                        'unmatchedPixels','opaquePixels','ambiguousPixels')}
                    metadata['revision'] = _radar_variant_revision(variant)
                    with mapped.getchannel('A') as alpha:
                        visible = mapped.width*mapped.height-alpha.histogram()[0]
                    return tile, store(target, disk_key, mapped, visible, metadata)

        def store(target, disk_key, mapped, visible, metadata, uncovered=None, grid=None):
            from PIL.PngImagePlugin import PngInfo
            from lib.radar_palette import weather_pixels
            info = PngInfo(); info.add_text('radarRemap',json.dumps(metadata,separators=(',',':')))
            info.add_text('radarVisiblePixels',str(visible))
            if uncovered is not None:
                # Native mosaics only (DATA_CONTRACT: pixels with no valid
                # measurement, and which 16-pixel cells were wholly measured).
                _radar_check_grid(grid, uncovered)
                info.add_text('radarUncoveredPixels',str(uncovered))
                info.add_text('radarMeasuredGrid',grid)
                metadata = dict(metadata, uncoveredPixels=uncovered, measuredGrid=grid)
            encoded = io.BytesIO()
            mapped.save(encoded, format='PNG', pnginfo=info)
            rendered = encoded.getvalue()
            echo_pixels = weather_pixels(mapped)
            # All raster work is on radar-tile threads. One write, no
            # read-back, PNG reopen or getsize. Publication uses the index.
            with self._lock:
                length = len(rendered)
                self._prune(ctx.get('previous_result'), incoming_size=length, incoming_files=1)
                cache = self._disk_inventory
                if len(cache)+1 > cache.MAX_FILES or cache.bytes+length > cache.MAX_BYTES:
                    raise _RadarBudget('protected tile cache full')
                parent = str(target.parent)
                if parent not in self._disk_inventory.directories:
                    chain = list(reversed(target.parent.parents)) + [target.parent]
                    root = Path(RADAR_DIR)
                    for directory in chain:
                        if directory != root and root not in directory.parents:
                            continue
                        name = str(directory)
                        if name not in self._disk_inventory.directories:
                            try: os.mkdir(name)
                            except FileExistsError: pass
                            self._disk_inventory.directories.add(name)
                tmp = str(target)+'.tmp'
                try:
                    with open(tmp, 'wb') as output:
                        output.write(rendered)
                    os.replace(tmp,target)
                    self._disk_inventory.add(disk_key,target,length,
                        dict(metadata, weatherPixels=echo_pixels))
                    self._disk_files=len(self._disk_inventory)
                    self._disk_bytes=self._disk_inventory.bytes
                finally:
                    try: os.unlink(tmp)
                    except FileNotFoundError: pass
            return rendered

        def checkpoint():
            self._checkpoint(ctx)
            if time.monotonic() >= deadline:
                raise TimeoutError('radar tile batch exceeded deadline')

        missing = []
        for tile in _radar_site_tiles(ctx, site):
            tx,ty,_,_ = tile
            if _radar_disk_key(source,site,stamp,ctx['zoom'],tx,ty,variant) in self._disk_inventory:
                yield tile, None
            else:
                missing.append(tile)
        if not missing:
            return
        tiles = iter(missing)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='radar-tile') as pool:
            pending = set()
            try:
                for tile in tiles:
                    checkpoint()
                    pending.add(pool.submit(fetch, tile))
                    if len(pending) == workers:
                        break
                while pending:
                    done, pending = wait(pending, timeout=max(0, deadline-time.monotonic()),
                                         return_when=FIRST_COMPLETED)
                    checkpoint()
                    # Inspect the whole completed batch before submitting more.
                    ready=[];error=None
                    for future in done:
                        try:ready.append(future.result())
                        except Exception as caught:
                            if error is None or isinstance(caught, (_RadarBudget, _RadarSuperseded, CircuitOpen)):
                                error = caught
                    for result in ready:
                        yield result
                    if isinstance(error, (_RadarBudget, _RadarSuperseded, CircuitOpen)):
                        raise error
                    if error is not None:
                        if failure_class(error) != 'host':
                            ctx[failure_class(error)+'_failure'] = True
                        ctx['last_error'] = str(error)
                        ctx['missing_tiles'] = True
                    for _ in done:
                        checkpoint()
                        tile = next(tiles, None)
                        if tile is not None:
                            pending.add(pool.submit(fetch, tile))
            finally:
                for future in pending:
                    future.cancel()

    def _sliding_frames(self, source, newest, ctx):
        """Retain published scan identities only within the same render geometry."""
        snap = self._result
        if (not snap.available or snap.source_id != source or
                snap.site_id != (ctx.get('site_id') if source == 'iem-nexrad-n0b' else None) or
                snap.zoom != ctx['zoom'] or snap.bounds != ctx['bounds'] or
                snap.center != dict(lat=ctx['station'][0], lon=ctx['station'][1]) or
                snap.legend != _RADAR_SOURCES[source]['legend'] or
                not snap.tiles or snap.tiles.get('revision') != _radar_render_revision(_radar_variant(ctx,source))):
            return {}
        return {f['ts']: dict(f) for f in snap.frames
                if max(newest, snap.ts_frame or newest) - RADAR_HISTORY_SEC <= f['ts']
                and not (f.get('primaryOnly') and not self._primary_only(ctx))
                and (source != 'iem-nexrad-n0b' or
                     set(map(tuple, f.get('requestedPairs', [(p['id'], p['ts']) for p in f['siteScans']])))
                     == set(_radar_site_pairs(ctx, f['ts'])))}

    def _hca_due(self, frame):
        """Missing reflectivity or classification inside the bounded upgrade window."""
        now = time.time()
        with self._lock:
            contributed = {(p['id'], p['ts']) for p in frame.get('siteScans', ())}
            for site, stamp in frame.get('requestedPairs', ()):
                if (site, stamp) in contributed or now - stamp > RADAR_N0H_UPGRADE_SEC:
                    continue
                key = (site, stamp)
                failed = self._level3_failed.get(key)
                if key in self._level3_scans or not failed or time.monotonic() >= failed[0]:
                    return True
            for p in frame.get('siteScans', ()):
                if p.get('filtered', True) or now - p['volumeTs'] > RADAR_N0H_UPGRADE_SEC:
                    continue
                key = (p['id'], p['volumeTs'], 'N0H')
                failed = self._level3_failed.get(key)
                if key in self._level3_scans or not failed or time.monotonic() >= failed[0]:
                    return True
        return False

    def _mosaic_cached(self, pairs, ts, ctx):
        from lib.radar_mosaic import read_frame_metadata
        revision = _radar_render_revision(_radar_variant(ctx, 'iem-nexrad-n0b'))
        root = Path(RADAR_DIR) / 't' / revision / 'iem-nexrad-n0b'
        for metadata in read_frame_metadata(root, _radar_stamp_text(ts), pairs, revision,
                self._disk_inventory.frame_metadata):
            if not self._hca_due(metadata) and all(_radar_present(ctx, 'iem-nexrad-n0b', metadata['mosaicKey'], ts,
                                 ctx['zoom'], x, y) for x, y, _, _ in ctx['tiles']):
                return metadata
        return None

    def _level3_fallback(self, error):
        now = time.monotonic()
        reason = f'{type(error).__name__}: {error}'[:200]
        with self._lock:
            first = self._level3_outage is None or now >= self._level3_outage['until']
            until = max(now + RADAR_LEVEL3_FALLBACK_SEC, self._cooldowns.get(RADAR_LEVEL3_TRANSPORT, 0))
            self._level3_outage = dict(until=until, wake=True, reason=reason, kind='unreachable',
                since=time.time() if first else self._level3_outage['since'])
            # The requested variant follows the outage from the moment it is
            # recorded, not from the next pass: every schedule (budget retry,
            # discovery, probe) prices transport through this flag, and a
            # stale True made them all wait out the Level III cooldown while
            # IEM could already draw.
            self._native_requested = False
        if first:
            self._logger.warning(f'almanac_emit: radar Level III unreachable, the site radar draws IEM tiles for {until-now:g} s - {reason}')

    def _level3_stalled(self, site, since):
        """ NOAA's S3 feed stopped publishing the primary's scans while IEM
        keeps advertising new ones. Draw IEM site tiles (labelled) and check
        Level III again after RADAR_LEVEL3_FALLBACK_SEC; a published product
        at or after `since` ends the stall. """
        now, lag = time.monotonic(), time.time() - since
        reason = f'{site} Level III not published for {lag:.0f} s'
        with self._lock:
            prior = self._level3_outage
            first = prior is None or prior.get('kind') != 'stalled'
            until = max(now + RADAR_LEVEL3_FALLBACK_SEC, prior['until'] if prior else 0,
                        self._cooldowns.get(RADAR_LEVEL3_TRANSPORT, 0))
            self._level3_outage = dict(until=until, wake=True, reason=reason, kind='stalled',
                since=time.time() if first else prior['since'])
            self._native_requested = False
            stall = self._level3_stall
            log = stall['loggedAt'] is None or time.time() - stall['loggedAt'] >= RADAR_FAILURE_LOG_SEC
            if log:
                held, stall['loggedAt'], stall['suppressed'] = stall['suppressed'], time.time(), 0
            else:
                stall['suppressed'] += 1
        if log:
            self._logger.warning(f'almanac_emit: radar Level III stalled: {reason}, first unpublished scan '
                           f'{_radar_stamp_text(since)}; the site radar draws IEM tiles ({held} not logged)')

    def _qc_failed(self, site, volume_ts, error):
        text = f'{type(error).__name__}: {error}'[:200]
        with self._lock:
            self._qc_failures += 1
            self._qc_last_error = dict(site=site, volumeTs=volume_ts, error=text, ts=time.time())
            key = (site, text)
            first = key not in self._qc_logged and len(self._qc_logged) < 256
            if first:
                self._qc_logged.add(key)
            # The decoded N0H is cached, and a cached classification makes the
            # frame "due" for its upgrade on every pass. Drop it and remember the
            # failure past the upgrade window, so the same QC is not rerun.
            self._level3_scans.pop((site, volume_ts, 'N0H'), None)
        self._remember_level3_failure((site, volume_ts, 'N0H'), RADAR_N0H_UPGRADE_SEC + 60,
                                            'classification QC failed: ' + text, type(error))
        if first:
            self._logger.warning(f'almanac_emit: radar {site} classification (N0H) QC failed, drawing it unfiltered - {text}')

    def _level3_site_failures(self, failures):
        """ Per-site Level III failures inside a frame that still drew: counted
        in /health.radar.mosaic.siteFailures. Warn only on outages or products
        still unpublished after 600 seconds, rate limited per site. """
        now = time.time()
        for failure in failures:
            site, error = failure[:2]
            stamp = failure[2] if len(failure) > 2 else None
            if isinstance(error, (_RadarSuperseded, _RadarBudget)):
                continue
            text = f'{type(error).__name__}: {error}'[:200]
            with self._lock:
                entry = self._level3_site_errors.setdefault(site, dict(count=0, loggedAt=None, suppressed=0))
                entry.update(count=entry['count'] + 1, lastError=text, lastTs=now)
                while len(self._level3_site_errors) > 32:
                    self._level3_site_errors.pop(next(iter(self._level3_site_errors)))
                outage = isinstance(error, CircuitOpen) or is_transport_error(error) or failure_class(error) in ('local', 'ambiguous')
                overdue = isinstance(error, _RadarScanUnpublished) and stamp is not None and now - stamp > RADAR_LEVEL3_UNPUBLISHED_LOG_SEC
                if not (outage or overdue):
                    continue
                if entry['loggedAt'] is not None and now - entry['loggedAt'] < RADAR_FAILURE_LOG_SEC:
                    entry['suppressed'] += 1
                    continue
                held, entry['loggedAt'], entry['suppressed'] = entry['suppressed'], now, 0
            self._logger.warning(f'almanac_emit: radar {site} Level III scan unavailable ({entry["count"]} so far, {held} not logged) - {text}')

    def _level3_cooling(self):
        return time.monotonic() < self._cooldowns.get(RADAR_LEVEL3_TRANSPORT, 0)

    def _level3_down(self):
        outage = self._level3_outage
        if time.monotonic() < self._cooldowns.get(RADAR_LEVEL3_TRANSPORT, 0):
            return True
        if outage is not None and time.monotonic() < outage['until']:
            return True
        return bool(self._health.probe_delay({RADAR_LEVEL3_TRANSPORT}))

    def _level3_fallback_health(self):
        outage = self._level3_outage
        now = time.monotonic()
        breaker = bool(self._health.probe_delay({RADAR_LEVEL3_TRANSPORT}))
        until = max(outage['until'] if outage else 0, self._cooldowns.get(RADAR_LEVEL3_TRANSPORT, 0))
        active = breaker or now < until
        return dict(active=bool(active), breakerOpen=breaker,
                    reason=outage['reason'] if outage else ('Level III breaker open' if breaker else 'Level III cooldown' if now < until else None),
                    since=outage['since'] if outage else None,
                    retrySec=round(until-now, 1) if now < until else None)

    def _native_fallback(self, snap):
        """Describe the drawn pixels, including retained tiles during recovery."""
        showing = snap.source_mode == 'site' and bool(snap.frames) and not _radar_is_native((snap.tiles or {}).get('variant'))
        paused = self._native_budget.snapshot()['ceilingState'] == 'paused'
        outage = self._level3_outage
        down = self._level3_down() or outage is not None
        reason = ('daily-limit' if paused else 'level3-stalled' if outage is not None and outage.get('kind') == 'stalled'
                  else 'level3-unreachable' if down else None)
        return dict(active=showing, reason=reason, recovering=showing and reason is None)

    @staticmethod
    def _primary_only(ctx):
        # Shadow mode supplies effective live; it never applies a shadow tier.
        return ctx.get('attention') == 'watch'

    def _mosaic_inputs(self, pairs, ts, ctx, deadline):
        from lib.radar_mosaic import mosaic_key, quality_control
        scans, contributors, identities = [], [], []
        cancelled = _Event()
        # Jobs/cancellation belong to the frame; executors belong to the emitter.
        # Bounded spare capacity isolates the next frame from stalled transports.
        hca_pool = self._hca_pool
        hca_jobs = []
        # Each successful reflectivity immediately starts its independent HCA
        # flight, while the other sites' reflectivity is still being acquired.
        def acquire(site, stamp):
            scan = self._level3_scan(site, stamp, ctx, deadline)
            # Registration and frame cancellation share one short lock, so a
            # reflectivity completion cannot orphan an untracked HCA flight.
            with self._lock:
                if cancelled.is_set():
                    raise _RadarSuperseded('frame inputs cancelled')
                future = hca_pool.submit(self._level3_scan,
                    site, stamp, ctx, deadline - 2, 'N0H', scan.volume_ts)
                hca_jobs.append((site, scan.volume_ts, future))
            return scan, future
        available = []
        pool = self._input_pool
        jobs = [(site, stamp, pool.submit(acquire, site, stamp)) for site, stamp in sorted(pairs)
                if ts - 480 <= stamp <= ts + 60]
        failures = []
        try:
            for site, stamp, job in jobs:
                try:
                    scan, hca = job.result(timeout=max(.001, deadline-time.monotonic()))
                    available.append((site, stamp, scan, hca))
                except _RadarSuperseded:
                    raise
                except _RadarBudget as error:
                    # A Level III 429 (or its cooldown refusing the next
                    # request) is a Level III outage, not a local yield:
                    # IEM's site tiles are on another host and can draw now.
                    if not self._level3_cooling():
                        raise
                    ctx.setdefault('site_reasons', {})[site] = 'scan unavailable'
                    ctx['last_error'] = str(error)
                    failures.append((site, error, stamp))
                except Exception as error:
                    ctx.setdefault('site_reasons', {})[site] = 'scan unavailable'
                    ctx['last_error'] = str(error)
                    failures.append((site, error, stamp))
            self._level3_site_failures(failures)
            outages = [e for _, e, _ in failures if isinstance(e, CircuitOpen)
                       or failure_class(e) in ('local', 'ambiguous') or is_transport_error(e)]
            limited = any(isinstance(e, _RadarBudget) for _, e, _ in failures)
            if jobs and not available and (outages or limited):
                # Level III has its own host; IEM's site tiles can still be healthy.
                # A 429 already recorded its outage (with the provider's reason).
                ctx['level3_failed'] = True
                if outages:
                    self._level3_fallback(outages[0])
            if (self._primary_only(ctx) and failures and not available
                    and all(isinstance(e, _RadarScanUnpublished) for _, e, _ in failures)):
                ctx['unpublished_stamp'] = ts
                ctx['unpublished_until'] = max(self._level3_failed[(site, stamp)][0]
                    for site, _, stamp in failures)
                raise failures[0][1]
            # One wait for the frame, never a timeout multiplied by site count.
            end = min(deadline - 2, time.monotonic() + RADAR_N0H_FRAME_BUDGET_SEC)
            arrived, _ = wait([p[3] for p in available], timeout=max(0, end-time.monotonic()))
            for site, stamp, scan, hca in available:
                filtered, has_hca, qc = scan, False, False
                try:
                    if hca not in arrived:
                        if ctx.get('prefetch'):
                            raise _RadarClassificationPending('classification unfinished for prefetch')
                    else:
                        classification = hca.result()  # failed fetches keep their own retry
                        qc = True
                        filtered = quality_control(scan, classification)
                        has_hca = True
                except (_RadarSuperseded, _RadarClassificationPending):
                    raise
                except _RadarBudget:
                    if ctx.get('prefetch'):
                        raise
                except Exception as error:
                    # Optional classification cannot block N0B. Rest failed QC
                    # past this volume's upgrade window, but keep fetch retries.
                    if qc:
                        self._qc_failed(site, scan.volume_ts, error)
                scans.append(filtered)
                contributors.append(dict(id=site, ts=stamp, volumeTs=scan.volume_ts, filtered=has_hca))
                identities.append((site, scan.volume_ts, has_hca))
        finally:
            with self._lock:
                cancelled.set()
                flights = tuple(hca_jobs)
            for _, _, job in jobs:
                job.cancel()
            for site, volume, hca in flights:
                if not hca.done() or hca.cancelled():
                    self._remember_level3_failure((site, volume, 'N0H'),
                        10, 'classification flight timed out or cancelled', TimeoutError)
                    hca.cancel()
        return dict(mosaicKey=mosaic_key(identities, _radar_render_revision(_radar_variant(ctx, 'iem-nexrad-n0b'))), siteScans=contributors,
                    requestedPairs=sorted([list(p) for p in pairs]),
                    unfilteredSites=[p['id'] for p in contributors if not p['filtered']]), tuple(scans)

    def _fill_frame(self, source, ts, ctx, deadline, tile_url, archive_url=None, layers=None, on_validated=None):
        """Fill independent immutable tiles; never allocate viewport RGBA buffers."""
        self._checkpoint(ctx)
        if time.monotonic() >= deadline:
            raise TimeoutError('radar acquisition deadline')
        if ctx['builds'] >= RADAR_MAX_FRAME_BUILDS_PER_PASS:
            raise _RadarBudget('radar tile-set budget')
        ctx['builds'] += 1
        pairs = tuple((s,t) for s,t,_ in layers) if layers is not None else None
        frame = _radar_frame(source,ts,ctx,pairs)
        if source == 'iem-nexrad-n0b':
            frame.update(primaryOnly=self._primary_only(ctx), requestedPairs=sorted([list(p) for p in pairs or ()]))
        mosaic = layers is not None and _radar_is_native(_radar_variant(ctx, source))
        if mosaic:
            metadata = self._mosaic_cached(pairs, ts, ctx)
            scans = ()
            if metadata is None or self._hca_due(metadata):
                metadata, scans = self._mosaic_inputs(pairs, ts, ctx, deadline)
            frame.update(metadata)
            if not frame['siteScans']:
                return dict(frame, publishable=False, acquiredSites=[])
        work = ([(frame['mosaicKey'], ts, None)] if mosaic else
                list(reversed(layers)) if layers is not None else [(None,ts,tile_url)])
        if archive_url:
            try: self._archive_probe(source,archive_url,deadline,ctx.get('request_reserve',0),
                negative_ttl=20 if not ctx.get('prefetch') and ctx.get('refresh', {}).get('state') == 'newest'
                else RADAR_NEGATIVE_CACHE_SEC)
            except (_RadarBudget,_RadarSuperseded,CircuitOpen): raise
            except Exception as error:
                if is_transport_error(error): raise
                ctx['last_error']=str(error)
                return frame
        if on_validated is not None: on_validated()
        # A validated discovery can list a pending scan before its first slow tile.
        # Cold starts and changed geometry still publish on first measurement.
        retained = self._sliding_frames(source, ts, ctx)
        if (retained and not ctx.get('prefetch') and self._result.ts_frame < ts):
            retained[ts] = dict(frame)
            window = tuple(retained[t] for t in sorted(retained))
            self._result = _radar_tile_snapshot(self._result._replace(frames=window,
                tiles=_radar_tile_manifest(source, window, ctx)))
            self._emit_now()
        drawn = []; present = set()
        for site,stamp,url in work:
            try:
                for tile, raw in self._tile_batch(source,stamp,
                        dict(ctx, mosaic_scans=scans, mosaic_filtered=tuple(
                            p['filtered'] for p in metadata['siteScans'])) if mosaic else ctx,deadline,url,site):
                    present.add((site,stamp))
                    if raw is None:
                        continue
                    self._checkpoint(ctx)
                    if not ctx.get('prefetch') and 'firstVisibleTile' not in ctx.setdefault('milestones',set()):
                        ctx['milestones'].add('firstVisibleTile')
                        self._phase_metrics.append(dict(phase='firstVisibleTile',at=time.monotonic(),cpuSec=time.process_time(),intent=dict(ctx.get('intent',{})),requests=len(self._request_times),bytes=getattr(self,'_received_bytes',0)))
                        self._phase_metrics=self._phase_metrics[-128:]
                    # Publish partial newest inventory as each independent tile lands.
                    snap = self._result
                    if (snap.source_id != source or
                            source == 'iem-nexrad-n0b' and snap.site_id != ctx.get('site_id') or
                            snap.ts_frame is None or ts >= snap.ts_frame) and not ctx.get('prefetch') and not ctx.get('staging_source'):
                        settings = _RADAR_SOURCES[source]
                        # A tile is an independently validated measurement; publish
                        # its stamp before the remaining viewport tiles finish.
                        partial = _RADAR_NONE._replace(available=True,reason=None,frames=(dict(frame),),
                            ts_frame=ts,center=dict(lat=ctx['station'][0],lon=ctx['station'][1]),
                            zoom=ctx['zoom'],mpp=ctx['mpp'],bounds=ctx['bounds'],scalebar=ctx['bar'],
                            rings=ctx['rings'],nexrad=ctx['nexrad'],ts_fetch=time.time(),source_id=source,
                            **settings,zoom_desired=ctx['desired'],zoom_auto_level=ctx['auto_zoom'],
                            geo=ctx.get('geo'),units=ctx['unit'],sources=tuple(ctx['sources']),
                            source_mode='site' if source=='iem-nexrad-n0b' else 'mosaic',
                            site_id=ctx.get('site_id'),sites=tuple(ctx.get('sites',())),
                            sites_considered=ctx.get('sites_considered',0),
                            partial_coverage=_radar_partial_coverage(source,frame,ctx),
                            tiles=_radar_tile_manifest(source,[frame],ctx))
                        retained = self._sliding_frames(source, ts, ctx)
                        if retained:
                            retained.setdefault(ts, dict(frame))
                            window = tuple(retained[t] for t in sorted(retained))
                            partial = partial._replace(frames=window,
                                tiles=_radar_tile_manifest(source, window, ctx),
                                ts_fetch=snap.ts_fetch if snap.ts_frame == ts else partial.ts_fetch)
                        self._note_source(source, ctx)
                        self._result=_radar_tile_snapshot(partial)
                        self._health.last_success = time.time()
                        self._emit_now()
                if ctx.get('reuse_newest') and not any(_radar_present(ctx,source,site,stamp,ctx['zoom'],x,y)
                        for x,y,_,_ in _radar_site_tiles(ctx,site)):
                    self._forget(source,site)
                    raise _RadarRevalidate('remembered newest unavailable')
                drawn.append((site,stamp))
            except (_RadarSuperseded, _RadarBudget, CircuitOpen, _RadarRevalidate):
                raise
            except Exception as error:
                if is_transport_error(error): raise
                self._forget(source,site)
                if ctx.get('reuse_newest'): raise _RadarRevalidate('remembered newest unavailable') from error
                ctx['last_error'] = str(error)
                if site: ctx.setdefault('site_reasons',{})[site] = 'scan unavailable'
            # Executor drain may finish paid-for tiles after the first failure.
            if any(_radar_present(ctx,source,site,stamp,ctx['zoom'],x,y)
                   for x,y,_,_ in _radar_site_tiles(ctx,site)):
                present.add((site,stamp))
        if layers is not None and not mosaic:
            frame['siteScans'] = [dict(id=s,ts=t) for s,t in pairs]
        # Admission needs a real measurement; manifest coverage separately
        # determines whether the entire tile set is ready for playback.
        frame['acquiredSites'] = (frame['siteScans'] if mosaic and present else
                                  [dict(id=s,ts=t) for s,t in (pairs or ()) if (s,t) in present])
        frame['publishable'] = bool(present)
        frame['complete'] = bool(present) and all(all(_radar_present(ctx,source,site,stamp,ctx['zoom'],x,y)
            for x,y,_,_ in _radar_site_tiles(ctx,site)) for site,stamp in _radar_frame_pairs(frame))
        frame['echo'] = self._frame_echo(ctx, source, _radar_frame_pairs(frame), ts) if frame['complete'] else None
        if mosaic and present:
            from lib.radar_mosaic import write_frame_metadata
            path = _radar_tile_path(source, frame['mosaicKey'], ts, ctx['zoom'], 0, 0, _radar_variant(ctx, source)).parents[2] / 'frame.json'
            write_frame_metadata(path, _radar_stamp_text(ts), pairs, metadata, _radar_render_revision(_radar_variant(ctx, source)),
                                 self._disk_inventory.frame_metadata)
        return frame

    def _known(self, source, ctx, site=None):
        entry = self._newest.get((source, site))
        if (ctx.get('intent_triggered') and entry is not None and
                0 <= time.monotonic() - entry[0] < _RADAR_SOURCES[source]['cadence']):
            return entry[1]
        return None

    def _forget(self, source, site=None):
        for key in list(self._newest):
            if key[0] == source and (site is None or key[1] == site):
                del self._newest[key]

    def _archive_probe(self, source, url, deadline, reserve=0, negative_ttl=RADAR_NEGATIVE_CACHE_SEC):
        if url in self._archive_positive:
            return
        if time.monotonic() < self._negative.get(url, 0):
            raise ValueError('archive unavailable')
        try:
            self._request(source, url, deadline, method='HEAD', **(dict(reserve=reserve) if reserve else {}))
        except (_RadarBudget, _RadarSuperseded):
            raise
        except Exception as error:
            if not is_transport_error(error):
                self._negative[url] = time.monotonic() + negative_ttl
            raise
        if len(self._archive_positive) >= 128:
            self._archive_positive.clear()
        self._archive_positive.add(url)

    def _iem_scan(self, ctx):
        """Discover newest; foreground and idle warming share validation lifetime."""
        source = 'iem-mrms-lcref'
        deadline = min(ctx['deadline'], time.monotonic() + RADAR_PRIMARY_DEADLINE_SEC)
        known = self._known(source, ctx)
        now = time.time()
        if known is not None and not 0 <= now - known['newest'] <= RADAR_IEM_STALE_SEC:
            known = None
        if known is None:
            self._forget(source)
            meta = json.loads(self._request(source, RADAR_IEM_METADATA_URL, deadline, metadata=True,
                **(dict(reserve=ctx['request_reserve']) if ctx.get('request_reserve') else {})))['meta']
            valid = datetime.fromisoformat(meta['end_valid'].replace('Z', '+00:00'))
            ts = int(valid.timestamp())
            if (valid.utcoffset() != timedelta(0) or meta.get('product') != 'lcref' or
                    meta.get('units') != '0.5 dBZ' or ts % RADAR_IEM_FRAME_INTERVAL_SEC or
                    not 0 <= now - ts <= RADAR_IEM_STALE_SEC):
                raise ValueError('invalid or stale IEM metadata')
            newest = ts
        else:
            newest = known['newest']
        return newest, known, deadline

    def _iem_frames(self, ctx):
        """Acquire a fresh complete primary first, probing even UTC slots backward."""
        source = 'iem-mrms-lcref'
        newest, known, deadline = self._iem_scan(ctx)
        self._discovery_unchanged(source, newest, ctx, dict(newest=newest))
        now = time.time()
        self._publish_refresh(ctx, frameTotal=RADAR_HISTORY_SEC // 120 + 1 if ctx['viewed'] else 1)
        def build(stamp, limit):
            utc = datetime.fromtimestamp(stamp, timezone.utc)
            def validated():
                # Superseding intents can reuse discovery even mid-tile-batch.
                self._newest[(source, None)] = (time.monotonic(), dict(newest=stamp))
            return self._fill_frame(source, stamp, ctx, limit,
                lambda x, y: RADAR_IEM_TILE_TEMPLATE.format(stamp=utc.strftime('%Y%m%d%H%M'),
                    z=ctx['zoom'], x=x, y=y),
                None if known is not None else utc.strftime(RADAR_IEM_ARCHIVE_TEMPLATE),
                on_validated=validated if known is None and stamp == newest else None)
        for candidate in range(newest, int(now - RADAR_IEM_STALE_SEC) - 1, -120):
            ctx['candidates'].append(candidate)
            ctx['reuse_newest'] = known is not None
            latest = build(candidate, deadline)
            ctx['reuse_newest'] = False
            if not latest.get('publishable',latest['complete']):
                self._forget(source)
                if known is not None:
                    raise _RadarRevalidate('remembered newest unavailable')
            if latest.get('publishable',latest['complete']):
                if time.time() - candidate > RADAR_IEM_STALE_SEC:
                    break
                return self._history(source, candidate, newest, ctx, build, latest)
        raise ValueError('no fresh complete IEM frame: ' + ctx.get('last_error', 'unavailable'))

    def _remember_level3_failure(self, key, retry, message, error_type):
        with self._lock:
            if key in self._level3_scans:
                return  # a late success already won
            self._level3_failed[key] = (time.monotonic()+retry, message, error_type)
            while len(self._level3_failed) > 2 * RADAR_LEVEL3_SCAN_CACHE:
                self._level3_failed.pop(next(iter(self._level3_failed)))

    def _level3_scan(self, site, stamp, ctx, deadline, product="N0B", volume_ts=None):
        """Share a bounded scan acquisition, including its failure, across tiles.

        Waiters never become owners of an obsolete flight. No completion or
        negative-cache entry retains an exception traceback (and its arrays).
        """
        import urllib.error
        import xml.etree.ElementTree as ET
        from lib.radar_level3 import decode, decode_n0h, match_key, s3_key_time
        from lib.radar_palette import DISPLAY_FLOOR_DBZ
        stamp_ts = datetime.strptime(_radar_stamp_text(stamp), '%Y%m%d%H%M').replace(tzinfo=timezone.utc).timestamp()
        if product not in ('N0B', 'N0H'):
            raise ValueError('unsupported Level III product')
        key = (site, stamp_ts) if product == 'N0B' else (site, volume_ts if volume_ts is not None else stamp_ts, product)
        self._checkpoint(ctx)
        with self._lock:
            scan = self._level3_scans.get(key)
            if scan is not None:
                self._level3_scans.move_to_end(key)
                return scan
            failed = self._level3_failed.get(key)
            if failed and (time.monotonic() < failed[0] or product == 'N0H'
                           and time.time() - key[1] > RADAR_N0H_UPGRADE_SEC):
                raise failed[2](failed[1])
            flight = self._level3_flights.get(key)
            owner = flight is None
            if owner:
                flight = dict(done=_Event(), scan=None, error=None)
                self._level3_flights[key] = flight
        if not owner:
            while not flight['done'].is_set():
                self._checkpoint(ctx)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('level3 scan wait deadline')
                flight['done'].wait(min(.1, remaining))
            self._checkpoint(ctx)
            if flight['error'] is not None:
                error_type, message = flight['error']
                raise error_type(message)
            return flight['scan']
        reserve = ctx.get('request_reserve', RADAR_HISTORY_RESERVE if ctx.get('prefetch') else 0)
        options = dict(reserve=reserve) if reserve else {}
        source = RADAR_LEVEL3_TRANSPORT if product == 'N0B' else RADAR_N0H_TRANSPORT
        if product == 'N0H':
            options['health'] = self._n0h_health
        try:
            prefix = '%s_%s_%s' % (site[1:], product, datetime.fromtimestamp(stamp_ts, timezone.utc).strftime('%Y_%m_%d_%H'))
            with self._lock:
                listed = self._level3_listings.get((site, prefix))
            def matching(keys):
                if volume_ts is not None:
                    return next((k for k in keys if s3_key_time(k, product) == volume_ts), None)
                return match_key(keys, stamp_ts, product)
            name = matching(listed[1]) if listed else None
            # Optional HCA has no mandatory-source probe pass. Its next
            # acquisition must half-open through a validated hourly listing,
            # even when that listing already contains the requested object.
            probe_listing = product == 'N0H' and bool(self._n0h_health.probes(source))
            if probe_listing or name is None and (listed is None or time.monotonic()-listed[0] > 20):
                keys = ()
                def validate_listing(raw):
                    nonlocal keys
                    try:
                        root = ET.fromstring(raw)
                    except ET.ParseError as error:
                        raise ValueError('level3 invalid listing') from error
                    if root.tag.rsplit('}', 1)[-1] != 'ListBucketResult':
                        raise ValueError('level3 invalid listing')
                    if any(e.text == 'true' for e in root.iter() if e.tag.rsplit('}', 1)[-1] == 'IsTruncated'):
                        raise ValueError('level3 truncated hourly listing')
                    keys = tuple(e.text for e in root.iter() if e.tag.rsplit('}', 1)[-1] == 'Key'
                                 and e.text and re.fullmatch(r'[A-Z0-9_]{1,64}', e.text)
                                 and e.text.startswith(prefix + '_'))
                self._request(source, RADAR_LEVEL3_BUCKET+'?list-type=2&prefix='+prefix,
                    min(deadline, time.monotonic()+RADAR_TILE_TIMEOUT_SEC),
                    metadata=True, validate=validate_listing, **options)
                listed = (time.monotonic(), keys)
                with self._lock:
                    self._level3_listings[(site, prefix)] = listed
                    while len(self._level3_listings) > RADAR_LEVEL3_LISTING_CACHE:
                        # Prefix suffixes sort chronologically; discard the oldest
                        # hour first, then least recently fetched within that hour.
                        victim = min(self._level3_listings, key=lambda k:
                            (k[1][-13:], self._level3_listings[k][0]))
                        self._level3_listings.pop(victim)
                name = matching(listed[1])
            if name is None:
                raise _RadarScanUnpublished('level3 scan %s %s not published' % (site, _radar_stamp_text(stamp)))
            self._checkpoint(ctx)
            def validate_product(raw):
                nonlocal scan
                lat, lon, _ = _NEXRAD_SITES[site]
                scan = (decode(raw, expect_site=(lat, lon), speckle_dbz=DISPLAY_FLOOR_DBZ) if product == 'N0B'
                        else decode_n0h(raw, expect_site=(lat, lon)))
                if (not 0 <= scan.volume_ts - stamp_ts < 60 or scan.volume_ts != s3_key_time(name, product)
                        or volume_ts is not None and scan.volume_ts != volume_ts):
                    raise ValueError('level3 volume time does not match the scan')
            self._request(source, RADAR_LEVEL3_BUCKET+name,
                min(deadline, time.monotonic()+2*RADAR_TILE_TIMEOUT_SEC), validate=validate_product, **options)
            with self._lock:
                if product == 'N0B':
                    # A decoded product proves recovery; a timer/listing does not.
                    # A stall ends only with the stalled site's own newer product.
                    stall = self._level3_stall
                    if stall is not None and stall['site'] == site and stamp_ts >= stall['since']:
                        self._level3_stall = stall = None
                    if stall is None or (self._level3_outage or {}).get('kind') != 'stalled':
                        self._level3_outage = None
                self._level3_scans[key] = scan
                self._level3_failed.pop(key, None)
                members = [k for k in self._level3_scans if (len(k) == 2) == (product == 'N0B')]
                for old in members[:-RADAR_LEVEL3_SCAN_CACHE]:
                    del self._level3_scans[old]
            flight['scan'] = scan
            return scan
        except BaseException as error:
            control = isinstance(error, (_RadarBudget, _RadarSuperseded, CircuitOpen))
            # Preserve pass control and local/ambiguous transport classification
            # without keeping socket objects or decode tracebacks alive.
            kind = failure_class(error)
            error_type = (type(error) if control or isinstance(error, _RadarScanUnpublished) else LocalTransportError if kind == 'local'
                          else AmbiguousTransportError if kind == 'ambiguous'
                          else TimeoutError if is_transport_error(error) else ValueError)
            message = type(error).__name__ + ': ' + str(error)
            flight['error'] = (error_type, message)
            if isinstance(error, Exception) and (not control or product == 'N0H' and isinstance(error, _RadarSuperseded)):
                retry = (20 if product == 'N0H' else RADAR_LEVEL3_RETRY_SEC) if isinstance(error, (ValueError, urllib.error.HTTPError)) else 10
                self._remember_level3_failure(key, retry, message,
                    TimeoutError if isinstance(error, _RadarSuperseded) else error_type)
            raise
        finally:
            with self._lock:
                flight['done'].set()
                del self._level3_flights[key]

    def _site_listing(self, ctx, site):
        """One listing owner for viewport acquisition and Region's cadence check."""
        from urllib.parse import urlencode
        source = 'iem-nexrad-n0b'
        now = time.time()
        fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%dT%H:%MZ')
        deadline = min(ctx['deadline'], time.monotonic() + RADAR_PRIMARY_DEADLINE_SEC)
        self._checkpoint(ctx)
        url = RADAR_SITE_LIST_URL + '?' + urlencode(dict(operation='list', radar=site['id'][1:],
            product='N0B', start=fmt(now - RADAR_HISTORY_SEC - RADAR_SITE_MAX_AGE_SEC), end=fmt(now)))
        cached = ctx.get('listing_results', {}).get(site['id'])
        if cached is not None:
            state, result = cached
            site.update(state)
            return result
        stamps = []
        known = None
        reason = 'not reporting'
        try:
            known = self._known(source, dict(ctx, intent_triggered=True) if ctx.get('auto_listing_cache') else ctx, site['id'])
            if known is not None:
                stamps = [t for t in known['stamps'] if 0 <= now-t <= RADAR_HISTORY_SEC + RADAR_SITE_MAX_AGE_SEC]
            else:
                self._forget(source, site['id'])
                listing = json.loads(self._request(source, url, deadline, metadata=True,
                    **(dict(reserve=ctx['request_reserve']) if ctx.get('request_reserve') else {})))
                for scan in listing['scans']:
                    valid = datetime.fromisoformat(scan['ts'].replace('Z', '+00:00'))
                    if valid.utcoffset() != timedelta(0):
                        raise ValueError('non-UTC site scan')
                    ts = int(valid.timestamp())
                    if 0 <= now - ts <= RADAR_HISTORY_SEC + RADAR_SITE_MAX_AGE_SEC and ts % 60 == 0:
                        stamps.append(ts)
                stamps = sorted(set(stamps))
                self._note_latency(site['id'], stamps, now)
                self._newest[(source, site['id'])] = (time.monotonic(),
                    dict(newest=stamps[-1] if stamps else None, stamps=tuple(stamps), checkedTs=now))
        except (_RadarBudget, _RadarSuperseded):
            raise
        except Exception as error:
            stamps = []
            reason = 'scan unavailable'
            self._log_failure(source, error, scope=site["id"])
        else:
            if known is None:
                with self._lock:
                    self._pass["recovered"].add((source, site["id"]))
        newest = stamps[-1] if stamps else None
        site.update(reason=None if newest is not None and now-newest < RADAR_SITE_MAX_AGE_SEC else reason)
        site.update(reporting=None if reason == 'scan unavailable' else newest is not None and now-newest < RADAR_SITE_MAX_AGE_SEC,
                    newestTs=newest, ageSec=int(now-newest) if newest is not None else None)
        checked = known.get('checkedTs') if known is not None else now
        if known is None:
            # Cached acquisition may age scans out of its history window, but
            # only an actual listing attempt can replace the last-check evidence.
            with self._lock:
                previous = self._site_status.get(site['id'], {})
                failed_since = previous.get('failedSince', now) if reason == 'scan unavailable' else None
                if failed_since is not None and now-failed_since >= _RADAR_SOURCES[source]['cadence']:
                    site['reporting'] = False
                self._site_status[site['id']] = dict(reporting=site['reporting'], newestTs=newest,
                    reason=site['reason'], checkedTs=checked)
                if failed_since is not None:
                    self._site_status[site['id']]['failedSince'] = failed_since
        result = site['id'], tuple(stamps), known is not None
        if 'listing_results' in ctx and known is None:
            ctx['listing_results'][site['id']] = ({k: site[k] for k in ('reporting', 'newestTs', 'ageSec', 'reason')}, result)
        return result

    def _note_latency(self, site_id, stamps, now):
        """Record publication latency from one successful listing.

        A scan's latency sample is now - scan_ts on the listing where it first
        appears, and only when that is evidence of IEM's delay:
        - "first" is against every scan this site has EVER listed (kept for
          the listing window), not just the previous listing, so a listing
          that regresses (empty, partial) and then recovers never makes old
          scans look newly published;
        - only a scan that advances the site's newest-ever scan counts, and a
          listing gives at most ONE sample (its newest new scan): a backlog
          delivered in one batch is one late arrival, not several votes;
        - the previous consistent listing must be recent
          (RADAR_SITE_LATENCY_GAP_SEC): a quiet tier lists every 15 minutes,
          and its gaps measure our polling, not IEM. A regressed listing does
          not count as a recent one.
        Samples carry their time and expire (RADAR_SITE_LATENCY_MAX_AGE_SEC),
        so contamination cannot outlive slow polling. The first listing of a
        site only seeds what it has seen."""
        retention = RADAR_HISTORY_SEC + RADAR_SITE_MAX_AGE_SEC + RADAR_SITE_LATENCY_GAP_SEC
        with self._lock:
            state = self._latency.get(site_id)
            if state is None:
                state = self._latency[site_id] = dict(checked=None, newest=None, seen={},
                    samples=deque(maxlen=RADAR_SITE_LATENCY_SAMPLES))
            seen = state['seen']
            for ts in [ts for ts in seen if now - ts > retention]:
                del seen[ts]
            listed_newest = max(stamps, default=None)
            regressed = state['newest'] is not None and (listed_newest is None or listed_newest < state['newest'])
            fresh = [ts for ts in stamps if ts not in seen]
            previous = state['checked']
            if (fresh and not regressed and previous is not None and 0 <= now - previous <= RADAR_SITE_LATENCY_GAP_SEC
                    and (state['newest'] is None or max(fresh) > state['newest'])):
                ts = max(fresh)
                if 0 <= now - ts <= RADAR_SITE_MAX_AGE_SEC + RADAR_SITE_LATENCY_GAP_SEC:
                    state['samples'].append((now, now - ts))
            for ts in fresh:
                seen[ts] = now
            if not regressed:
                state['checked'] = now
            if listed_newest is not None and (state['newest'] is None or listed_newest > state['newest']):
                state['newest'] = listed_newest

    def _site_latency(self, site_id, now=None):
        now = time.time() if now is None else now
        with self._lock:
            state = self._latency.get(site_id)
            if not state:
                return None
            return _radar_scan_latency([value for at, value in state['samples']
                                        if 0 <= now - at <= RADAR_SITE_LATENCY_MAX_AGE_SEC])

    def _site_discover(self, ctx, *, primary_only=False):
        """Concurrent per-site listings, reused by intent and idle tile warming."""
        primary_only = primary_only or self._primary_only(ctx)
        sites, considered = _radar_sites(ctx['station'], ctx['bounds'])
        if primary_only:
            # Respect the saved camera even when only one radar is acquired:
            # candidates are in-view sites, nearest to the station first.
            sites = sorted(sites, key=lambda s: (s['distanceMeters'], s['id']))
        if not sites:
            ctx['site_failure'] = 'out of view'
            raise ValueError('no viewport coverage')
        for site in sites:
            site.update(reporting=False, newestTs=None, ageSec=None, primary=False, contributing=False, reason='not reporting')
        ctx.update(sites=sites, sites_considered=considered, site_scans={})
        deadline = min(ctx['deadline'], time.monotonic() + RADAR_PRIMARY_DEADLINE_SEC)
        # Timeline ownership is independent of viewport selection and arrival order.
        timeline = sorted((dict(id=i, lat=a, lon=b, distanceMeters=distance_meters(*ctx['station'],a,b))
                           for i,(a,b,_) in _NEXRAD_SITES.items()
                           if distance_meters(*ctx['station'],a,b) <= RADAR_SITE_RANGE_METERS),
                          key=lambda s:(s['distanceMeters'],s['id']))[:RADAR_SITE_MAX_COUNT]
        if primary_only:
            # Like live, the primary is the nearest REPORTING radar in view.
            # List in distance order and stop at the first that reports; a
            # site whose listing is fresh evidence of "not reporting" is
            # skipped without a request. A failed listing (IEM trouble) stops
            # the search: the next site's listing shares that host.
            examined = []
            cadence = _RADAR_SOURCES['iem-nexrad-n0b']['cadence']
            for site in sites:
                examined.append(site)
                state = self._site_status.get(site['id'], {})
                if (state.get('reporting') is False and state.get('reason') == 'not reporting'
                        and state.get('checkedTs') is not None and 0 <= time.time()-state['checkedTs'] < cadence):
                    site.update(newestTs=state.get('newestTs'))
                    continue
                self._checkpoint(ctx)
                if deadline <= time.monotonic():
                    raise TimeoutError('radar site discovery deadline')
                ident, stamps, reused = self._site_listing(ctx, site)
                ctx['site_scans'][ident] = stamps
                ctx['reuse_newest'] = ctx.get('reuse_newest', False) or reused
                if site['reporting'] or site['reason'] == 'scan unavailable':
                    break
            sites = timeline = examined
            ctx['sites'] = sites
            listings = {s['id']: s for s in sites}
        else:
            listings = {s['id']:s for s in sites}
            for site in timeline:
                listings.setdefault(site['id'], dict(site, reporting=False, newestTs=None, reason='not reporting'))
            with ThreadPoolExecutor(max_workers=RADAR_TILE_WORKERS, thread_name_prefix='radar-list') as pool:
                futures = [pool.submit(self._site_listing, ctx, site) for site in
                           sorted(listings.values(), key=lambda s:(s['distanceMeters'],s['id']))]
                for future in futures:
                    site, stamps, reused = future.result(timeout=max(0, deadline-time.monotonic()))
                    ctx['site_scans'][site] = stamps
                    ctx['reuse_newest'] = ctx.get('reuse_newest', False) or reused
        self._checkpoint(ctx)
        reporting = sorted((s for s in listings.values() if s['reporting'] and s['id'] in {t['id'] for t in timeline}), key=lambda s: (s['distanceMeters'], s['id']))
        if not reporting:
            ctx.setdefault('site_failure', 'scan unavailable' if any(s['reason']=='scan unavailable' for s in sites) else 'not reporting')
            raise ValueError('no site reporting in viewport')
        ctx['site_id'] = reporting[0]['id']
        if self._pass['source'] == 'iem-nexrad-n0b':
            self._pass['site'] = ctx['site_id']
        for site in sites:
            site.update(primary=site['id']==ctx['site_id'], contributing=site['reporting'],
                        reason=None if site['reporting'] else site['reason'])
        ctx['sources'][1] = dict(mode='site', siteId=ctx['site_id'], available=True, reason=None)
        return ctx['site_scans'][ctx['site_id']], deadline

    def _site_frames(self, ctx):
        source = 'iem-nexrad-n0b'
        now = time.time()
        stamps, deadline = ctx.pop('auto_discovered', None) or self._site_discover(ctx)
        ctx.update(_radar_scan_cadence(stamps), scan_latency_sec=self._site_latency(ctx['site_id'], now))
        self._discovery_unchanged(source, stamps[-1], ctx)
        def build(ts, limit, pairs=None):
            layers = []
            for site, scan in (_radar_site_pairs(ctx, ts) if pairs is None else pairs):
                stamp = datetime.fromtimestamp(scan, timezone.utc).strftime('%Y%m%d%H%M')
                def url(x, y, site=site, stamp=stamp):
                    return RADAR_SITE_TILE_TEMPLATE.format(site=site[1:], stamp=stamp,
                        z=ctx['zoom'], x=x, y=y)
                layers.append((site, scan, url))
            return self._fill_frame(source, ts, ctx, limit, None, layers=layers)
        primary_only = self._primary_only(ctx)
        if primary_only:
            ctx['frames_target'] = 1
        candidates = stamps[-1:] if primary_only else stamps[-3:] if (_radar_is_native(_radar_variant(ctx, source)) and
                                     ctx.get('native_ceiling') == 'newest-only') else stamps
        for ts in reversed(candidates):
            if now - ts >= RADAR_SITE_MAX_AGE_SEC:
                break
            ctx['candidates'].append(ts)
            slots = [t for t in stamps if ts - RADAR_HISTORY_SEC <= t <= ts][-(8 if _radar_is_native(_radar_variant(ctx, source)) or len(_radar_site_pairs(ctx, ts)) >= 2 else 31):]
            self._publish_refresh(ctx, frameTotal=len(slots) if ctx['viewed'] else 1)
            latest = build(ts, deadline)
            if ctx.get('level3_failed'):
                # Level III went down during this pass. Older products may be
                # cached, but drawing them would hide the newest scan that IEM
                # can draw now: end the pass on the outage path (v1 retry).
                raise ValueError('no complete site scan: Level III unavailable (' + ctx.get('last_error', '') + ')')
            if ctx.get('reuse_newest') and not latest.get('publishable',latest['complete']):
                raise _RadarRevalidate('remembered site scan unavailable')
            ctx['reuse_newest'] = False
            if latest.get('publishable',latest['complete']):
                cap = 8 if latest.get('mosaicKey') or len(latest.get('acquiredSites',latest.get('siteScans', ()))) >= 2 else 31
                slots = [t for t in stamps if ts - RADAR_HISTORY_SEC <= t <= ts][-cap:]
                self._publish_refresh(ctx, frameTotal=len(slots) if ctx['viewed'] else 1)
                return self._history(source, ts, stamps[-1], ctx, build, latest, slots)
        raise ValueError('no complete site scan: ' + ctx.get('last_error', 'unavailable'))

    def _rainviewer_frames(self, ctx):
        """Global past-frame adapter; never include nowcast or more than one hour."""
        source = 'rainviewer'
        known = self._known(source, ctx)
        if known is None:
            self._forget(source)
            manifest = json.loads(self._request(source, RADAR_RAINVIEWER_MANIFEST_URL,
                ctx['deadline'], metadata=True))
            host = manifest['host'].rstrip('/')
            if not host.startswith('https://'):
                raise ValueError('invalid RainViewer host')
            past = {int(f['time']): f['path'] for f in manifest['radar']['past']}
            newest = max(past)
            past = {t: p for t, p in past.items() if newest - RADAR_HISTORY_SEC <= t <= newest}
        else:
            newest, host, past = known['newest'], known['host'], known['past']
        if not 0 <= time.time() - newest <= RADAR_RAINVIEWER_STALE_SEC:
            if known is not None:
                raise _RadarRevalidate('remembered RainViewer scan aged out')
            raise ValueError('invalid or stale RainViewer manifest')
        self._discovery_unchanged(source, newest, ctx, dict(newest=newest, host=host, past=past))
        self._publish_refresh(ctx, frameTotal=len(past) if ctx['viewed'] else 1)
        def build(ts, limit):
            path = past[ts]
            if not isinstance(path, str) or not path.startswith('/'):
                raise ValueError('invalid RainViewer path')
            if known is None and ts == newest:
                self._newest[(source, None)] = (time.monotonic(), dict(newest=newest, host=host, past=past))
            return self._fill_frame(source, ts, ctx, limit,
                lambda x, y: f'{host}{path}/256/{ctx["zoom"]}/{x}/{y}/{RADAR_RAINVIEWER_COLOR}/{RADAR_RAINVIEWER_TILE_OPTS}.png')
        ctx['reuse_newest'] = known is not None
        latest = build(newest, ctx['deadline'])
        ctx['reuse_newest'] = False
        if known is not None and not latest.get('publishable',latest['complete']):
            raise _RadarRevalidate('remembered RainViewer scan unavailable')
        if not latest.get('publishable',latest['complete']):
            raise ValueError('no complete RainViewer latest')
        ctx['rainviewer_prefix'] = host + past[newest]
        return self._history(source, newest, newest, ctx, build, latest, sorted(past))

    def _history(self, source, newest, advertised, ctx, build, latest, slots=None):
        """Publish latest promptly, then atomically replace with bounded backfill."""
        ctx['tile_workers'] = RADAR_TILE_WORKERS
        settings = _RADAR_SOURCES[source]
        newest_only = source == 'iem-nexrad-n0b' and (self._primary_only(ctx) or
            _radar_is_native(_radar_variant(ctx, source)) and ctx.get('native_ceiling') == 'newest-only')
        slots = [newest] if newest_only else slots or list(range(newest - RADAR_HISTORY_SEC, newest + 1, settings['cadence']))
        # The loop this pass builds: every refresh and publication below says so.
        ctx['loop_target'] = 1 if newest_only else self._loop_target(dict(ctx, loop_target=None))
        if newest_only:
            self._publish_refresh(ctx, frameTotal=1)
        frames = {t: _radar_frame(source, t, ctx, _radar_site_pairs(ctx, t)
                  if source == 'iem-nexrad-n0b' else None) for t in slots}
        # Retain only frames whose requested measurements still match this
        # view. A watch frame must acquire neighbours before joining a full loop.
        retained = self._sliding_frames(source, newest, ctx)
        if not newest_only:
            frames.update(retained)
        frames[newest] = latest
        slots = sorted(frames)
        previous = ctx.get('previous_result',self._result)
        newest_reasons = dict(ctx.get('site_reasons', {}))
        fetched = time.time()
        # A provisional rebuild may already have replaced watch pixels at the
        # same timestamp. Reuse a previous loop only if its frames still match.
        if (newest < advertised and previous.available and previous.source_id == source
                and previous.site_id == (ctx.get('site_id') if source == 'iem-nexrad-n0b' else None) and previous.center == dict(lat=ctx['station'][0], lon=ctx['station'][1]) and previous.bounds == ctx['bounds']
                and previous.zoom == ctx['zoom'] and previous.ts_frame == newest
                and all(retained.get(f['ts']) == f for f in previous.frames)):
            ctx['retained_failed'] = True
            self._schedule_retry('radar', self._check, RADAR_RETRY_SEC)
            return previous
        if ctx.get('site_budget_limited'):
            self._budget_retry(source, len(ctx['tiles'])+2)
        elif newest < advertised:
            self._schedule_retry('radar', self._check, RADAR_RETRY_SEC)
        if newest < advertised and previous.source_id == source and previous.ts_frame == newest:
            fetched = previous.ts_fetch  # a failed newer frame is not a successful refresh
        def publish():
            self._checkpoint(ctx)
            # A retained frame can first become unfiltered during backfill,
            # even when the newest was fully classified before history began.
            if any(any(not p.get('filtered', True) and time.time()-p['volumeTs'] <= RADAR_N0H_UPGRADE_SEC
                       for p in f.get('siteScans', ())) or
                   any(time.time()-stamp <= RADAR_N0H_UPGRADE_SEC and
                       not any(p['id'] == site and p['ts'] == stamp for p in f.get('siteScans', ()))
                       for site, stamp in f.get('requestedPairs', ())) for f in frames.values()):
                self._schedule_retry('radar', self._check, 20)
            if source == 'iem-mrms-lcref' and time.time() - newest > settings['stale_sec']:
                return False
            if (previous.available and previous.source_id == source and previous.center == dict(lat=ctx['station'][0], lon=ctx['station'][1]) and previous.bounds == ctx['bounds']
                    and previous.zoom == ctx['zoom'] and previous.site_id == (ctx.get('site_id') if source == 'iem-nexrad-n0b' else None) and previous.ts_frame is not None and previous.ts_frame > newest):
                raise ValueError('source timestamp regressed')
            if (ctx.get('staging_source') and self._result.source_id != source
                    and not latest.get('publishable', latest['complete'])
                    and sum(f['complete'] for f in frames.values()) < min(4, target)):
                return True  # continue building behind the retained manifest
            self._note_source(source, ctx)
            snapshot = _RadarResult(True, None, tuple(dict(frames[t]) for t in sorted(frames)),
                newest, dict(lat=ctx['station'][0],lon=ctx['station'][1]), ctx['zoom'], ctx['mpp'], ctx['bounds'],
                ctx['bar'], ctx['rings'], ctx['nexrad'], fetched, source, **settings,
                zoom_desired=ctx['desired'], zoom_auto_level=ctx['auto_zoom'],
                geo=ctx.get('geo'), tiles=_radar_tile_manifest(source,[frames[t] for t in sorted(frames)],ctx), units=ctx['unit'], source_mode='site' if source == 'iem-nexrad-n0b' else 'mosaic',
                site_id=ctx.get('site_id') if source == 'iem-nexrad-n0b' else None,
                sites=tuple(dict(s, filtered=next((p.get('filtered') for p in latest.get('siteScans', ()) if p['id']==s['id']), None), contributing=any(p['id']==s['id'] for p in latest.get('siteScans', ())),
                    reason=None if any(p['id']==s['id'] for p in latest.get('siteScans', ())) else
                    (newest_reasons.get(s['id']) or s.get('reason') or 'scan unavailable')) for s in ctx.get('sites', ())), sites_considered=ctx.get('sites_considered', 0),
                sources=tuple(dict(s) for s in ctx.get('sources', ())),
                **({k:ctx.get(k) for k in ('scan_cadence_sec','scan_mode','scan_mode_source','scanning_slowly','scan_latency_sec')}
                   if source == 'iem-nexrad-n0b' else dict(scanning_slowly=False)),
                partial_coverage=_radar_partial_coverage(source, frames[newest], ctx))
            self._result_stamp = ctx.get('preference_stamp')
            self._publish_refresh(ctx, snapshot=snapshot,
                frameIndex=sum(frames[t]['complete'] for t in sorted(frames)[-target:]))
            return True
        # Disk tiles survive tab closure, camera moves and emitter restarts.
        for t in slots:
            f = frames[t]
            pairs = _radar_frame_pairs(f)
            f['complete'] = all(all(_radar_present(ctx,source,site,scan,ctx['zoom'],x,y)
                for x,y,_,_ in _radar_site_tiles(ctx,site)) for site,scan in pairs)
        limit = ctx.get('frames_target')  # the attention tier's loop size when active
        target = min(ctx['loop_target'], len(slots))
        def pending_work():
            count = sum(frames[t]['complete'] for t in slots[-target:])
            self._pending = dict(newest=not frames[newest]['complete'],
                four=count < min(4,target), eight=count < target,
                optional=count >= target and (limit is None or ctx.get('attention_knobs', {}).get('prefetch', False)))
        pending_work()
        starting_complete = sum(frames[t]['complete'] for t in slots[-target:])
        if not publish():
            raise ValueError('IEM frame aged out during acquisition')
        self._health.last_success = time.time()
        ctx['deadline'] = ctx.get('pass_deadline', ctx['deadline'])
        self._session.begin_pass(ctx['deadline'])
        self._idle_context = (source, {k: v for k, v in ctx.items() if k != 'listing_results'})
        if not latest['complete']:
            if self._failed_pass(source, TimeoutError('visible newest incomplete'), ctx):
                raise TimeoutError('visible newest objective failed three passes')
            self._pending.update(newest=True, four=True, eight=True)
            ctx['retained_failed'] = True
            self._budget_retry(source, len(ctx['tiles'])+2, reason=self._retry_reason or 'provider')
            return self._result
        if target > 1 or ctx['viewed'] and limit is None or ctx.get('staging_source'):
            self._publish_refresh(ctx,state='idle')
            ctx['tiles'] = _radar_grid(ctx)
            ordered = [newest] if newest_only else list(reversed(slots))
            if limit is not None:
                if not (ctx['viewed'] and ctx.get('attention_knobs', {}).get('prefetch')):
                    ordered = ordered[:target]
            elif not ctx['viewed']:
                ordered = ordered[:4]
            needed = 0
            view_delay = 0
            deferred = False
            retry_reason = 'budget'
            retry = self._session.on_retry
            # The callback also charges transparent retries at the current tier's
            # floor. HEAD probes and tile workers use the same atomic gate.
            self._session.on_retry = lambda end, first_byte=False: self._transport_retry(
                source, end, ctx.get('request_reserve', 0), first_byte=first_byte)
            try:
                for deep, tier in ((False, ordered[:RADAR_LOOP_FRAMES]),
                                   (True, ordered[RADAR_LOOP_FRAMES:])):
                    if deep:
                        if self._attention_active() and not self._attention_knobs()['prefetch']:
                            break
                        if deferred or any(not frames[t]['complete'] for t in ordered[:RADAR_LOOP_FRAMES]):
                            break
                        self._publish_refresh(ctx, state='idle')
                        visible_tiles = ctx['tiles']
                        ctx.update(tiles=_radar_grid(ctx, margin=1), prefetch=True,
                                   request_reserve=self._mandatory_reserve(source, ctx, [newest]))
                        mandatory_builds = ctx['builds']
                        try:
                            build(newest, ctx['deadline'])
                        finally:
                            ctx['builds'] = mandatory_builds
                            ctx['tiles'] = visible_tiles
                            ctx.pop('prefetch', None)
                            ctx.pop('request_reserve', None)
                        self._prefetch(source,ctx)
                        self._pending['optional'] = False
                    reserve = self._mandatory_reserve(source, ctx, slots[-4:]) if deep else 0
                    if deep:
                        reserve += RADAR_PREFETCH_HEADROOM
                    ctx['request_reserve'] = reserve
                    for t in tier:
                        self._checkpoint(ctx)
                        if frames[t]['complete'] and not self._hca_due(frames[t]):
                            continue
                        # Repair all requested measurements, including a missing
                        # neighbour, rather than freezing the contributors alone.
                        pairs = frames[t].get('requestedPairs', [(p['id'],p['ts']) for p in frames[t]['siteScans']]) if source == 'iem-nexrad-n0b' else [(None,t)]
                        cost = self._frame_request_cost(source, ctx, pairs, frame_ts=t)
                        cost += source == 'iem-mrms-lcref'
                        needed = cost + reserve
                        if deep:
                            view_delay = self._deep_view_delay(ctx)
                            if view_delay != 0:
                                deferred = True
                                break
                            ctx['deep_history'] = True
                        if self._headroom_delay(source, needed):
                            deferred = True
                            break
                        # Loop and deep history must not evict the newest
                        # native tiles just warmed for the next press. Touch
                        # these entries before each bounded frame build.
                        stamps = (set((p['id'], p['ts']) for p in latest.get('siteScans', ()))
                                  if source == 'iem-nexrad-n0b' else {(None, newest)})
                        protected = {(source, site, stamp) for site, stamp in stamps}
                        protected.update((key[0], site, stamp)
                            for key, signature in self._prefetched.items()
                            if key[2:] == (ctx['center']['lat'], ctx['center']['lon'])
                            for site, stamp in signature)
                        with self._lock:
                            for group in protected:
                                for key in self._native_groups.get(group, ()):
                                    if key in self._tiles:
                                        self._tiles.move_to_end(key)
                        self._publish_refresh(ctx, state='history', frameTotal=target)
                        try:
                            frame = build(t, ctx['deadline'], pairs=pairs) if source == 'iem-nexrad-n0b' else build(t, ctx['deadline'])
                        except (TimeoutError, _RadarBudget) as error:
                            retry_reason = 'deadline' if isinstance(error, TimeoutError) else 'budget'
                            deferred = True
                            self._note_yield(error)
                            break
                        if frame['complete']:
                            frames[t] = frame
                            pending_work()
                            if not publish():
                                break
            except (TimeoutError, _RadarBudget) as error:
                retry_reason = 'deadline' if isinstance(error, TimeoutError) else 'budget'
                deferred = True
                self._note_yield(error)
                view_delay = self._deep_view_delay(ctx) if ctx.get('deep_history') else 0
            finally:
                ctx.pop('deep_history', None)
                ctx.pop('request_reserve', None)
                self._session.on_retry = retry
            if self._attention_active() and not self._attention_knobs()['prefetch']:
                # Optional demand may disappear after `ordered` was built.
                # Finish/retry the visible target only, not abandoned warming.
                ordered = ordered[:target]
                ctx['attention_knobs'] = self._attention_knobs()
                pending_work()
            if any(not frames[t]['complete'] for t in ordered):
                if deferred and retry_reason == 'budget':
                    # The gate refused before this frame's cost was priced, so
                    # `needed` can still be 0: a retry asking for no headroom fires
                    # 2 s later into the same full window, one probe per pass, for
                    # as long as the window stays full (2026-09-16 zoom-out loop).
                    # Ask for one frame's tiles so the retry waits for real room.
                    needed = max(needed, len(ctx['tiles'])+2)
                if deferred and view_delay is not None:
                    self._budget_retry(source, needed, min_delay=view_delay, reason=retry_reason)
                else:
                    self._budget_retry(source, max(1, needed))
        completed = sum(frames[t]['complete'] for t in slots[-target:])
        if completed < min(4,target) and completed <= starting_complete and ctx.get('missing_tiles'):
            ctx['retained_failed'] = True
            if self._failed_pass(source, TimeoutError('four-frame objective made no progress'), ctx):
                raise TimeoutError('four-frame objective failed three passes')
        if ctx.get('staging_source') and self._result.source_id != source:
            ctx['retained_failed'] = True
            self._budget_retry(source, len(ctx['tiles'])+2)
        return self._result

    def _frame_request_cost(self, source, ctx, pairs, frame_ts=None, priced=None):
        """Price network acquisitions, not the number of generated PNGs."""
        from lib.radar_level3 import match_key
        native = _radar_is_native(_radar_variant(ctx, source))
        scans, listings = priced if priced is not None else (set(), set())
        initial = len(scans) + len(listings)
        count = 0
        if native and pairs:
            frame_ts = frame_ts if frame_ts is not None else next(
                (stamp for site, stamp in pairs if site == ctx.get('site_id')), max(t for _, t in pairs))
            metadata = self._mosaic_cached(pairs, frame_ts, ctx)
            if metadata is not None and not self._hca_due(metadata):
                return 0
        n0h_probe = native and self._n0h_health.probe_delay({RADAR_N0H_TRANSPORT}) == 0
        for site, stamp in pairs:
            missing = 1 if native else sum(not _radar_present(ctx, source, site, stamp, ctx['zoom'], x, y)
                          for x, y, _, _ in _radar_site_tiles(ctx, site))
            if not native:
                count += missing
                continue
            if not missing:
                continue
            text = _radar_stamp_text(stamp)
            ts = datetime.strptime(text, '%Y%m%d%H%M').replace(tzinfo=timezone.utc).timestamp()
            with self._lock:
                reflectivity = self._level3_scans.get((site, ts))
                for product in ('N0B', 'N0H'):
                    key = ((site, ts) if product == 'N0B' else
                           (site, reflectivity.volume_ts if reflectivity else ts, product))
                    if key in self._level3_scans or key in scans:
                        continue
                    failed = self._level3_failed.get(key)
                    if failed and (time.monotonic() < failed[0] or product == 'N0H'
                                   and time.time() - key[1] > RADAR_N0H_UPGRADE_SEC):
                        continue
                    scans.add(key)
                    prefix = '%s_%s_%s' % (site[1:], product, datetime.fromtimestamp(ts, timezone.utc).strftime('%Y_%m_%d_%H'))
                    listed = self._level3_listings.get((site, prefix))
                    if (product == 'N0H' and n0h_probe or listed is None
                            or match_key(listed[1], ts, product) is None):
                        listings.add((site, prefix))
        return count + len(scans) + len(listings) - initial

    def _mandatory_reserve(self, source, ctx, stamps):
        tiles = _radar_grid(ctx)
        count = 0
        def required(stamp):
            if source != 'iem-nexrad-n0b':
                return [(None,stamp)]
            if 'site_scans' in ctx:
                return _radar_site_pairs(ctx,stamp)
            return [(site['id'],site.get('newestTs') or stamp) for site in ctx.get('sites',()) if site.get('reporting')]
        if _radar_is_native(_radar_variant(ctx, source)):
            priced = (set(), set())
            count = sum(self._frame_request_cost(source, dict(ctx, tiles=tiles), required(stamp),
                        frame_ts=stamp, priced=priced) for stamp in stamps)
            layers = max(1, len(required(stamps[-1]))) if stamps else 1
            # A cold newest needs two hourly listings and two products per site;
            # also reserve IEM discovery for each site.
            return min(RADAR_REQUESTS_PER_MIN-1, max(count, 4*layers)+layers)
        for stamp in stamps:
            pairs = required(stamp)
            for site, scan in pairs:
                for x,y,_,_ in _radar_site_tiles(dict(ctx, tiles=tiles), site):
                    count += not _radar_present(dict(ctx, inventory=self._disk_inventory, manifest_cache=self._manifest_cache), source, site, scan, ctx['zoom'], x, y)
        # Preserve a cold newest at this footprint even when the current loop
        # is already cached; price every required site layer.
        layers = len(required(stamps[-1])) if stamps else 1
        return min(RADAR_REQUESTS_PER_MIN-1, max(count, len(tiles)*max(1,layers))+2)

    def _prefetch(self, source, ctx):
        """Idle source/zoom rounds before history; newest native and disk tiles."""
        if self._attention_active() and not self._attention_knobs()['prefetch']:
            return
        if (not ctx['viewed'] or not self._is_viewed() or
                ctx.get('refresh', {}).get('state') != 'idle'):
            return
        known_ctx = dict(ctx, intent_triggered=True)
        newest = self._result.ts_frame
        known = self._known(source, known_ctx) if source != 'iem-nexrad-n0b' else None
        own_fresh = source == 'iem-nexrad-n0b' or known is not None and known['newest'] == newest
        floor = RADAR_SITE_MIN_ZOOM if source == 'iem-nexrad-n0b' else RADAR_MIN_ZOOM
        targets = [(source, z) for z in (ctx['zoom']-1, ctx['zoom']+1)
                   if floor <= z <= _RADAR_SOURCES[source]['max_zoom']]
        if source == 'iem-nexrad-n0b' and ctx['camera_zoom'] <= 7:
            if ctx['zoom'] <= _RADAR_SOURCES['iem-mrms-lcref']['max_zoom']:
                targets.insert(0, ('iem-mrms-lcref', ctx['zoom']))
            targets.extend(('iem-mrms-lcref', z) for z in (ctx['zoom']-1, ctx['zoom']+1)
                           if RADAR_MIN_ZOOM <= z <= _RADAR_SOURCES['iem-mrms-lcref']['max_zoom'])
        elif (source == 'iem-mrms-lcref' and ctx['sources'][1]['available']
              and ctx['zoom'] >= RADAR_SITE_MIN_ZOOM):
            # Same-centre mode tap first; also cover a mode+zoom press (z8 -> site z7).
            targets.extend(('iem-nexrad-n0b', z) for z in
                           (ctx['zoom'], ctx['zoom']-1, ctx['zoom']+1)
                           if RADAR_SITE_MIN_ZOOM <= z <= 10)
        # The current camera's opposite mode comes before optional zoom neighbours.
        targets.sort(key=lambda item: item[0] == source or item[1] != ctx['zoom'])
        targets = [(target,z) for target,z in targets if target != source or own_fresh]
        reserve = self._mandatory_reserve(source, ctx, [newest])
        retry = self._session.on_retry
        admitted_sources = set()
        denied_sources = set()
        try:
            for target, zoom in targets:
                if target in denied_sources:
                    continue
                warm = dict(ctx, intent_triggered=True, prefetch=True, target_source=target, sources=list(ctx['sources']),
                            request_reserve=reserve, tile_workers=RADAR_TILE_WORKERS)
                self._checkpoint(warm)
                if target not in admitted_sources:
                    # As with the original Z±1 tier, admit a source round once
                    # with 60 spare slots; every request still preserves 34.
                    if self._headroom_delay(target, RADAR_PREFETCH_HEADROOM + reserve, warm):
                        self._budget_retry(target, RADAR_PREFETCH_HEADROOM + reserve)
                        # A source cooldown must not block the other source.
                        # The shared request cap and reserve still apply to both.
                        denied_sources.add(target)
                        continue
                    admitted_sources.add(target)
                tiles, _, bounds, _ = _radar_viewport(ctx['center']['lat'], ctx['center']['lon'],
                    zoom, RADAR_VIEWPORT_W, RADAR_VIEWPORT_H)
                if target == source:
                    cx,cy = world_point(ctx['center']['lat'],ctx['center']['lon'],zoom)
                    tiles = [(x,y,0,0) for y in (int(cy//256)-1,int(cy//256))
                             for x in (int(cx//256)-1,int(cx//256)) if 0<=x<2**zoom and 0<=y<2**zoom]
                warm.update(zoom=zoom, camera_zoom=zoom, tiles=tiles, bounds=bounds)
                if target != source and zoom == ctx['zoom']:
                    warm['tiles'] = _radar_grid(warm,margin=1)
                self._session.on_retry = lambda end, first_byte=False: self._transport_retry(
                    target, end, reserve, first_byte=first_byte)
                try:
                    if target == 'iem-nexrad-n0b':
                        # The daily ceiling bounds optional warming separately
                        # from attention; being unviewed alone is no restriction.
                        stamps, _ = self._site_discover(warm,
                            primary_only=warm.get('native_ceiling') == 'newest-only')
                        pairs = _radar_site_pairs(warm, stamps[-1])
                        # Empty listings participate in the round identity too.
                        signature = tuple(sorted(set(pairs) |
                            {(site, scans[-1] if scans else None)
                             for site, scans in warm['site_scans'].items()}, key=repr))
                    elif target == 'iem-mrms-lcref':
                        stamp, known, deadline = self._iem_scan(warm)
                        if known is None:
                            utc = datetime.fromtimestamp(stamp, timezone.utc)
                            self._archive_probe(target, utc.strftime(RADAR_IEM_ARCHIVE_TEMPLATE),
                                                      deadline, reserve)
                            self._newest[(target, None)] = (time.monotonic(), dict(newest=stamp))
                        pairs = ((None, stamp),)
                        signature = pairs
                    else:
                        pairs = ((None, newest),)
                        signature = pairs
                    if _radar_is_native(_radar_variant(warm, target)):
                        frame_ts = stamps[-1]
                        input_pairs = pairs
                        metadata = self._mosaic_cached(pairs, frame_ts, warm)
                        scans = ()
                        if metadata is None or self._hca_due(metadata):
                            metadata, scans = self._mosaic_inputs(pairs, frame_ts, warm, ctx['deadline'])
                        if not metadata['siteScans']:
                            continue
                        warm['mosaic_scans'] = scans
                        warm['mosaic_filtered'] = tuple(p['filtered'] for p in metadata['siteScans'])
                        pairs = ((metadata['mosaicKey'], frame_ts),)
                        signature = pairs
                    key = (target, zoom, ctx['center']['lat'], ctx['center']['lon'])
                    if (self._prefetched.get(key) == signature and
                            all(_radar_present(dict(warm, inventory=self._disk_inventory, manifest_cache=self._manifest_cache), target, site, stamp, zoom, x, y)
                                for site, stamp in pairs
                                for x, y, _, _ in _radar_site_tiles(warm, site))):
                        continue
                    # Bounded completion memory per geometry and scan set. Successful
                    # in-flight tiles survive cancellation in the ordinary tile LRU.
                    if len(self._prefetched) >= RADAR_TILE_CACHE_SIZE:
                        self._prefetched.pop(next(iter(self._prefetched)))
                    # Record completion only after every tile succeeds. An intent
                    # interrupt must not turn a partial round into a permanent hit.
                    self._prefetched.pop(key, None)
                    for site, stamp in pairs:
                        utc = datetime.fromtimestamp(stamp, timezone.utc).strftime('%Y%m%d%H%M')
                        if target == 'iem-mrms-lcref':
                            url = lambda x, y: RADAR_IEM_TILE_TEMPLATE.format(stamp=utc, z=zoom, x=x, y=y)
                        elif target == 'iem-nexrad-n0b':
                            url = lambda x, y: RADAR_SITE_TILE_TEMPLATE.format(site=site[1:], stamp=utc, z=zoom, x=x, y=y)
                        else:
                            url = lambda x, y: (f'{ctx["rainviewer_prefix"]}/256/{zoom}/{x}/{y}/'
                                f'{RADAR_RAINVIEWER_COLOR}/{RADAR_RAINVIEWER_TILE_OPTS}.png')
                        try:
                            for _ in self._tile_batch(target, stamp, warm, ctx['deadline'], url, site):
                                pass
                        except (_RadarBudget, _RadarSuperseded):
                            raise
                        except Exception:
                            self._forget(target, site)
                            raise
                    if not all(_radar_present(warm, target, site, stamp, zoom, x, y)
                               for site, stamp in pairs for x, y, _, _ in _radar_site_tiles(warm, site)):
                        continue
                    if _radar_is_native(_radar_variant(warm, target)):
                        from lib.radar_mosaic import write_frame_metadata
                        path = _radar_tile_path(target, metadata['mosaicKey'], frame_ts,
                            zoom, 0, 0, _radar_variant(warm, target)).parents[2] / 'frame.json'
                        write_frame_metadata(path, _radar_stamp_text(frame_ts), input_pairs,
                                             metadata, _radar_render_revision(_radar_variant(warm, target)), self._disk_inventory.frame_metadata)
                    self._prefetched[key] = signature
                except (_RadarBudget, _RadarSuperseded):
                    raise
                except Exception:
                    # Optional warming cannot invalidate the foreground scan.
                    continue
        except _RadarSuperseded:
            raise
        except _RadarBudget:
            pass
        finally:
            self._session.on_retry = retry

    def _prune(self, previous=None, incoming_size=0, incoming_files=0):
        """Pin the displayed and retained visible loops; evict other served LRU."""
        cache = self._disk_inventory
        if len(cache)+incoming_files <= cache.MAX_FILES and cache.bytes+incoming_size <= cache.MAX_BYTES:
            return
        pinned = set()
        for snap in (self._result, previous):
            if snap is None or not snap.tiles:
                continue
            grid = snap.tiles.get('grid', {})
            for frame in snap.frames[-8:]:
                for tile_site, tile_stamp in _radar_frame_pairs(frame):
                    for y in range(grid.get('y0',0), grid.get('y0',0)+grid.get('h',0)):
                        for x in range(grid.get('x0',0), grid.get('x0',0)+grid.get('w',0)):
                            pinned.add(_radar_disk_key(snap.source_id,tile_site,tile_stamp,snap.zoom,x,y,snap.tiles.get('variant',False)))
        cache.evict(pinned, incoming_size, incoming_files)
        self._disk_files=len(cache);self._disk_bytes=cache.bytes

    def _invalidate_tile(self, key):
        """Explicit eviction/repair notification, also used by local cache tools."""
        with self._lock:
            record = self._disk_inventory.discard(key)
            if record is not None:
                record[0].unlink(missing_ok=True)
                if (key[1] or '').startswith('M') and not any(
                        group[:3] == key[:3] for group in self._disk_inventory.group_counts):
                    (record[0].parents[2] / 'frame.json').unlink(missing_ok=True)
                    self._disk_inventory.frame_metadata.discard(record[0].parents[2] / 'frame.json')
                self._disk_files=len(self._disk_inventory)
                self._disk_bytes=self._disk_inventory.bytes
                self._pending.update(newest=True, four=True, eight=True)
                self._acquisition_pending = True
            return record is not None

    def _consume_bad_tiles(self):
        if not self._cache_ready.is_set():
            return
        marker = Path(self.output_path).with_name('radar_bad_tiles')
        try:
            stat = marker.stat()
            stamp = (stat.st_ino,stat.st_mtime_ns,stat.st_size)
            if stamp == self._bad_stamp:
                return
            self._bad_stamp = stamp
            if stat.st_size > 32768:
                return
            paths = json.loads(marker.read_text())
            if not isinstance(paths, list):
                return
            for relative in paths[:128]:
                if not isinstance(relative,str):
                    continue
                parts = relative.split('/')
                revisions = {_radar_render_revision(v): v for v in RADAR_RENDER_VARIANTS}
                if len(parts) != 9 or parts[:2] != ['radar','t'] or parts[2] not in revisions:
                    continue
                _,_,_,source,site,scan,z,x,y = parts
                try:
                    key = _radar_disk_key(source,None if site=='-' else site,scan,int(z),int(x),int(y[:-4]),revisions[parts[2]])
                    with self._lock:
                        record = self._disk_inventory.records.get(key)
                        if record is None:
                            continue
                        try:
                            _radar_tile_metadata(record[0],source)
                        except (OSError, ValueError, KeyError, TypeError):
                            self._invalidate_tile(key)
                except ValueError:
                    continue
        except (OSError, ValueError, TypeError):
            pass

    def _start_inventory(self):
        """Boot the tile cache: publish revision markers, then scan and
        reconcile the tile tree. Ready only when both succeed. A failure (disk
        full, transient storage trouble) is not ready: the server refuses tiles
        without their revision markers, and files outside the inventory escape
        eviction. It is recorded for /health and retried with bounded backoff;
        each retry rescans from an empty inventory, so admission resumes only
        on a reconciled cache. Acquisition stays parked until then."""
        with self._lock:
            if self._cache_thread is not None or self._cache_ready.is_set():
                return
            failure = self._cache_error
            if failure is not None and time.monotonic() < failure['retryMono']:
                return
            root = Path(RADAR_DIR)
            self._cache_done.clear()
            def bootstrap():
                error = None
                try:
                    self._migrate_cache(str(root))
                    self._disk_inventory.scan_roots(tuple(
                        (root/'t'/_radar_render_revision(v), (v,) if v else ())
                        for v in RADAR_RENDER_VARIANTS), _radar_tile_metadata,
                        expire_before=_radar_stamp_text(int(time.time()-RADAR_CACHE_RETENTION_SEC)))
                except Exception as caught:                          # noqa: BLE001
                    error = caught
                deferred, delay = False, None
                with self._lock:
                    self._disk_files=len(self._disk_inventory)
                    self._disk_bytes=self._disk_inventory.bytes
                    if error is None:
                        self._cache_error = None
                        self._cache_ready.set()
                        deferred, self._cache_deferred = self._cache_deferred, False
                    else:
                        attempts = (self._cache_error or {}).get('attempts', 0) + 1
                        delay = min(RADAR_CACHE_RETRY_MAX_SEC, RADAR_CACHE_RETRY_SEC * 2**(attempts-1))
                        self._cache_error = dict(error=f'{type(error).__name__}: {error}'[:200],
                            attempts=attempts, ts=time.time(), retryTs=time.time()+delay,
                            retryMono=time.monotonic()+delay)
                    self._cache_thread = None
                    self._cache_done.set()
                # Scheduling takes _life_lock, which orders before _radar_lock.
                if error is not None:
                    self._logger.warning(f'almanac_emit: radar inventory startup failed - {error}; '
                                   f'retry in {delay:g} s')
                    self._runtime.schedule(lambda _dt: self._start_inventory(), delay)
                elif deferred:
                    self._runtime.schedule(self._check, 0)  # the pass that found the cache scanning
            self._cache_thread = _InventoryThread(target=bootstrap, name='radar-inventory', daemon=True)
            self._cache_thread.start()

    def _migrate_cache(self, radar_dir=None):
        import shutil
        from lib.radar_basemap import publish_revision,remove_empty_parents
        root=Path(radar_dir or RADAR_DIR);root.mkdir(parents=True,exist_ok=True)
        # The installed tile tree is owned storage, never an external link.
        if (root/'t').is_symlink():
            (root/'t').unlink()
        site_revision=_radar_sites_revision();site_path=root/('sites-'+site_revision+'.json')
        marker=root/'.sites-revision'
        if not marker.exists() or marker.read_text()!=site_revision or not site_path.is_file():
            sites=[dict(id=i,lat=a,lon=b,name=n) for i,(a,b,n) in _NEXRAD_SITES.items()]
            temp=site_path.with_suffix('.tmp');temp.write_text(json.dumps(sites,separators=(',',':')));os.replace(temp,site_path)
            marker.write_text(site_revision)
            for obsolete in root.glob('sites-*.json'):
                if obsolete!=site_path:obsolete.unlink()
            (root/'sites.json').unlink(missing_ok=True)
        smooth_revision = _radar_render_revision(True)
        current = {_radar_render_revision(v) for v in RADAR_RENDER_VARIANTS}
        for obsolete in (root/'t').glob('*'):
            if (obsolete.name not in current
                    and obsolete.is_dir() and not obsolete.is_symlink()):
                shutil.rmtree(obsolete)
        (root/'.smooth-revision').write_text(smooth_revision)
        (root/'.native-revision').write_text(_radar_render_revision('native'))
        (root/'.native-smooth-revision').write_text(_radar_render_revision('native-smooth'))
        revision=root/'.tile-revision'
        if revision.exists() and revision.read_text()==_radar_render_revision():return
        for name in ('basemap','t',*{s['legend']['id'] for s in _RADAR_SOURCES.values()}):
            path=root/name
            if path.is_dir() and not path.is_symlink():shutil.rmtree(path)
        for obsolete in (root/'geo').glob('*/*/*/*/*.bin'):
            obsolete.unlink(missing_ok=True);remove_empty_parents(obsolete,root/'geo')
        revision.write_text(_radar_render_revision())
        publish_revision(str(root))

    def _failed_pass(self, source, error, ctx):
        """Only consecutive provider failures may advance the fallback chain."""
        if isinstance(error, _RadarScanUnpublished) and self._primary_only(ctx):
            # Lag runs from the first unpublished scan of the current streak,
            # never from the newest advertised scan: IEM renews that every
            # few minutes, so a stalled S3 feed would never reach the bound.
            site, stamp = ctx.get('site_id'), ctx['unpublished_stamp']
            now = time.time()
            with self._lock:
                stall = self._level3_stall
                # A streak is continuous evidence: watch sees a late scan about
                # once a minute. One not observed for the bound is abandoned
                # (the tier left watch, Auto chose Region), and extending it
                # would turn a routine delay hours later into a false stall.
                if (stall is None or stall['site'] != site
                        or now - stall.get('seen', now) > RADAR_LEVEL3_UNPUBLISHED_LOG_SEC):
                    stall = self._level3_stall = dict(site=site, since=stamp, loggedAt=None, suppressed=0)
                stall['since'] = since = min(stall['since'], stamp)
                stall['seen'] = now
            if time.time() - since > RADAR_LEVEL3_UNPUBLISHED_LOG_SEC:
                # Level III is unavailable for the site view. IEM's route is
                # healthy: no strike, no local backoff, v1 retry now.
                self._pass['outcome'] = 'failed'
                self._pass['error'] = f'level3-stalled: {type(error).__name__}: {error}'
                self._level3_stalled(site, since)
                self._local_failure_streak = 0
                self._retained_refresh('failed')
                self._budget_retry(source, 1, min_delay=2, reason='provider')
                return False
            # IEM discovery precedes S3 publication. Preserve the last pixels;
            # no older download, provider strike, fallback or pass warning.
            self._pass['outcome'] = 'unpublished'
            self._unpublished_until = ctx['unpublished_until']
            self._clear_retry()
            self._schedule_retry('radar', self._check,
                max(1, ctx['unpublished_until']-time.monotonic()), retry_reason='provider')
            self._retained_refresh('idle')
            return False
        self._pass["outcome"] = "failed"
        self._log_failure(source, error)
        if ctx.get('level3_failed'):
            # A failure on the Level III host says nothing about IEM's route.
            # Retry on v1 soon without resetting or advancing IEM's failures.
            self._local_failure_streak = 0
            self._retained_refresh('failed')
            self._budget_retry(source, 1, min_delay=2, reason='provider')
            return False
        outcome, health = failure_class(error), self._health
        failures = health.failure_counts(source)
        # The pass error is often a synthetic TimeoutError ("visible newest
        # incomplete"); the tile loop's per-class flags and the health counters
        # say what actually failed underneath it.
        truly_local = (outcome == 'local' or ctx.get('local_failure')
                       or failures['local'] > ctx.get('local_failure_start', failures['local']))
        ambiguous = (outcome == 'ambiguous' or ctx.get('ambiguous_failure')
                     or failures['ambiguous'] > ctx.get('ambiguous_failure_start', failures['ambiguous']))
        local = truly_local or ambiguous  # neither may advance the fallback chain
        if local:
            self._transport_failures.pop(source, None)
        else:
            self._transport_failures[source] = self._transport_failures.get(source, 0)+1
        # A dead route or resolver never opens a host breaker (HostHealth.record
        # returns before sampling), so without its own backoff a network outage
        # would rerun a doomed pass every two seconds for as long as it lasts.
        # The streak feeds _radar_local_backoff, the floor under EVERY scheduled
        # radar retry. Ambiguous failures (a reused socket that got no bytes) may
        # be the provider stalling, so they end the streak and keep 2 s.
        self._local_failure_streak = self._local_failure_streak+1 if truly_local else 0
        if not local and self._transport_failures[source] >= 3:
            return True
        self._retained_refresh('failed')
        probe = self._health.probe_delay(set(self._transport_sources(source, ctx))) or 0
        self._budget_retry(source, 1, min_delay=max(2, probe),
            reason='deadline' if isinstance(error, TimeoutError) and not local else 'local' if local else 'provider')
        return False

    def _auto_coverage(self, bounds, sites):
        # Geometry changes only with the camera or the reporting set, not scans.
        key = (tuple(sorted(bounds.items())), tuple(sorted((s['lat'], s['lon']) for s in sites)))
        if key not in self._coverage_cache:
            self._coverage_cache[key] = radar_auto.coverage_fraction(bounds, sites, RADAR_SITE_RANGE_METERS)
            if len(self._coverage_cache) > 16:
                self._coverage_cache.popitem(last=False)
        return self._coverage_cache[key]

    def _auto_source(self, ctx, site_ok):
        zoom = ctx['desired'] if ctx['desired'] is not None else ctx['auto_zoom']
        previous = ctx['previous_result']
        showing = previous.source_mode if previous.frames else None
        # Watch discovery deliberately omits neighbours. It cannot supply a
        # new coverage verdict; retain the last mode Auto selected until
        # attended. A Region the fallback chain forced (Site strikes) or that
        # failed Site evidence chose is not a verdict: re-evaluate Site.
        if self._primary_only(ctx) and showing is not None and showing == self._auto_chosen:
            ctx['auto_choice'] = showing
            return showing
        available, coverage = (None if site_ok else False), 0.
        def availability(site):
            evidence = self._site_status.get(site['id'], site) if site else {}
            return radar_auto.listing_availability(evidence, time.time(),
                _RADAR_SOURCES['iem-nexrad-n0b']['cadence'], RADAR_SITE_MAX_AGE_SEC)
        evidence = self._site_status.get(ctx['nexrad']['id'], {}) if ctx['nexrad'] else {}
        refused = (evidence.get('reporting') is False and evidence.get('reason') == 'not reporting'
                   and evidence.get('checkedTs') is not None
                   and 0 <= time.time()-evidence['checkedTs'] < _RADAR_SOURCES['iem-nexrad-n0b']['cadence'])
        if refused:
            available = False
        coverage_zoom = max(zoom, radar_auto.UP_ZOOM)
        key = (ctx['center']['lat'], ctx['center']['lon'], coverage_zoom)
        self._auto_evidence = {k: v for k, v in self._auto_evidence.items()
                                    if 0 <= time.time()-v['at'] < RADAR_SITE_MAX_AGE_SEC}
        # Below the upward threshold a cold/wide Region needs no extra listings.
        if site_ok and not refused and (zoom >= radar_auto.UP_ZOOM or showing == 'site' and zoom > radar_auto.DOWN_ZOOM):
            _, _, bounds, _ = _radar_viewport(ctx['center']['lat'], ctx['center']['lon'], zoom,
                                             RADAR_VIEWPORT_W, RADAR_VIEWPORT_H)
            _, _, coverage_bounds, _ = _radar_viewport(ctx['center']['lat'], ctx['center']['lon'], coverage_zoom,
                                                      RADAR_VIEWPORT_W, RADAR_VIEWPORT_H)
            ctx.update(bounds=bounds, zoom=zoom, camera_zoom=zoom, target_source='iem-nexrad-n0b', auto_listing_cache=True)
            if self._session is None or self._provider != 'iem':
                if self._session is not None:
                    self._session.close()
                self._session = RadarSession()
                self._provider = 'iem'
            self._session.begin_pass(ctx['deadline'])
            self._session.on_retry = lambda end, first_byte=False: self._transport_retry(
                'iem-nexrad-n0b', end, first_byte=first_byte)
            try:
                ctx['auto_discovered'] = self._site_discover(ctx)
                nearest_available = availability(ctx['nexrad'])
                coverage_sites = _radar_sites(ctx['station'], bounds)[0] if self._primary_only(ctx) else ctx['sites']
                reporting = [s for s in coverage_sites if availability(s) is True]
                missing = [s for s in coverage_sites if availability(s) is None]
                coverage = self._auto_coverage(coverage_bounds, reporting)
                threshold = radar_auto.STAY_COVERAGE if showing == 'site' else radar_auto.MIN_COVERAGE
                if nearest_available is False:
                    available = False
                elif nearest_available is True:
                    # Unknown neighbours matter only if they could change the
                    # coverage verdict. A redundant failed site cannot veto entry.
                    available = True
                    if (coverage < threshold and missing and
                            self._auto_coverage(coverage_bounds, reporting+missing) >= threshold):
                        available = None
                if available is not None:
                    self._auto_evidence[key] = dict(at=time.time(), available=available, coverage=coverage)
            except _RadarSuperseded:
                raise
            except (ValueError, TimeoutError, _RadarBudget, CircuitOpen):
                if ctx.get('site_failure') == 'out of view':
                    available, coverage = True, 0.  # known geometry, not a failed listing
                if availability(ctx['nexrad']) is False:
                    available = False
        elif site_ok and not refused:
            # Zoom alone can select Region without collecting new Site evidence.
            available, coverage = True, 1.
        # Unknown acquisition is a bounded hold, never evidence of fresh scans.
        if available is None and showing == 'site' and (
                previous.ts_frame is None or not 0 <= time.time()-previous.ts_frame < RADAR_SITE_MAX_AGE_SEC
                or self._transport_failures.get('iem-nexrad-n0b', 0) >= 3):
            available = False
        ctx['auto_evidence'] = self._auto_evidence.get(key)
        last = self._auto_switch
        selected = radar_auto.choose(zoom, showing, available, coverage,
                                     time.monotonic()-last[0] if last else None,
                                     zoom-last[1] if last else 0)
        wanted = radar_auto.choose(zoom, showing, available, coverage)
        self._auto_due = last[0]+radar_auto.SWITCH_GUARD_SEC if selected != wanted and last else None
        ctx['auto_choice'] = selected if available is True else None
        return selected

    def _note_auto_choice(self, ctx, source):
        # A chain-forced mode is never Auto's verdict, so it is never
        # inherited by a later unattended Auto watch.
        mode = 'site' if source == 'iem-nexrad-n0b' else 'mosaic'
        self._auto_chosen = mode if ctx.get('auto_choice') == mode else None

    def _acquire(self, intent_triggered=None, view_started=False, discovery=False):
        """Primary-first orchestration; radar failures never alter engine health."""
        self._begin_log_pass()
        self._unpublished_until = 0
        self._attention_demand()
        self._probe_reuse.clear()
        stamp_names = self._stamp_names()
        stamp = self._preference_stamp(stamp_names)
        if intent_triggered is None:
            # Direct callers follow changed markers; scheduled/retry callbacks
            # explicitly force validation even if an intent arrived meanwhile.
            intent_triggered = stamp != self._zoom_stamp or self._restart
        # A scheduled pass consumes the retry, even if discovery overtook it.
        # Intent work can reuse cached knowledge while validation remains scheduled.
        if not intent_triggered or self._next_retry is None or self._next_retry <= time.time():
            self._clear_retry()
        inherited_retry = self._runtime.retries.get('radar')
        self._restart = False
        self._warm_pending = False
        self._zoom_stamp = stamp
        self._start_inventory()
        # Production starts this at boot, 60s before the provider. An early tap
        # yields the lane while the bounded scanner finishes; no cache I/O here.
        # The finished scan runs the deferred pass itself (no polling), and a
        # failed one stays parked until its backoff retry succeeds.
        ready = self._cache_ready
        if not ready.is_set() and 'radar' not in self._runtime.inflight:
            self._cache_done.wait(30)  # direct callers (tools, tests) wait out the attempt
        with self._lock:
            usable = ready.wait(0)
            if not usable:
                self._cache_deferred = True
        if not usable:
            return
        # Boot validation owns a separate deadline. A successful 26-second
        # inventory wait must not hand acquisition an already expired budget.
        pass_deadline = time.monotonic() + RADAR_BUILD_DEADLINE_SEC
        self._consume_bad_tiles()
        if view_started and stamp == self._result_stamp:
            previous = self._result
            if (self._current_complete(time.time())
                    and len(previous.frames)>=8 and all(f['complete'] for f in previous.frames[-8:])
                    and self._inventory_valid(previous) and self._idle_context is not None):
                self._retained_refresh('idle')
                # Publish the retained loop first. Resume the idle tier on the
                # next watcher tick, in the same single-flight radar lane.
                self._warm_pending = True
                self._log_pass(pass_deadline-RADAR_BUILD_DEADLINE_SEC)
                return
        try:
            config = self._config() or {}
            lat = _num(_cfg(config, 'Station', 'Latitude'))
            lon = _num(_cfg(config, 'Station', 'Longitude'))
            if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                self._pass.update(outcome='failed', error='no location')
                self._result = _RADAR_NONE._replace(reason='no location')
                self._retained_refresh('failed')
                return
            previous = self._result
            station_lat, station_lon = lat, lon
            station = (lat, lon)
            if getattr(self, '_station', station) != station:
                self._result = previous = _RADAR_NONE  # a changed station must never inherit old pixels
            self._station = station
            try:
                with open(os.path.join(os.path.dirname(self.output_path), 'radar_center')) as preference:
                    raw = preference.read(1024)
                override = parse_center(raw.strip()) if len(raw) < 1024 else None
                if override is not None:
                    lat, lon = override
            except (OSError, ValueError, UnicodeError):
                pass  # absent/station/invalid: station viewport
            intent_record = self._read_intent()
            if intent_record is not None:
                selected = intent_record['center']
                lat, lon = station if selected == 'station' else (selected['lat'], selected['lon'])
            center = dict(lat=lat, lon=lon)
            centered = lat == station_lat and lon == station_lon
            try:
                from PIL import Image  # noqa: F401
            except ImportError:
                self._result = _RADAR_NONE._replace(reason='compositor unavailable')
                self._retained_refresh('failed')
                return
            auto_zoom = _radar_zoom_for(station_lat)
            desired = None
            try:
                with open(os.path.join(os.path.dirname(self.output_path), 'radar_zoom')) as preference:
                    raw = preference.read(128)
                value = raw.strip()
                if len(raw) < 128 and re.fullmatch(r'[0-9]{1,2}', value):
                    level = int(value)
                    if RADAR_MIN_ZOOM <= level <= max(s['max_zoom'] for s in _RADAR_SOURCES.values()):
                        desired = level
            except (OSError, ValueError, UnicodeError):
                pass  # absence/auto/invalid all mean latitude-auto
            if intent_record is not None:
                desired = None if intent_record['zoom']=='auto' else intent_record['zoom']
            viewed = self._is_viewed()
            unit = _radar_distance_unit(config)
            # One budget spans both attempts; geometry is source-specific, intent is not.
            try:
                with Path(self.output_path).with_name('radar_smooth').open() as preference:
                    raw = preference.read(128)
                smooth = len(raw) < 128 and raw.strip() == 'on'
            except (OSError, UnicodeError):
                smooth = False
            native = not self._level3_down()
            self._native_requested = native
            self._policy_ceiling = self._native_budget.snapshot()['ceilingState']
            self._auto_due = None
            ctx = dict(listing_results={}, smooth=smooth, native=native,
                native_ceiling=self._policy_ceiling, center=center, nexrad=_radar_nexrad(station_lat, station_lon, unit), viewed=viewed,
                desired=desired, auto_zoom=auto_zoom, builds=0, station=station, unit=unit, previous_result=previous,
                preference_stamp=stamp, stamp_names=stamp_names, intent_triggered=intent_triggered, discovery=discovery,
                deadline=pass_deadline, pass_deadline=pass_deadline, inventory=self._disk_inventory, manifest_cache=self._manifest_cache)
            self._negative = dict(list((k, v) for k, v in self._negative.items() if v > time.monotonic())[-512:])
            for key in list(self._metadata):
                if time.monotonic()-self._metadata_at.get(key, 0) > 3600:
                    self._metadata.pop(key, None); self._metadata_at.pop(key, None)
            adapters = [('rainviewer', self._rainviewer_frames)]
            if _radar_iem_eligible(station_lat, station_lon):
                adapters.insert(0, ('iem-mrms-lcref', self._iem_frames))
            site = ctx['nexrad']
            # A radar in range is enough: the CONUS mask bounds MRMS, not NEXRAD, and
            # Alaska, Hawaii, Puerto Rico and Guam have their own sites (IEM lists
            # them, NOAA publishes their Level III).
            site_ok = bool(site and site['distanceMeters'] <= RADAR_SITE_RANGE_METERS)
            ctx['sources'] = [dict(mode='mosaic', available=True),
                dict(mode='site', siteId=site['id'] if site else None, available=site_ok,
                     reason=None if site_ok else 'no site in range')]
            try:
                raw_seq = Path(os.path.join(os.path.dirname(self.output_path), 'radar_intent')).read_text().strip()
                seq = int(raw_seq) if re.fullmatch(r'[0-9]{1,12}', raw_seq) else 0
            except (OSError, UnicodeError):
                seq = 0
            ctx['intent'] = dict(intent_record) if intent_record else dict(seq=seq, zoom=desired if desired is not None else 'auto',
                                 center='station' if centered else dict(center))
            self._checkpoint(ctx)
            knobs = self._attention_knobs()
            ctx['attention'] = self._effective_tier()
            if self._attention_active():
                ctx['attention_knobs'] = knobs
                ctx['frames_target'] = knobs['frames']
                if not knobs['tiles']:
                    self._pass.update(source='iem-nexrad-n0b' if site_ok else 'iem-mrms-lcref', site=site['id'] if site_ok else None)
                    self._quiet_pass(ctx, knobs, site, site_ok)
                    return
            # Refresh closest-site evidence on Region's existing discovery wakeup,
            # including unchanged MRMS stamps and unviewed/zoom-below-seven maps.
            if discovery and previous.source_mode == 'mosaic' and site_ok:
                camera_zoom = desired if desired is not None else auto_zoom
                check = dict(ctx, zoom=min(camera_zoom, previous.max_zoom), camera_zoom=camera_zoom)
                stamps = [f['ts'] for f in previous.frames[-(8 if viewed else 1):]]
                reserve = max(RADAR_HISTORY_RESERVE, self._mandatory_reserve(previous.source_id, check, stamps))
                check['request_reserve'] = reserve
                # This IEM listing has no Level III transport dependency.
                if not self._headroom_delay('iem-nexrad-n0b', reserve+1, dict(check, native=False)):
                    if self._session is None or self._provider != 'iem':
                        if self._session is not None:
                            self._session.close()
                        self._session = RadarSession()
                        self._provider = 'iem'
                    self._session.begin_pass(pass_deadline)
                    self._session.on_retry = lambda end, first_byte=False: self._transport_retry(
                        'iem-nexrad-n0b', end, reserve=reserve, first_byte=first_byte)
                    try:
                        self._site_listing(check, dict(site))
                    except _RadarBudget:
                        pass  # preserve unknown/last evidence until the next cadence
            if not self._attention_active() or knobs['tiles']:
                if self._auto_source(ctx, site_ok) == 'site':
                    adapters.insert(0, ('iem-nexrad-n0b', self._site_frames))
            target_mode = 'site' if adapters[0][0] == 'iem-nexrad-n0b' else 'mosaic'
            if (previous.available and previous.source_mode == target_mode
                    and previous.source_id in dict(adapters) and previous.source_id != adapters[0][0]
                    and time.monotonic()-self._source_since < 300
                    and self._transport_failures.get(previous.source_id, 0) < 3):
                active = next(pair for pair in adapters if pair[0] == previous.source_id)
                adapters = [active]  # five-minute dwell includes transport recovery probes
            errors = []
            for source, adapter in adapters:
                ctx['target_source'] = self._target_source = source
                self._pass.update(source=source, site=site["id"] if source == "iem-nexrad-n0b" and site else None)
                ctx.pop('level3_failed', None)
                for kind, count in self._health.failure_counts(source).items():
                    ctx.pop(kind+'_failure', None)
                    ctx[kind+'_failure_start'] = count
                ctx.pop('staging_source', None)
                ctx['switch_reason'] = '; '.join(errors) or 'initial source selection'
                if previous.frames and source != previous.source_id:
                    ctx['staging_source'] = source
                    ctx['switch_reason'] = '; '.join(errors) or (
                        'automatic settled zoom/coverage selection' if previous.source_mode != target_mode
                        else 'preferred source recovered after 300s dwell')
                if any(self._cooldowns.get(s, 0) > time.monotonic()
                       for s in self._transport_sources(source, ctx)):
                    self._retained_refresh('failed')
                    self._budget_retry(source, 1, reason='provider')
                    return
                ctx['deadline'] = min(pass_deadline, time.monotonic()+RADAR_SOURCE_DEADLINE_SEC)
                ctx.pop('hedge_budget', None)
                ctx.pop('missing_tiles', None)
                ctx.pop('retained_failed', None)
                try:
                    probes = [probe for s in self._transport_sources(source, ctx)
                              for probe in self._health.probes(s)]
                except CircuitOpen as error:
                    if source == 'iem-nexrad-n0b':
                        ctx['sources'][1].update(available=False, reason='scan unavailable')
                    errors.append(str(error))
                    if not self._failed_pass(source, error, ctx):
                        return
                    continue
                zoom = max(RADAR_SITE_MIN_ZOOM if source == 'iem-nexrad-n0b' else RADAR_MIN_ZOOM, min(desired if desired is not None else auto_zoom,
                                               _RADAR_SOURCES[source]['max_zoom']))
                camera_zoom = desired if desired is not None else auto_zoom
                tiles, mpp, bounds, _ = _radar_viewport(lat, lon, zoom,
                    RADAR_VIEWPORT_W*2**(zoom-camera_zoom), RADAR_VIEWPORT_H*2**(zoom-camera_zoom))
                ctx['camera_zoom'] = camera_zoom
                bar, rings = _radar_scale(mpp, RADAR_VIEWPORT_PX, unit, max_fraction=.25)
                identity = (lat, lon, zoom, source)
                with self._lock:
                    geometry = (identity, stamp)
                    if self._view_geometry != geometry:
                        self._view_geometry = geometry
                        self._geometry_since = time.monotonic()
                ctx.update(zoom=zoom, tiles=tiles, mpp=mpp, bounds=bounds, bar=bar, rings=rings,
                           identity=identity, candidates=[])
                ctx['tiles'] = sorted(_radar_grid(ctx),key=lambda t: math.hypot(t[0]+.5-world_point(lat,lon,zoom)[0]/256,t[1]+.5-world_point(lat,lon,zoom)[1]/256))
                from lib.radar_basemap import version
                ctx['geo'] = dict(version=version(),base='radar/geo/',sites='radar/sites-'+_radar_sites_revision()+'.json')
                # Source/station knowledge can exist before the first tile. No camera acknowledgement.
                if not self._result.available:
                    self._result = _RADAR_NONE._replace(available=True,reason=None,
                        center=dict(lat=station_lat,lon=station_lon), zoom=zoom, nexrad=ctx['nexrad'],
                        source_id=source, **_RADAR_SOURCES[source], zoom_desired=desired,
                        zoom_auto_level=auto_zoom, geo=ctx['geo'], units=unit, rings=rings,
                        sources=tuple(ctx['sources']))
                self._publish_refresh(ctx,state='newest',frameIndex=0,frameTotal=1)
                provider = _RADAR_SOURCES[source]['provider']
                if self._session is None or self._provider != provider:
                    if self._session is not None:
                        self._session.close()
                    self._session = RadarSession()
                    self._provider = provider
                self._session.on_retry = lambda end, first_byte=False, source=source: self._transport_retry(source, end, first_byte=first_byte)
                self._session.begin_pass(ctx['deadline'])
                needed = 1  # discover first; price actual missing visible layers below
                if self._headroom_delay(source, 1 if discovery else needed):
                    self._publish_refresh(ctx, state='idle')
                    self._budget_retry(source, needed)
                    return
                try:
                    for probe_url, is_metadata in probes:
                        probe_source = RADAR_LEVEL3_TRANSPORT if probe_url.startswith(RADAR_LEVEL3_BUCKET) else source
                        try:
                            raw = self._request(probe_source, probe_url, ctx['deadline'],
                                method='GET' if is_metadata else 'HEAD', metadata=True)
                        except (_RadarBudget, _RadarSuperseded):
                            raise
                        except Exception as error:
                            # Recovery probes precede frame inputs but need the
                            # same host isolation and v1 fallback on failure.
                            if probe_source == RADAR_LEVEL3_TRANSPORT:
                                ctx['level3_failed'] = True
                                self._level3_fallback(error)
                            raise
                        if is_metadata:
                            self._probe_reuse[probe_url] = raw
                    ctx['tile_workers'] = RADAR_NEWEST_TILE_WORKERS
                    ctx['reuse_newest'] = False
                    try:
                        adapter(ctx)
                    except _RadarRevalidate:
                        self._forget(source)
                        ctx.update(intent_triggered=False, reuse_newest=False)
                        ctx.pop('site_reasons', None)
                        adapter(ctx)  # same deadline, build count and rolling request gate
                    if not ctx.get('retained_failed'):
                        self._note_auto_choice(ctx, source)
                        if source in self._pass['validated']:
                            self._pass['recovered'].add((source, 'pass'))
                        if self._pass['outcome'] != 'deferred':
                            self._pass['outcome'] = 'ok'
                        self._transport_failures.pop(source, None)
                        self._local_failure_streak = 0
                    try:
                        self._prune(previous)
                    except OSError as error:
                        self._logger.warning(f'almanac_emit: radar cache prune failed - {error}')
                    if ctx.get('retained_failed'):
                        self._forget(source)
                        self._retained_refresh('failed')
                    else:
                        self._publish_refresh(ctx, state='idle')
                    probe_delay = self._probe_delay()
                    if probe_delay is not None:
                        self._schedule_retry('radar', self._check, max(1, probe_delay))
                    return
                except _RadarUnchanged:
                    self._clear_retry()
                    self._note_auto_choice(ctx, source)
                    self._pass['outcome'] = 'unchanged'
                    if source in self._pass['validated']:
                        self._pass['recovered'].add((source, 'pass'))
                    self._transport_failures.pop(source, None)
                    self._local_failure_streak = 0
                    self._retained_refresh('idle')
                    return
                except _RadarSuperseded:
                    raise
                except _RadarBudget as error:
                    # Local rate capacity does not erase earlier service failures.
                    # The pass log names the yield: a deferred pass with error=None
                    # hid a read-only tile cache for a whole evening (2026-09-16).
                    with self._lock:
                        self._pass['error'] = 'deferred: '+(str(error) or type(error).__name__)
                    self._forget(source)
                    fresh = (previous.available and previous.ts_frame is not None and 0 <= time.time()-previous.ts_frame
                             and not _radar_freshness(previous, time.time())['stale'])
                    if self._result.ts_frame is None:
                        self._result = previous
                    self._retained_refresh('idle' if fresh else 'failed')
                    self._budget_retry(source, needed)
                    return
                except Exception as error:
                    self._health.last_error = str(error) or type(error).__name__
                    self._forget(source)
                    self._checkpoint(ctx)
                    self._session.close()
                    self._session = None
                    errors.append(str(error))
                    if source == 'iem-nexrad-n0b':
                        ctx['sources'][1].update(available=False, reason=ctx.get('site_failure', 'scan unavailable'))
                        if ctx.get('site_failure') == 'out of view':
                            self._result = self._result._replace(sources=tuple(ctx['sources']),
                                reason='out of view' if not self._result.frames else self._result.reason)
                    if not self._failed_pass(source, error, ctx):
                        if previous.available and previous.source_id != source:
                            continue  # recovery failed; refresh the active fallback
                        return
                    errors[-1] = f'{source}: 3 consecutive failed passes ({type(error).__name__}: {error})'
            raise ValueError('; '.join(errors))
        except _RadarSuperseded:
            self._pass['outcome'] = 'superseded'
            self._restart = True
            # The worker's single-flight guard releases before its immediate wakeup.
            # The 100 ms watcher also sees the unserved preference stamp.
            self._clear_retry()
        except Exception as error:
            if self._result.ts_frame is None:
                self._result=self._result._replace(available=self._result.center is not None,reason='no radar tiles')
            if 'ctx' in locals():
                if self._result.available:
                    self._retained_refresh('failed')
                else:
                    self._publish_refresh(ctx, state='failed')
            self._health.last_error = str(error) or type(error).__name__
            self._pass['outcome'] = 'failed'
            self._log_failure(self._pass['source'] or self._result.source_id, error)
            probe_delay = self._probe_delay()
            self._schedule_retry('radar', self._check,
                RADAR_RETRY_SEC if probe_delay is None else max(1, probe_delay),
                retry_reason='deadline' if isinstance(error, TimeoutError) else
                'provider' if failure_class(error) == 'host' else 'local')

        finally:
            # A completed pass retires its fulfilled retry. Preserve a new yield
            # and an intent pass's still-pending scheduled validation.
            if (self._pass['outcome'] in ('ok', 'unchanged')
                    and self._runtime.retries.get('radar') is inherited_retry
                    and not (intent_triggered and self._next_retry is not None
                             and self._next_retry > time.time())):
                self._clear_retry()
            if any(self._pending.get(k) for k in ('newest','four','eight')) and not self._restart and 'radar' not in self._runtime.retries:
                self._budget_retry(self._result.source_id, 1)
            self._native_budget.persist(wait=False)
            self._arm_discovery()
            self._log_pass(pass_deadline-RADAR_BUILD_DEADLINE_SEC)
            if not self._runtime.running and self._session is not None and 'radar' in self._runtime.inflight:
                self._session.close()
                self._session = None

    @staticmethod
    def _payload(snap, now, tz, refresh=None, style='24 hr'):
        refresh = dict(refresh or dict(state='idle', frameIndex=0, frameTotal=0))
        retry = refresh.get('nextRetry')
        if not isinstance(retry, (int, float)) or not math.isfinite(retry) or retry <= now:
            refresh.pop('nextRetry', None)
            refresh.pop('retryReason', None)
        def local(ts):
            return _clock(datetime.fromtimestamp(ts,tz), style) if ts is not None else None
        complete = [f['ts'] for f in snap.frames if f['complete']]
        gaps = [b-a for a,b in zip(complete,complete[1:]) if b>a]
        fresh = _radar_freshness(snap, now)
        age = fresh['age']
        factor = 1609.344 if snap.units == 'mi' else 1000
        choices = [dict(meters=d*factor,label=f'{d} {snap.units}') for d in (5,10,20,25,50,100,150,200,250)]
        nearest = None
        if snap.nexrad:
            nearest = dict(reporting=None, newestTs=None, reason=None, checkedTs=None, nextCheckTs=None)
            nearest.update(snap.nexrad)
            nearest.update(ageSec=max(0, int(now-nearest['newestTs'])) if nearest['newestTs'] is not None else None,
                           checkedAt=local(nearest['checkedTs']), nextCheckAt=local(nearest['nextCheckTs']))
        tiles = dict(snap.tiles or {})
        # The loop target in force (refresh.loopFrames). Publish the newest
        # `loop` slots, which the engine is completing, plus any older frame
        # that is already complete (left by an earlier, larger target). An
        # incomplete slot outside the target is never fetched: listing it told
        # the page to wait for it forever ("Refreshing · frame 4 of 8").
        loop = refresh.get('loopFrames')
        loop = loop if isinstance(loop, int) and not isinstance(loop, bool) and loop > 0 else None
        withheld = set() if loop is None else {f['ts'] for f in snap.frames[:-loop] if not f['complete']}
        tiles['frames'] = [{k:v for k,v in dict(f,at=local(f['ts']),**({'observedRange':_radar_observed_range(f)}
                               if _radar_observed_range(f) else {})).items() if k not in ('complete','publishable','acquiredSites')}
                           for f in tiles.get('frames',()) if f['ts'] not in withheld]
        return dict(available=snap.available,reason=snap.reason,geo=snap.geo,tiles=tiles,
            intent=tiles.get('intent', {}), geometry=tiles.get('geometry'), camera=tiles.get('camera'),
            advertisedTs=max((f['ts'] for f in snap.frames), default=None), acquiredTs=snap.ts_frame,
            pending=refresh.get('pending', {}),
            **({k: refresh[k] for k in ('nextRetry', 'retryReason') if k in refresh}),
            switchDeadlineSec=20,
            sourceMode=snap.source_mode,siteId=snap.site_id,sources=list(snap.sources),
            sites=[dict(s,ageSec=int(now-s['newestTs']) if s['newestTs'] is not None else None) for s in snap.sites],
            sitesConsidered=snap.sites_considered,sitesDrawn=len(snap.sites),
            refresh=refresh or dict(state='idle',frameIndex=0,frameTotal=0),loopFrames=loop,
            scanningSlowly=snap.scanning_slowly,latestOnly=snap.source_mode=='site' and snap.scan_cadence_sec is None and len(snap.frames)==1,
            scanCadenceSec=snap.scan_cadence_sec,scanLatencySec=snap.scan_latency_sec,scanMode=snap.scan_mode,scanModeSource=snap.scan_mode_source,
            sourceId=snap.source_id,
            attribution='NOAA NEXRAD Level III' if _radar_is_native((snap.tiles or {}).get('variant')) else snap.attribution,
            attributionUrl='https://registry.opendata.aws/noaa-nexrad/' if _radar_is_native((snap.tiles or {}).get('variant')) else snap.attribution_url,
            provider=snap.provider,cadenceSec=snap.cadence,frameSpacingSec=median(gaps) if gaps else None,
            historyGaps=any(g!=snap.cadence for g in gaps),historySpanSec=complete[-1]-complete[0] if complete else 0,
            completeFrameCount=len(complete),partialCoverage=snap.partial_coverage,center=snap.center,
            smooth=(snap.tiles or {}).get('smooth',False),native=_radar_is_native((snap.tiles or {}).get('variant')),
            zoomAuto=snap.zoom_desired is None,zoomAutoLevel=snap.zoom_auto_level,
            zoomMin=RADAR_MIN_ZOOM,zoomMax=snap.max_zoom,
            zoomSource='MRMS' if snap.source_id=='iem-mrms-lcref' else 'NEXRAD' if snap.source_mode=='site' else 'RainViewer',
            zoomCapped=(snap.zoom_desired if snap.zoom_desired is not None else snap.zoom_auto_level)!=snap.zoom,
            zoomDesired=snap.zoom_desired,units=snap.units,scaleChoices=choices,
            rings=[dict(meters=float(r['label'].split()[0])*factor,label=r['label']) for r in snap.rings or ()],
            frameCount=len(snap.frames),observedAt=local(snap.ts_frame),observedTs=snap.ts_frame,
            ageSec=age,staleSec=fresh['stale_sec'],stale=fresh['stale'],observedRange=fresh['observed'],
            fetchedAt=snap.ts_fetch,updatedAt=local(snap.ts_fetch),nexrad=nearest,legend=dict(snap.legend))

    def _warnings_query(self):
        """ (reach, home, codes, url) for the station, or None without a
        location. `reach` is everything the page camera can show (its floor
        zoom, RADAR_MIN_ZOOM, and pan limit: see nws_warnings.Reach); `home`
        the station's automatic view, which decides the fast cadence. codes is
        empty where no NWS area is in reach. """
        config = self._config()
        lat = _num(_cfg(config, 'Station', 'Latitude'))
        lon = _num(_cfg(config, 'Station', 'Longitude'))
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        cached = self._warnings_query_cache
        if cached is None or cached[0] != (lat, lon):
            reach = nws_warnings.Reach(lat, lon, RADAR_MIN_ZOOM)
            home = nws_warnings.Reach(lat, lon, _radar_zoom_for(lat))
            codes = nws_warnings.area_codes(reach)
            cached = self._warnings_query_cache = ((lat, lon), (reach, home, codes,
                (nws_warnings.query_url(codes) if codes else None)), nws_warnings.area_codes(home))
        return cached[1]

    def _warnings_fast(self, now):
        """ Poll fast while someone is on (or just left) the radar: they can
        pan anywhere in reach. Without a viewer, only what concerns the
        station's own view speeds it up: weather nearby (attention) where NWS
        covers that view, or a warning in force within it. """
        attention = self._attention
        if attention.tier in ('warm', 'live') or self._warnings.near(now):
            return True
        home_codes = self._warnings_query_cache[2] if self._warnings_query() else ()
        return bool(home_codes) and bool(attention.weather(now))

    def _check_warnings(self, _dt=None):
        """ The TICK_SEC due-check: cheap, on the Clock thread; the fetch
        itself runs on a daemon thread (one at a time, like every provider). """
        try:
            query = self._warnings_query()
            if query is None:
                return
            now = time.time()
            reach, home, codes, url = query
            if not codes:
                if self._warnings.coverage is not False:
                    self._warnings.no_coverage(now)   # no NWS coverage: no polygons, no errors, no requests
                return
            fast = self._warnings_fast(now)
            if self._warnings.due(now, fast, url):
                self._spawn('warnings', lambda: self._do_warnings(url, reach, home, fast))
        except Exception as error:                                        # noqa: BLE001
            self._logger.warning(f'almanac_emit: warnings check failed - {error}')

    def _warnings_open(self, req):
        """ One GET under ONE end-to-end deadline (FETCH_DEADLINE_SEC): DNS,
        connect, TLS, the request, every body read and any reconnect retry.
        urlopen's timeout bounds each socket operation, so a body trickled a
        byte at a time could hold the only warnings worker for hours; the
        radar transport recomputes the remaining time before every read. """
        if self._warnings_session is None:
            self._warnings_session = RadarSession()
        return self._warnings_session.open(req, timeout=nws_warnings.FETCH_DEADLINE_SEC)

    def _do_warnings(self, url, reach, home=None, fast=False):
        """ One area fetch. Never raises. A failure keeps the last-good
        polygons, counts toward backoff and reports refresh failure until staleAt. """
        import urllib.request
        import urllib.error
        now = time.time()
        self._warnings.began(now, fast)
        try:
            config = self._config()
            contact = (_cfg(config, 'Station', 'Contact')
                       or os.environ.get('ALMANAC_CONTACT') or ALERTS_UA_FALLBACK)
            headers = {'User-Agent': contact, 'Accept': 'application/geo+json'}
            if self._warnings.etag and self._warnings.query == url:
                headers['If-None-Match'] = self._warnings.etag
            req = urllib.request.Request(url, headers=headers)
            try:
                with self._warnings_open(req) as resp:
                    body = resp.read(nws_warnings.MAX_BODY_BYTES + 1)
                    etag = (getattr(resp, 'headers', None) or {}).get('ETag')
            except urllib.error.HTTPError as http_error:
                if http_error.code == 304:
                    self._warnings.not_modified(time.time())
                    return
                retry = _num((http_error.headers or {}).get('Retry-After')) if http_error.headers else None
                self._warnings.failed(time.time(), f'HTTP {http_error.code}', retry)
                self._logger.warning(f'almanac_emit: warnings fetch failed - HTTP {http_error.code}')
                return
            if len(body) > nws_warnings.MAX_BODY_BYTES:
                raise ValueError('warnings response too large')
            data = json.loads(body.decode('utf-8'))
            features = data.get('features') if isinstance(data, dict) else None
            if not isinstance(features, list):
                raise ValueError('warnings response has no features')
            tz = _station_tz(config)
            style = _clock_style(config or {})
            until = (lambda ts: _clock(datetime.fromtimestamp(ts, tz), style)) if tz else None
            done = time.time()
            items = nws_warnings.parse(features, done, reach, until, home)
            self._warnings.succeeded(done, items, url, etag)
            covering = frozenset(i['id'] for i in items if i['affectsStation'])
            if covering - self._warnings_seen:
                # A new warning covers the station: refresh the strip now rather
                # than on its 15-minute cadence (its semantics are unchanged).
                self._refresh_alerts()
            self._warnings_seen = covering
        except Exception as error:                                        # noqa: BLE001
            self._warnings.failed(time.time(), error)
            self._logger.warning(f'almanac_emit: warnings fetch failed - {error}')
