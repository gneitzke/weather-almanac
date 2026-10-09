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
#   radar: the emitter's radar-health.json (written beside wx.json), plus
#     available:true and fileAgeSec. Missing or unreadable -> {available:false,
#     reason}. Radar health is diagnostics: it never changes `status` or the code.
#
# Bind stays on 127.0.0.1 by default (chromium is local; no data leaves the box).
# Set WFP_BIND=0.0.0.0 to expose /health (and the page) to the LAN for remote
# monitoring and radar control — private-network browsers share the panel view.
# LAN mode protections (all also active on loopback):
#   - Host allow-list (DNS rebinding): IP literals, localhost, this machine's
#     hostname and hostname.local, plus WFP_ALLOWED_HOSTS (comma separated).
#   - Control side effects of the wx.json poll (session/touch/view/radarSmooth/
#     camera) are dropped for cross-site requests (Sec-Fetch-Site, else Origin
#     vs Host); the read is still served.
#   - Connections: LAN clients share a bounded pool (per client and in total);
#     loopback has its own pool, so LAN clients cannot starve the kiosk. Idle
#     keep-alive, request-header and I/O socket deadlines bound every connection.
#   - No directory listings.
#   - Smooth changes need the current camera owner, a small per-client budget,
#     and reach durable storage debounced. Durable (fsync) writes run only on a
#     dedicated writer thread: no request, not even the one that made the
#     change, waits on the SD card.
import http.server, socketserver, json, math, os, time, threading, re, io, zlib, ipaddress, socket
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
# Extra Host names this server answers to (a DNS name pointing at the Pi, say).
ALLOWED_HOSTS = frozenset(h.strip().lower().rstrip('.') for h in os.environ.get("WFP_ALLOWED_HOSTS", "").split(',') if h.strip())

# Socket deadlines. The kiosk polls every 2 s (300 ms while steering), so an idle
# keep-alive connection past 15 s is abandoned; a request's line and headers must
# arrive within 10 s of its first byte however slowly they trickle in; any one
# read or write of the body/response then gets 30 s.
KEEPALIVE_IDLE_SEC, HEADER_DEADLINE_SEC, IO_TIMEOUT_SEC = 15.0, 10.0, 30.0
# Concurrent connections. Loopback (the kiosk) has its own pool; LAN clients
# share the other, with a per-address cap so one device cannot hold all of it.
LOCAL_CONNECTIONS, LAN_CONNECTIONS, LAN_CLIENT_CONNECTIONS = 64, 48, 12

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
# Smooth is a durable (SD card) preference: a change costs a write, so a client
# gets a few in a row and then one every ten seconds. No-op repeats are free.
_SMOOTH_RATE, _SMOOTH_BURST, _SMOOTH_DEBOUNCE_SEC = 0.1, 3.0, 1.0
_smooth_buckets = {}


def _allow_control_write(address, buckets=None, rate=None, burst=None):
    if buckets is None:
        buckets = _control_buckets
    rate = _CONTROL_RATE if rate is None else rate
    burst = _CONTROL_BURST if burst is None else burst
    now = time.monotonic()
    key = str(_client_ip(address))
    if key not in buckets:
        for stale, (tokens, at) in list(buckets.items()):
            # Idle long enough to have refilled completely: forgetting it is free.
            if now - at >= max(60, burst/rate):
                del buckets[stale]
        if len(buckets) >= _CONTROL_CLIENTS:
            return False
    tokens, at = buckets.get(key, (burst, now))
    tokens = min(burst, tokens + max(0, now-at)*rate)
    allowed = tokens >= 1
    buckets[key] = (tokens-1 if allowed else tokens, now)
    return allowed


def _host_name(value):
    """The host part of a Host header, lower-cased, or None when malformed."""
    value = value.strip().lower()
    if value.startswith('['):
        host, bracket, rest = value[1:].partition(']')
        if not bracket or (rest and not re.fullmatch(r':[0-9]{1,5}', rest)):
            return None
        return host
    host, _, port = value.partition(':')
    if port and not re.fullmatch(r'[0-9]{1,5}', port):
        return None
    return host.rstrip('.')


