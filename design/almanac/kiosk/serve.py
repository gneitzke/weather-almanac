#!/usr/bin/env python3
# Tiny static server for the almanac overlay (index.html + wx.json) plus a
# /health endpoint for monitoring. Replaces `python -m http.server`.
#
#   /health -> JSON {status, reason, dataAgeSec, obsAgeSec, renders, polls, ...}
#     status: "ok"        engine heartbeat AND observation both fresh
#             "stale"     wx.json older than STALE_SEC (engine stalled)
#             "degraded"  wx.json fresh, but the newest observation is older
#                         than OBS_STALE_SEC (sensor silent — the engine is
#                         faithfully republishing a dead reading)
#             "error"     wx.json missing/unreadable (engine down)
#     reason: "engine stalled" | "sensor silent" | the read error
#   HTTP 200 when ok, 503 otherwise (so a monitor can alert on non-2xx).
#
# Bind stays on 127.0.0.1 by default (chromium is local; no data leaves the box).
# Set WFP_BIND=0.0.0.0 to expose /health (and the page) to the LAN for remote
# monitoring and radar control — private-network browsers share the panel view.
import http.server, socketserver, json, math, os, time, threading, re, io, zlib, ipaddress
from urllib.parse import parse_qs
from decimal import Decimal
from pathlib import Path
import sys
# The kiosk launches this script from its web directory, outside the repo root.
REPO_ROOT = str(Path(__file__).resolve().parents[3])
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
from lib.radar_auto import source_preference

PORT      = int(os.environ.get("WFP_PORT", "8137"))
WEB       = os.environ.get("WFP_WEB", ".")
BIND      = os.environ.get("WFP_BIND", "127.0.0.1")
DATA      = os.environ.get("WFP_DATA", "/tmp/wfp_data/wx.json")
STALE_SEC = int(os.environ.get("WFP_STALE_SEC", "20"))
# A station can go silent for minutes while the engine keeps emitting. Long
# enough not to trip on one dropped Tempest report (they arrive ~60 s apart).
OBS_STALE_SEC = int(os.environ.get("WFP_OBS_STALE_SEC", "300"))

# Aligned with the emitter shared floor and highest source ceiling.
RADAR_MIN_ZOOM, RADAR_MAX_DESIRED_ZOOM = 4, 10

LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")

# The most recent private-LAN client that fetched anything, written at most every
# 30 s to <data dir>/last_viewer. The wifi keepalive sends that host a few unicast
# frames each minute: on a multi-node mesh the node bridging the Pi stopped
# forwarding wired-side traffic to it (ARP "(incomplete)" from a wired Mac while
# the Pi's own outbound worked), and the Pi's OWN frames toward a wired host are
# what re-teach the node's bridge table. Loopback and public addresses are never
# recorded; the file holds one IP and nothing else.
_LAN_VIEWER_INTERVAL = 30.0
_lan_viewer_at = 0.0
_lan_viewer_lock = threading.Lock()