def _host_allowed(value):
    """DNS rebinding guard: a page from another name must not reach this server.

    IP literals (how LAN browsers and the kiosk address the Pi), localhost, this
    machine's own name and its mDNS name, and WFP_ALLOWED_HOSTS. A request
    without Host (HTTP/1.0 tools) carries no foreign name and is allowed.
    """
    if value is None:
        return True
    host = _host_name(value)
    if not host:
        return False
    try:
        ipaddress.ip_address(host.split('%', 1)[0])
        return True
    except ValueError:
        pass
    own = socket.gethostname().lower().rstrip('.')
    short = own.split('.', 1)[0]
    return host in {'localhost', own, short, own+'.local', short+'.local'} or host in ALLOWED_HOSTS


def _cross_site(headers):
    """True when a browser says this request came from another origin.

    Fetch metadata first (Chromium 76+, Firefox 90+, Safari 16.4+): only the
    page's own origin, or a user's direct navigation, may steer. Without it,
    an Origin header must name this Host. Requests with neither are not from a
    cross-origin page of a current browser (curl, the tests, old engines).
    """
    site = headers.get('Sec-Fetch-Site')
    if site is not None:
        return site.strip().lower() not in ('same-origin', 'none')
    origin, host = headers.get('Origin'), headers.get('Host')
    if origin is None:
        return False
    return host is None or origin.strip().lower() not in ('http://'+host.strip().lower(), 'https://'+host.strip().lower())


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
    global _presence_at, _pref_seq
    now = time.time()
    with _presence_lock:
        if 0 <= now - _presence_at < 10:
            return
        with _pref_lock:
            if 'radar_source' in _pref_pending:
                # A source expiry (or choice) is still on its way to disk. The
                # engine must never see this touch beside the old choice - it
                # would renew the expired lease - so the touch lands after it,
                # in order, on the writer thread.
                _pref_seq += 1
                _pref_pending['presence'] = (_pref_seq, str(now), time.monotonic())
                _start_pref_writer()
                _pref_wake.notify_all()
                _presence_at = now
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


def _preference_value(name, values):
    """The canonical marker text for one valid request value, else None."""
    if name not in ('radar_zoom', 'radar_source', 'radar_center', 'radar_smooth') or len(values) != 1:
        return None
    value = values[0]
    if name == 'radar_center':
        if value != 'station':
            if not re.fullmatch(r'-?\d{1,3}(\.\d+)?,-?\d{1,3}(\.\d+)?', value, re.ASCII):
                return None
            lat, lon = map(float, value.split(','))
            if not (-85.05112878 <= lat <= 85.05112878 and -180 <= lon <= 180):
                return None
            # Keep canonical floats in the decimal grammar (str() can emit 1e-10).
            value = ','.join(format(Decimal(str(n)), 'f') for n in (lat, lon))
    elif name == 'radar_smooth':
        if value not in ('on', 'off'):
            return None
    elif name == 'radar_source':
        if value not in ('auto', 'mosaic', 'site'):
            return None
    elif value != 'auto':
        if not re.fullmatch(r'[0-9]{1,2}', value):
            return None
        level = int(value)
        if not RADAR_MIN_ZOOM <= level <= RADAR_MAX_DESIRED_ZOOM:
            return None
        value = str(level)
    return value


def _preference_path(name):
    # The kiosk links this sibling to durable station storage before startup.
    # Resolve the link so replacement updates its target, not the link.
    marker = os.path.join(os.path.dirname(DATA), name)
    # Pan belongs to tmpfs. Never follow a durable link, even if one was
    # accidentally installed: atomic replacement replaces the link itself.
    return marker if name == 'radar_center' else os.path.realpath(marker)


# Preference writes are staged in memory and written (with fsync, on the SD
# card) by one dedicated writer thread. No request thread - not even the one
# whose request staged the value - ever waits on that write: an SD stall
# inside a request stalled its response, and inside _count_lock every
# client's poll. A sequence number per staging keeps the newest decision when
# a value is restaged during its write; readers in this process see staged
# values before they land. Debounced values wait for their due time; shutdown
# writes everything still staged.
_pref_lock = threading.Lock()       # guards the fields below; no I/O under it
_pref_wake = threading.Condition(_pref_lock)
_pref_io_lock = threading.Lock()    # serializes durable writes; never taken by a request thread
_pref_pending = {}                  # name -> (seq, value, due on the monotonic clock)
_pref_seq = 0
_pref_writer = None
_pref_closing = False


def _stage_preference(name, values, delay=0.0):
    """Record a valid preference to be written. Returns the canonical value or None."""
    global _pref_seq
    value = _preference_value(name, values)
    if value is None:
        return None
    with _pref_lock:
        _pref_seq += 1
        # Debounce: a newer staging replaces the value and moves `due`.
        _pref_pending[name] = (_pref_seq, value, time.monotonic()+delay)
        _start_pref_writer()
        _pref_wake.notify_all()
    return value


def _start_pref_writer():
    """Caller holds _pref_lock."""
    global _pref_writer
    if _pref_writer is None or not _pref_writer.is_alive():
        _pref_writer = threading.Thread(target=_pref_writer_loop, name='preference-writer', daemon=True)
        _pref_writer.start()


def _pref_writer_loop():
    while True:
        with _pref_lock:
            while True:
                if _pref_closing:
                    return  # _close_preferences writes what remains
                now = time.monotonic()
                due = min((at for _, _, at in _pref_pending.values()), default=None)
                if due is not None and due <= now:
                    break
                # The clock is read again on every wake; a timeout only re-checks.
                _pref_wake.wait(None if due is None else min(max(due-now, .01), 1.0))
        _flush_preferences()


def _flush_preferences(force=False):
    """Write due (or, with force, all) staged preferences, waiting for any
    write in progress. Only the writer thread, shutdown and tests call it."""
    with _pref_io_lock:
        while True:
            with _pref_lock:
                now = time.monotonic()
                due = [(name, seq, value) for name, (seq, value, at) in _pref_pending.items() if force or at <= now]
            if not due:
                return
            for name, seq, value in sorted(due, key=lambda item: item[1]):  # staging order
                _persist_preference(name, value)
                with _pref_lock:
                    # A failed write is dropped like it always was: polling never fails here.
                    if _pref_pending.get(name, (None,))[0] == seq:
                        del _pref_pending[name]


def _close_preferences(timeout=5.0):
    """Shutdown: stop the writer and write every staged value now, debounced or not."""
    global _pref_closing
    with _pref_lock:
        _pref_closing = True
        writer = _pref_writer
        _pref_wake.notify_all()
    if writer is not None and writer is not threading.current_thread():
        writer.join(timeout)
    _flush_preferences(force=True)


def _read_preference(name):
    """The effective preference: a staged value, else the marker's text, else None."""
    with _pref_lock:
        pending = _pref_pending.get(name)
    if pending is not None:
        return pending[1]
    limit = 1024 if name == 'radar_center' else 128
    try:
        with open(os.path.join(os.path.dirname(DATA), name)) as stream:
            text = stream.read(limit)
    except (OSError, UnicodeError):
        return None
    return text.strip() if len(text) < limit else None


def _write_radar_preference(name, values):
    """Validated preference write, staged for the writer thread."""
    _stage_preference(name, values)


def _persist_preference(name, value):
    if name == 'presence':
        _persist_runtime(name, value)
        return
    marker = _preference_path(name)
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


def _persist_runtime(name, value):
    """A runtime (tmpfs) marker the writer orders after a durable one: atomic, no fsync."""
    marker = os.path.join(os.path.dirname(DATA), name)
    tmp = f'{marker}.tmp.{os.getpid()}'
    try:
        with open(tmp, 'w') as f:
            f.write(value)
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
    requested = intent['source'] if intent else _read_preference('radar_source')
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
            json.dump(record, stream)  # runtime tmpfs: atomic, no fsync
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
        source = _read_preference('radar_source')
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
        with _count_lock:  # staged preferences land on the writer thread
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