_CONTROLLER_NETWORKS = tuple(map(ipaddress.ip_network, (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7', 'fe80::/10')))


def _client_ip(address):
    try:
        ip = ipaddress.ip_address(address)
        return (ip.ipv4_mapped or ip) if isinstance(ip, ipaddress.IPv6Address) else ip
    except ValueError:
        return None


def _is_loopback(address):
    ip = _client_ip(address)
    return ip is not None and ip.is_loopback


def _default_gateways(route_path='/proc/net/route'):
    """Read Linux's little-endian IPv4 routes; injectable/off-Linux safe."""
    gateways = set()
    try:
        lines = Path(route_path).read_text().splitlines()[1:]
    except (OSError, UnicodeError):
        return gateways
    for line in lines:
        fields = line.split()
        try:
            if (fields[1] == '00000000' and fields[7] == '00000000'
                    and int(fields[3], 16) & 3 == 3):
                ip = ipaddress.IPv4Address(int(fields[2], 16).to_bytes(4, 'little'))
                if not ip.is_unspecified:
                    gateways.add(ip)
        except (IndexError, ValueError, OverflowError):
            continue
    return gateways


# Re-read, not frozen at import: the kiosk unit starts before DHCP on the USB
# wifi dongle, and a gateway can change, so a startup read could stay empty and
# silently stop excluding the router. None reads live; tests inject a set.
_DEFAULT_GATEWAYS = None
_GATEWAY_TTL_SEC = 30
_gateway_cache = (float('-inf'), frozenset())


def _gateways():
    global _gateway_cache
    if _DEFAULT_GATEWAYS is not None:
        return _DEFAULT_GATEWAYS
    now = time.monotonic()
    if now - _gateway_cache[0] >= _GATEWAY_TTL_SEC:
        _gateway_cache = (now, frozenset(_default_gateways()))
    return _gateway_cache[1]


def _is_controller(address):
    ip = _client_ip(address)
    return ip is not None and ip not in _gateways() and (ip.is_loopback or any(ip in net for net in _CONTROLLER_NETWORKS))


def _is_private_ipv4(address):
    ip = _client_ip(address)
    return isinstance(ip, ipaddress.IPv4Address) and any(ip in net for net in _CONTROLLER_NETWORKS)


# Per IP (including mapped aliases), under _count_lock. Reads always succeed.
# A full bucket permits a gesture burst; ordinary polling uses <4 tokens/sec.
_CONTROL_RATE, _CONTROL_BURST = 20.0, 60.0
_CONTROL_CLIENTS = 4096
_control_buckets = {}
_bad_tile_buckets = {}


def _allow_control_write(address, buckets=None):
    if buckets is None:
        buckets = _control_buckets
    now = time.monotonic()
    key = str(_client_ip(address))
    if key not in buckets:
        for stale, (_, at) in list(buckets.items()):
            if now - at >= 60:
                del buckets[stale]
        if len(buckets) >= _CONTROL_CLIENTS:
            return False
    tokens, at = buckets.get(key, (_CONTROL_BURST, now))
    tokens = min(_CONTROL_BURST, tokens + max(0, now-at)*_CONTROL_RATE)
    allowed = tokens >= 1
    buckets[key] = (tokens-1 if allowed else tokens, now)
    return allowed


def _valid_radar_session(params):
    values = params.get('radarSession', [])
    return len(values) == 1 and re.fullmatch(r'[A-Za-z0-9-]{16,64}', values[0]) is not None


def _note_lan_viewer(address):
    global _lan_viewer_at
    ip = _client_ip(address)
    if not isinstance(ip, ipaddress.IPv4Address) or not _is_private_ipv4(address):
        return
    address = str(ip)
    now = time.time()
    with _lan_viewer_lock:
        if now - _lan_viewer_at < _LAN_VIEWER_INTERVAL:
            return
        _lan_viewer_at = now
    marker = os.path.join(os.path.dirname(DATA), "last_viewer")
    tmp = f"{marker}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w") as f:
            f.write(address + "\n")
        os.replace(tmp, marker)
    except OSError:
        pass

# Two counters, and the difference matters. `polls` counts wx.json REQUESTS
# from anyone — a LAN viewer, a curl, a renderer that fetched and then threw.
# `renders` counts frames the KIOSK actually painted: the page reports the
# previous frame's success by adding r=1 to its next poll, and only a loopback
# client is believed. The watchdog reads `renders`, because a growing request
# count never proved anything reached the screen.
_polls      = 0
_renders    = 0
_count_lock = threading.Lock()


# A separate session marker preserves radar_viewed's 15-minute demand hint.
# The engine admits deep history only during a continuous, live view session.
RADAR_VIEW_POLL_GAP_SEC = 5


# A human touched a controller page (any tab): the page adds `touch` to its next
# poll. The engine's attention tiers read this marker's age. At most one write
# every ten seconds; a browser left open never writes it.
_presence_lock = threading.Lock()
_presence_at = 0.0
_view_owner = None


def _view_transaction(params):
    """Explicit page reports, independently ordered from camera transactions.

    Caller holds _count_lock. Once a current page reports, auxiliary reads and
    delayed/aborted requests cannot clear or resurrect its viewing marker.
    A reload claims the owner returned in X-View-Session, like camera ownership.
    """
    global _view_owner
    if 'viewSession' not in params:
        return _view_owner is None  # legacy pages, until the first explicit report
    if any(len(params.get(k, [])) != 1 for k in ('viewSession', 'viewSeq', 'view')):
        return False
    session, seq = params['viewSession'][0], params['viewSeq'][0]
    if (not re.fullmatch(r'[A-Za-z0-9-]{16,64}', session)
            or not re.fullmatch(r'[0-9]{1,12}', seq)
            or params['view'] not in (['radar'], ['none'])):
        return False
    seq = int(seq)
    if _view_owner is None:
        _view_owner = dict(session=session, seq=-1)
    elif session != _view_owner['session']:
        if params.get('viewClaim') != [_view_owner['session']]:
            return False
        _view_owner = dict(session=session, seq=-1)
    if seq <= _view_owner['seq']:
        return False
    _view_owner['seq'] = seq
    return True


def _note_presence():
    global _presence_at
    now = time.time()
    with _presence_lock:
        if 0 <= now - _presence_at < 10:
            return
        marker = os.path.join(os.path.dirname(DATA), 'presence')
        tmp = f'{marker}.tmp.{os.getpid()}'
        try:
            with open(tmp, 'w') as f:
                f.write(str(now))
            os.replace(tmp, marker)
            _presence_at = now
        except OSError:
            pass
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _write_radar_viewing(viewed):
    marker = os.path.join(os.path.dirname(DATA), 'radar_viewing')
    tmp = f'{marker}.tmp.{os.getpid()}'
    try:
        if not viewed:
            try:
                os.unlink(marker)
            except FileNotFoundError:
                pass
            return
        now = time.time()
        since = now
        try:
            with open(marker) as f:
                previous = json.load(f)
            if (0 <= now-previous['last'] < RADAR_VIEW_POLL_GAP_SEC
                    and previous['since'] <= previous['last']):
                since = previous['since']
        except (OSError, ValueError, TypeError, KeyError):
            pass
        with open(tmp, 'w') as f:
            json.dump(dict(since=since, last=now), f)
        os.replace(tmp, marker)
    except OSError:
        pass
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _write_radar_zoom(values):
    return _write_radar_preference('radar_zoom', values)


def _write_radar_center(values):
    return _write_radar_preference('radar_center', values)


def _write_radar_source(values):
    return _write_radar_preference('radar_source', values)


def _write_radar_preference(name, values):
    """Caller holds _count_lock and has checked controller admission. Polling cannot fail here."""
    if name not in ('radar_zoom', 'radar_source', 'radar_center', 'radar_smooth') or len(values) != 1:
        return
    value = values[0]
    if name == 'radar_center':
        if value != 'station':
            if not re.fullmatch(r'-?\d{1,3}(\.\d+)?,-?\d{1,3}(\.\d+)?', value, re.ASCII):
                return
            lat, lon = map(float, value.split(','))
            if not (-85.05112878 <= lat <= 85.05112878 and -180 <= lon <= 180):
                return
            # Keep canonical floats in the decimal grammar (str() can emit 1e-10).
            value = ','.join(format(Decimal(str(n)), 'f') for n in (lat, lon))
    elif name == 'radar_smooth':
        if value not in ('on', 'off'):
            return
    elif name == 'radar_source':
        if value not in ('auto', 'mosaic', 'site'):
            return
    elif value != 'auto':
        if not re.fullmatch(r'[0-9]{1,2}', value):
            return
        level = int(value)
        if not RADAR_MIN_ZOOM <= level <= RADAR_MAX_DESIRED_ZOOM:
            return
        value = str(level)
    # The kiosk links this sibling to durable station storage before startup.
    # Resolve the link so replacement updates its target, not the link.
    marker = os.path.join(os.path.dirname(DATA), name)
    # Pan belongs to tmpfs. Never follow a durable link, even if one was
    # accidentally installed: atomic replacement replaces the link itself.
    if name != 'radar_center':
        marker = os.path.realpath(marker)
    tmp = f"{marker}.tmp.{os.getpid()}"
    try:
        try:
            with open(marker) as f:
                limit = 1024 if name == 'radar_center' else 128
                current = f.read(limit)
                if (len(current) < limit and current.strip() == value
                        and not (name == 'radar_center' and os.path.islink(marker))):
                    return
        except (OSError, UnicodeError):
            pass
        with open(tmp, 'w') as f:
            f.write(value + '\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, marker)
    except OSError:
        pass
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _read_radar_intent():
    try:
        with open(os.path.join(os.path.dirname(DATA), 'radar_intent')) as stream:
            record = json.load(stream)
        if not isinstance(record, dict): record = {'seq': int(record)}
        if type(record.get('seq')) is not int or not 0 <= record['seq'] <= 999999999999:
            return {'seq': 0}
        return record
    except (OSError, ValueError, TypeError):
        return {'seq': 0}


def _expire_radar_source():
    """Expire before processing a new touch, under the server's writer lock.

    Keep camera ownership/generation intact; advance only the worker sequence.
    An older debounce callback cannot restore the expired manual preference.
    """
    record = _read_radar_intent()
    intent = record if 'source' in record else None
    root = Path(DATA).parent
    try:
        requested = intent['source'] if intent else (root/'radar_source').read_text()[:128].strip()
    except (OSError, UnicodeError):
        return
    if requested not in ('mosaic', 'site'):
        return
    if source_preference(root, intent, time.time()) != 'auto':
        return
    if intent and intent['source'] != 'auto':
        record = dict(record, source='auto', seq=min(999999999999, record['seq']+1))
        marker = root / 'radar_intent'
        temporary = marker.with_name(marker.name+'.tmp')
        try:
            temporary.write_text(json.dumps(record))
            os.replace(temporary, marker)
        except OSError:
            return
        finally:
            temporary.unlink(missing_ok=True)
    _write_radar_source(['auto'])


def _write_radar_intent(params):
    """One validated transaction; duplicate generations never mutate preferences."""
    keys = ('radarSeq','radarZoom','radarSource','radarCenter')
    if any(len(params.get(k, [])) != 1 for k in keys): return
    seq, zoom, source, center = (params[k][0] for k in keys)
    if not re.fullmatch(r'[0-9]{1,12}', seq, re.ASCII): return
    seq = int(seq)
    if seq <= _read_radar_intent()['seq']: return
    if zoom != 'auto':
        if not re.fullmatch(r'[0-9]{1,2}', zoom, re.ASCII): return
        zoom = int(zoom)
        if not RADAR_MIN_ZOOM <= zoom <= RADAR_MAX_DESIRED_ZOOM: return
    if source not in ('auto','site','mosaic'): return
    if center != 'station':
        if not re.fullmatch(r'-?\d{1,3}(\.\d+)?,-?\d{1,3}(\.\d+)?', center, re.ASCII): return
        lat, lon = map(float, center.split(','))
        if not (-85.05112878 <= lat <= 85.05112878 and -180 <= lon <= 180): return
        center = dict(lat=lat, lon=lon)
    record = dict(seq=seq, zoom=zoom, source=source, center=center)
    marker = os.path.join(os.path.dirname(DATA), 'radar_intent')
    tmp = f'{marker}.tmp.{os.getpid()}'
    try:
        with open(tmp, 'w') as stream:
            json.dump(record, stream); stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, marker)
        # Durable preferences are persistence only once a runtime intent exists.
        _write_radar_zoom([str(zoom)])
        _write_radar_source([source])
    except OSError: pass
    finally:
        try: os.unlink(tmp)
        except OSError: pass


_camera_persist_timer = None


# Only a settled user commit may claim the acknowledged owner. Polls and reloads
# reconcile without changing ownership. The handler holds _count_lock across
# comparison, durable runtime intent replacement and activity publication.
_radar_owner = None
_radar_owner_seen = None
# Per-session fences for requests that arrive late. Stale takeovers are already
# refused by the ownership epoch (every transfer advances it, and a claim must
# echo the current one), and the owner's own fences live in _radar_owner, so an
# idle non-owner entry only guards the short window in which its requests can
# still be in flight. Entries idle longer than that age out; at capacity the
# least recently seen non-owner goes. Every page load is a new session, so a
# table that never evicted would eventually lock every new page out of control.
_RADAR_SESSIONS = 4096
_RADAR_SESSION_IDLE_SEC = 600
_radar_high_water = {}


def _prune_high_water(owner_session, incoming):
    now = time.monotonic()
    for key in [k for k, v in _radar_high_water.items()
                if k != owner_session and now - v.get('seen', now) > _RADAR_SESSION_IDLE_SEC]:
        del _radar_high_water[key]
    while incoming not in _radar_high_water and len(_radar_high_water) >= _RADAR_SESSIONS:
        victims = [k for k in _radar_high_water if k != owner_session]
        if not victims:
            return
        del _radar_high_water[min(victims, key=lambda k: _radar_high_water[k].get('seen', 0))]


def _camera_owner(record):
    return _radar_owner or dict(session=record.get('session', ''),
                               generation=record.get('generation', 0),
                               epoch=record.get('epoch', 0))


def _camera_transaction(activity, params):
    global _radar_owner, _radar_owner_seen
    if not _valid_radar_session(params):
        return False
    if len(params.get('radarGeneration', [])) != 1:
        return False
    if any(len(params[k]) != 1 for k in ('radarHeartbeat', 'radarCommit', 'radarPolicy', 'radarSource', 'radarClaim', 'radarClaimEpoch') if k in params):
        return False
    session = params['radarSession'][0]
    generation = params['radarGeneration'][0]
    heartbeat = params.get('radarHeartbeat', ['0'])[0]
    if not re.fullmatch(r'[0-9]{1,9}', generation) or not re.fullmatch(r'[0-9]{1,12}', heartbeat):
        return False
    generation, heartbeat = int(generation), int(heartbeat)
    old = _read_radar_intent()
    owner = _camera_owner(old)
    if owner['session'] and owner['session'] not in _radar_high_water:
        _prune_high_water(owner['session'], owner['session'])
        _radar_high_water[owner['session']] = dict(generation=owner['generation'], heartbeat=owner.get('heartbeat', 0),
                                                   seen=time.monotonic())
    high = _radar_high_water.get(session, {})
    floor = max(high.get('generation', 0), owner['generation'] if session == owner['session'] else 0)
    last_heartbeat = max(high.get('heartbeat', 0), owner.get('heartbeat', 0) if session == owner['session'] else 0)
    claiming = session != owner['session']
    commit = params.get('radarCommit') == ['1']
    if claiming:
        if (not commit or generation <= floor or heartbeat <= last_heartbeat
                or params.get('radarClaim') != [owner['session']]
                or params.get('radarClaimEpoch') != [str(owner.get('epoch', 0))]):
            return False
    elif generation < floor or heartbeat <= last_heartbeat:
        return False
    _prune_high_water(owner['session'], session)
    if commit and (claiming or generation > floor):
        if activity.get('moving') or 'zoom' not in activity or 'center' not in activity:
            return False
        if ('radarSource' in params and params['radarSource'] not in (['auto'], ['site'], ['mosaic'])) or params.get('radarPolicy') not in (['auto'], ['manual']):
            return False
        epoch = owner.get('epoch', 0) + int(claiming)
        if not _write_settled_camera(activity, params, epoch):
            return False
        owner = dict(session=session, generation=generation, epoch=epoch)
    elif generation != owner['generation']:
        return False
    if heartbeat:
        owner['heartbeat'] = heartbeat
    _radar_owner = owner
    _radar_owner_seen = time.monotonic()  # the owner is alive while its radar polls are accepted
    _radar_high_water[session] = dict(generation=generation, heartbeat=heartbeat, seen=time.monotonic())
    return True


def _write_settled_camera(activity, params, epoch=None):
    """Activity is the only live camera input; durable zoom is an output."""
    global _camera_persist_timer
    if activity.get('moving') or 'zoom' not in activity or 'center' not in activity:
        return
    old = _read_radar_intent()
    sources = params.get('radarSource', [])
    source = sources[0] if len(sources) == 1 and sources[0] in ('auto', 'site', 'mosaic') else old.get('source')
    if source is None:
        try:
            source = open(os.path.join(os.path.dirname(DATA), 'radar_source')).read().strip()
        except OSError:
            source = 'auto'
    if source not in ('auto', 'site', 'mosaic'):
        source = 'auto'
    record = dict(zoom=activity['zoom'], center=activity['center'], source=source, camera=True)
    if sources:
        record['sourceAcceptedAt'] = time.time()
    elif 'sourceAcceptedAt' in old or 'acceptedAt' in old:
        record['sourceAcceptedAt'] = old.get('sourceAcceptedAt', old.get('acceptedAt'))
    if 'radarSession' in params:
        record.update(session=params['radarSession'][0], generation=int(params['radarGeneration'][0]),
                      zoomPolicy=params['radarPolicy'][0], acceptedAt=time.time(), epoch=epoch)
    if all(old.get(k) == v for k, v in record.items()):
        return True
    record['seq'] = min(999999999999, max(old['seq']+1, int(time.time()*100)))
    marker = os.path.join(os.path.dirname(DATA), 'radar_intent')
    tmp = marker+'.tmp'
    try:
        with open(tmp, 'w') as stream:
            json.dump(record, stream)
        os.replace(tmp, marker)
    except OSError:
        return
    # Runtime intent is immediate. SD-card persistence follows a settled quiet
    # interval; a newer report cancels the pending older write.
    if _camera_persist_timer is not None:
        _camera_persist_timer.cancel()
    def persist():
        with _count_lock:
            if _read_radar_intent() == record:
                _write_radar_zoom(['auto' if record.get('zoomPolicy') == 'auto' else str(record['zoom'])])
                _write_radar_source([record['source']])
    _camera_persist_timer = threading.Timer(.25, persist)
    _camera_persist_timer.daemon = True
    _camera_persist_timer.start()
    return True


def _radar_activity(params):
    record=dict(at=time.time(),theme=params['radarTheme'][0],moving=params.get('radarMoving')==['1'])
    centers,zooms=params.get('radarGeoCenter',[]),params.get('radarGeoZoom',[])
    if len(centers)==len(zooms)==1:
        if (re.fullmatch(r'-?\d{1,3}(\.\d+)?,-?\d{1,3}(\.\d+)?',centers[0],re.ASCII)
                and re.fullmatch(r'[0-9]{1,2}',zooms[0],re.ASCII)):
            lat,lon=map(float,centers[0].split(','));zoom=int(zooms[0])
            if -85.05112878<=lat<=85.05112878 and -180<=lon<=180 and 4<=zoom<=10:
                record.update(center=dict(lat=lat,lon=lon),zoom=zoom)
    return record


class Handler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def __init__(self, *a, **k):
        super().__init__(*a, directory=WEB, **k)

    def do_POST(self):
        # The engine alone owns tile eviction. A page may report corruption or
        # an unexpected 404; bounded hints are validated against its own index.
        if self.path != '/radar-bad-tile' or not _is_loopback(self.client_address[0]):
            self.send_error(403)
            return
        try:
            length = int(self.headers.get('Content-Length','0'))
            if not 0 < length <= 256:
                raise ValueError('report length')
            path = self.rfile.read(length).decode('ascii').lstrip('/')
            if not re.fullmatch(r'radar/t/[a-f0-9]{12}/(?:iem-mrms-lcref|iem-nexrad-n0b|rainviewer)/(?:-|[A-Z0-9]{4}|M[a-f0-9]{24})/[0-9]{12}/[0-9]{1,2}/[0-9]{1,4}/[0-9]{1,4}\.png',path,re.ASCII):
                raise ValueError('tile path')
            with _count_lock:
                if not _allow_control_write(self.client_address[0], _bad_tile_buckets):
                    self.send_error(429, 'Control write rate exceeded')
                    return
                marker = os.path.join(os.path.dirname(DATA),'radar_bad_tiles')
                try:
                    with open(marker) as f: paths = json.load(f)
                    if not isinstance(paths,list): paths=[]
                except (OSError,ValueError): paths=[]
                paths = [p for p in paths[-127:] if p != path]+[path]
                with open(marker+'.tmp','w') as f: json.dump(paths,f)
                os.replace(marker+'.tmp',marker)
            self.send_response(204);self.send_header('Content-Length','0');self.end_headers()
        except (OSError,ValueError,UnicodeError):
            self.send_error(400)

    def do_GET(self):
        self._immutable_radar = False
        self._radar_throttled = False
        path, _, query = self.path.partition("?")
        _note_lan_viewer(self.client_address[0])
        if path == "/health":
            return self._health()
        if path == "/wx.json":
            global _polls, _renders
            address = self.client_address[0]
            panel, controller = _is_loopback(address), _is_controller(address)
            params = parse_qs(query, keep_blank_values=True)
            viewed_radar = params.get('view') == ['radar']
            with _count_lock:
                _polls += 1
                # A poll may expire a source or update viewing without a
                # camera commit. Gate all its side effects, never the read.
                admitted = controller and _allow_control_write(address)
                self._radar_throttled = controller and not admitted
                view_accepted = False
                if panel and params.get('r') == ['1']:
                    _renders += 1
                if admitted:
                    _expire_radar_source()
                    camera_report = viewed_radar and params.get('radarTheme') in (['paper'], ['night'])
                    ordered = 'radarSession' in params
                    accepted = _camera_transaction(_radar_activity(params), params) if ordered and camera_report else panel and not ordered and _radar_owner is None and not _read_radar_intent().get('session')
                    view_accepted = panel and _view_transaction(params)
                    if view_accepted:
                        _write_radar_viewing(viewed_radar)
                    if _valid_radar_session(params):
                        _write_radar_preference('radar_smooth', params.get('radarSmooth', []))
                    if camera_report and view_accepted:
                        marker=os.path.join(os.path.dirname(DATA),'radar_activity');tmp=marker+'.tmp'
                        try:
                            with open(tmp,'w') as f:json.dump(_radar_activity(params),f)
                            os.replace(tmp,marker)
                        except OSError:pass
                    if camera_report and accepted and not ordered:
                        _write_settled_camera(_radar_activity(params), params)
                    elif not ordered and accepted and not _read_radar_intent().get('camera') and 'radarSeq' in params:
                        _write_radar_intent(params)  # older pages, until the first camera report
                    elif not ordered and accepted and set(_read_radar_intent()) == {'seq'}:
                        _write_radar_zoom(params.get('radarZoom', []))
                        _write_radar_center(params.get('radarCenter', []))
                        _write_radar_source(params.get('radarSource', []))
                if admitted and params.get('touch') == ['1']:
                    _note_presence()
                if view_accepted and viewed_radar:
                    # Share only a timestamp with the emitter. Serialize writers
                    # and replace atomically so it never reads a partial epoch.
                    marker = os.path.join(os.path.dirname(DATA), "radar_viewed")
                    tmp = f"{marker}.tmp.{os.getpid()}"
                    try:
                        with open(tmp, "w") as f:
                            f.write(str(time.time()))
                        os.replace(tmp, marker)
                    except OSError:
                        pass  # an optional demand hint must never break polling
                    finally:
                        try:
                            os.unlink(tmp)
                        except OSError:
                            pass
        return super().do_GET()

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError,ConnectionResetError):
            pass  # ordinary tab closure/cancel, not an unbounded server traceback

    def send_head(self):
        path = self.path.split('?')[0]
        local = self.translate_path(path)
        tile=re.fullmatch(r'/radar/t/([a-f0-9]{12})/(iem-mrms-lcref|iem-nexrad-n0b|rainviewer)/(-|[A-Z0-9]{4}|M[a-f0-9]{24})/[0-9]{12}/([0-9]{1,2})/([0-9]{1,4})/([0-9]{1,4})\.png',path,re.ASCII)
        geo=re.fullmatch(r'/radar/geo/([a-f0-9]{12})/(paper|night)/([0-9]{1,2})/([0-9]{1,4})/([0-9]{1,4})\.png',path,re.ASCII)
        sites=re.fullmatch(r'/radar/sites-([a-f0-9]{12})\.json',path,re.ASCII)
        def revision(kind,value):
            try:
                with open(os.path.join(WEB,'radar','.'+kind+'-revision')) as f:return f.read()==value
            except OSError:return False
        immutable=bool(tile and (not tile[3].startswith('M') or tile[2]=='iem-nexrad-n0b' and revision('native',tile[1])) and (revision('tile',tile[1]) or revision('smooth',tile[1]) or revision('native',tile[1])) and 4<=int(tile[4])<=10 and int(tile[5])<2**int(tile[4]) and int(tile[6])<2**int(tile[4]) or
                       geo and revision('geo',geo[1]) and 4<=int(geo[3])<=10 and int(geo[4])<2**int(geo[3]) and int(geo[5])<2**int(geo[3]) or
                       sites and revision('sites',sites[1]))
        self._immutable_radar=immutable and os.path.isfile(local)
        if path.startswith(('/radar/t/','/radar/geo/','/radar/sites')) and not self._immutable_radar:
            self.send_error(404,'File not found');return None
        if self._immutable_radar:
            stat=os.stat(local);os.utime(local,ns=(time.time_ns(),stat.st_mtime_ns))
        return super().send_head()

    def end_headers(self):
        if self.path.split('?')[0] == '/wx.json' and _is_controller(self.client_address[0]):
            with _count_lock:
                record = _read_radar_intent()
                owner = _camera_owner(record)
                try:
                    with open(os.path.join(os.path.dirname(DATA), 'radar_smooth')) as stream:
                        smooth = stream.read(128).strip() == 'on'
                except (OSError, UnicodeError):
                    smooth = False
                if getattr(self, '_radar_throttled', False):
                    self.send_header('X-Radar-Throttled', '1')
                self.send_header('X-Radar-Panel', '1' if _is_loopback(self.client_address[0]) else '0')
                self.send_header('X-Radar-Smooth', 'on' if smooth else 'off')
                self.send_header('X-View-Session', _view_owner['session'] if _view_owner else '')
                # ownerIdleSec lets the panel tell a live owner (a phone still
                # watching a storm) from a dead one (closed or hidden tab).
                idle = None if _radar_owner_seen is None else round(time.monotonic() - _radar_owner_seen, 1)
                self.send_header('X-Radar-Intent', json.dumps(dict(intent=record, acceptedGeneration=owner['generation'],
                    ownerIdleSec=idle, **owner), separators=(',', ':')))
        if getattr(self, '_immutable_radar', False):
            self.send_header('Cache-Control', 'public, max-age=31536000, immutable')
        super().end_headers()

    def log_error(self,format,*args):
        if self.path.startswith(('/radar/t/','/radar/geo/')):return
        super().log_error(format,*args)

    def log_request(self, code="-", size="-"):
        # drop the ~2s wx.json/index poll churn (it grew the log unbounded on
        # tmpfs). Keep errors and any other path so real problems still surface.
        p = self.path.split("?")[0]
        if p.startswith(("/radar/t/", "/radar/geo/")): return
        if str(code) in ("200", "304") and p in ("/wx.json", "/index.html", "/health", "/"):
            return
        super().log_request(code, size)

    def _health(self):
        h = {"status": "ok", "polls": _polls, "renders": _renders}
        try:
            with open(DATA) as f:
                d = json.load(f)
            age = time.time() - float(d.get("ts", 0))
            h["dataAgeSec"]      = round(age, 1)
            h["radar"] = (d.get("radar") or {}).get("health", dict(lastSuccessTs=None,
                successRate60s=None, hedges=0, retries=0, breaker="closed", lastError=None))
            h["station"]         = d.get("station")
            h["temp"]            = d.get("temp")
            h["updateAvailable"] = d.get("updateAvailable")
            obs_age = d.get("obsAgeSec")
            # a bool is not an age, nor is NaN/inf: treat those as absent
            if isinstance(obs_age, bool) or not isinstance(obs_age, (int, float)) or not math.isfinite(obs_age):
                obs_age = None
            else:
                obs_age = float(obs_age) + max(age, 0.0)
            h["obsAgeSec"] = round(obs_age, 1) if obs_age is not None else None
            # Order matters: a stalled engine is the bigger fault, and its stale
            # file makes every observation in it look old too.
            if age > STALE_SEC:
                h["status"], h["reason"] = "stale", "engine stalled"
            elif obs_age is not None and obs_age > OBS_STALE_SEC:
                h["status"], h["reason"] = "degraded", "sensor silent"
        except Exception as e:                                            # noqa: BLE001
            h["status"] = "error"
            h["reason"] = h["error"] = str(e)
        try:
            body = json.dumps(h, allow_nan=False).encode()
        except ValueError as e:                                          # a non-finite crept in
            h["status"] = "error"
            body = json.dumps({"status": "error", "reason": str(e)}).encode()
        self.send_response(200 if h["status"] == "ok" else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    with Server((BIND, PORT), Handler) as httpd:
        httpd.serve_forever()