def _stage_smooth(address, values):
    """Caller holds _count_lock and has accepted the owner's camera transaction."""
    value = _preference_value('radar_smooth', values)
    if value is None or value == _read_preference('radar_smooth'):
        return
    if _allow_control_write(address, _smooth_buckets, _SMOOTH_RATE, _SMOOTH_BURST):
        _stage_preference('radar_smooth', [value], delay=_SMOOTH_DEBOUNCE_SEC)


def _reject_constant(name):
    raise ValueError(f'non-finite number {name}')


def _finite_float(text):
    # json turns an overflowing literal (1e999) into infinity without calling
    # parse_constant; refuse it here like NaN and Infinity.
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f'non-finite number {text}')
    return value


def _check_finite(value, depth=0):
    """Every number in a JSON value is finite and fits a float. Raises ValueError."""
    if depth > 64:
        raise ValueError('nested too deeply')
    if isinstance(value, dict):
        for item in value.values():
            _check_finite(item, depth+1)
    elif isinstance(value, list):
        for item in value:
            _check_finite(item, depth+1)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f'non-finite number {value}')
    elif isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2**53:
        raise ValueError('integer out of range')


def _radar_health():
    """The emitter's radar-health.json beside wx.json, for /health.

    Missing, unreadable, non-object or non-finite content (NaN, Infinity, an
    overflowing literal such as 1e999, an integer no float can hold) is
    reported as unavailable with the reason. Everything is decided inside this
    boundary, and the result always serializes, so radar diagnostics can never
    change /health's status or HTTP code.
    """
    path = os.path.join(os.path.dirname(DATA), 'radar-health.json')
    try:
        with open(path) as stream:
            health = json.load(stream, parse_constant=_reject_constant, parse_float=_finite_float)
        if not isinstance(health, dict):
            return dict(available=False, reason='radar-health.json is not an object')
        _check_finite(health)
        written = health.get('writtenTs')
        age = (round(time.time() - written, 1) if isinstance(written, (int, float))
               and not isinstance(written, bool) else None)
        result = dict(health, available=True, fileAgeSec=age)
        json.dumps(result, allow_nan=False)
        return result
    except FileNotFoundError:
        return dict(available=False, reason='radar-health.json missing')
    except (OSError, UnicodeError, ValueError, TypeError, OverflowError, RecursionError) as error:
        return dict(available=False, reason=f'radar-health.json unreadable: {error}'[:300])


class _DeadlineReader(io.RawIOBase):
    """Socket reads under one absolute deadline (None: the socket's own timeout).

    A per-read timeout alone lets a client trickle a header one byte at a time
    forever; every recv here gets only what remains of the request's deadline.
    """
    def __init__(self, sock):
        self.sock, self.deadline = sock, None

    def readable(self):
        return True

    def readinto(self, buffer):
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout('request deadline')
            self.sock.settimeout(remaining)
        return self.sock.recv_into(buffer)


class Handler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = IO_TIMEOUT_SEC
    def __init__(self, *a, **k):
        super().__init__(*a, directory=WEB, **k)

    def setup(self):
        super().setup()
        # Requests are read through the deadline reader; responses keep wfile.
        self.rfile.close()
        self._reader = _DeadlineReader(self.connection)
        self.rfile = io.BufferedReader(self._reader)

    def handle_one_request(self):
        # Wait for the next request's first byte for at most the keep-alive
        # idle time; an idle close is routine, not an error to log.
        self._reader.deadline = time.monotonic() + KEEPALIVE_IDLE_SEC
        try:
            if not self.rfile.peek(1):
                self.close_connection = True
                return
        except OSError:
            self.close_connection = True
            return
        self._reader.deadline = time.monotonic() + HEADER_DEADLINE_SEC
        super().handle_one_request()

    def parse_request(self):
        parsed = super().parse_request()
        # Headers are in: the body and response get ordinary I/O timeouts.
        self._reader.deadline = None
        self.connection.settimeout(IO_TIMEOUT_SEC)
        if not parsed:
            return False
        if not _host_allowed(self.headers.get('Host')):
            self.send_error(421, 'Unknown host')
            return False
        return True

    def list_directory(self, path):
        self.send_error(404, 'File not found')
        return None

    def do_POST(self):
        # The engine alone owns tile eviction. A page may report corruption or
        # an unexpected 404; bounded hints are validated against its own index.
        if (self.path != '/radar-bad-tile' or not _is_loopback(self.client_address[0])
                or _cross_site(self.headers)):
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
            # A page on another origin can make a browser send this GET (an
            # image, a script tag) but must not steer the panel through it.
            same_site = not _cross_site(getattr(self, 'headers', None) or {})
            with _count_lock:  # staged preferences land on the writer thread
                _polls += 1
                # A poll may expire a source or update viewing without a
                # camera commit. Gate all its side effects, never the read.
                admitted = controller and same_site and _allow_control_write(address)
                self._radar_throttled = controller and same_site and not admitted
                view_accepted = False
                if panel and same_site and params.get('r') == ['1']:
                    _renders += 1
                if admitted:
                    _expire_radar_source()
                    camera_report = viewed_radar and params.get('radarTheme') in (['paper'], ['night'])
                    ordered = 'radarSession' in params
                    accepted = _camera_transaction(_radar_activity(params), params) if ordered and camera_report else panel and not ordered and _radar_owner is None and not _read_radar_intent().get('session')
                    view_accepted = panel and _view_transaction(params)
                    if view_accepted:
                        _write_radar_viewing(viewed_radar)
                    if ordered and camera_report and accepted:
                        # Smooth changes what everyone sees: only the camera
                        # owner whose transaction was just accepted may change it.
                        _stage_smooth(address, params.get('radarSmooth', []))
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
        if self._immutable_radar and geo:
            # Geography prunes by served atime (radar_basemap.prune). Radar tiles
            # evict in write order, so serving them costs no SD metadata write.
            stat=os.stat(local);os.utime(local,ns=(time.time_ns(),stat.st_mtime_ns))
        return super().send_head()

    def end_headers(self):
        if self.path.split('?')[0] == '/wx.json' and _is_controller(self.client_address[0]):
            with _count_lock:
                record = _read_radar_intent()
                owner = _camera_owner(record)
                smooth = _read_preference('radar_smooth') == 'on'
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
        if getattr(self,'path','').startswith(('/radar/t/','/radar/geo/')):return
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
        # Read after the verdict: radar diagnostics never change status or code.
        h["radar"] = _radar_health()
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
    """Threaded server with connection admission in the accept loop.

    Admission is decided before a thread exists: over its pool, a connection
    is closed at once. Loopback has its own pool, so a LAN flood can delay the
    kiosk by at most an accept, never lock it out.
    """
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, *args, **kwargs):
        self._admission = threading.Lock()
        self._local = self._lan = 0
        self._clients = {}
        super().__init__(*args, **kwargs)

    def _admit(self, address):
        local, key = _is_loopback(address), str(_client_ip(address))
        with self._admission:
            if local:
                if self._local >= LOCAL_CONNECTIONS:
                    return None
                self._local += 1
            else:
                if self._lan >= LAN_CONNECTIONS or self._clients.get(key, 0) >= LAN_CLIENT_CONNECTIONS:
                    return None
                self._lan += 1
                self._clients[key] = self._clients.get(key, 0) + 1
        return local, key

    def _release(self, ticket):
        local, key = ticket
        with self._admission:
            if local:
                self._local -= 1
            else:
                self._lan -= 1
                self._clients[key] -= 1
                if not self._clients[key]:
                    del self._clients[key]

    def process_request(self, request, client_address):
        ticket = self._admit(client_address[0])
        if ticket is None:
            self.shutdown_request(request)
            return
        try:
            threading.Thread(target=self._serve_admitted, args=(request, client_address, ticket),
                             daemon=True).start()
        except BaseException:
            self._release(ticket)
            self.shutdown_request(request)
            raise

    def _serve_admitted(self, request, client_address, ticket):
        try:
            self.process_request_thread(request, client_address)
        finally:
            self._release(ticket)


def _terminate(signum, frame):
    raise SystemExit(0)  # unwinds serve_forever so staged preferences are written


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGTERM, _terminate)
    try:
        with Server((BIND, PORT), Handler) as httpd:
            httpd.serve_forever()
    finally:
        _close_preferences()
