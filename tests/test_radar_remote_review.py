"""Remote review regressions: real handlers without sockets and production JS in Node."""
import io
import ipaddress
import json

import pytest

from tests.test_radar_remote_serve import server, request, camera, A, B  # noqa: F401
from tests.test_radar_remote_page import run_remote


def commit(server, session=A, generation=1, **extra):
    owner = server._camera_owner(server._read_radar_intent())
    return dict(camera(session, generation, owner['session']),
                radarClaimEpoch=owner['epoch'], **extra)


def test_epoch_and_session_fences_defeat_aba_replay(server):
    first = json.loads(request(server, **commit(server))['X-Radar-Intent'])
    assert first['epoch'] == first['intent']['epoch'] == 1
    delayed = commit(server, B, radarGeoZoom=5)
    request(server, **delayed)
    request(server, **commit(server, A, 2, radarGeoZoom=9))
    accepted = server._read_radar_intent()
    assert accepted['epoch'] == 3
    # Same old camera, both its original claim and a claim refreshed to A's new epoch.
    for packet in (delayed, dict(delayed, radarClaimEpoch=3, radarHeartbeat=99),
                   dict(delayed, radarClaimEpoch=3, radarGeneration=2)):
        request(server, **packet)
        assert server._read_radar_intent() == accepted
    # A never-accepted older generation also cannot use an obsolete epoch.
    request(server, **dict(delayed, radarGeneration=2, radarHeartbeat=2))
    assert server._read_radar_intent() == accepted
    ack = json.loads(request(server, **commit(server, B, 2))['X-Radar-Intent'])
    assert ack['epoch'] == 4 and ack['session'] == B
    request(server, **dict(commit(server, B, 2), radarHeartbeat=3, radarGeoZoom=4))
    assert server._read_radar_intent() == ack['intent']  # idempotent generation


@pytest.mark.parametrize('epoch', [None, '', '01', '-1', ['1', '1'], '0'])
def test_claim_requires_exact_single_epoch(server, epoch):
    request(server, **commit(server))
    pending = commit(server, B)
    if epoch is None:
        pending.pop('radarClaimEpoch')
    else:
        pending['radarClaimEpoch'] = epoch
    request(server, **pending)
    assert server._read_radar_intent()['session'] == A


def test_high_water_evicts_idle_sessions_without_reopening_replay(server, monkeypatch):
    """Every page load is a session; a full table must not lock new pages out,
    and eviction must not let a stale takeover through (the epoch refuses it)."""
    monkeypatch.setattr(server, '_RADAR_SESSIONS', 2)
    request(server, **commit(server))            # A owns, epoch 0
    stale_b = commit(server, B)                  # B's takeover, echoing epoch 0 ...
    request(server, **stale_b)                   # ... accepted: B owns, epoch 1
    C = 'remote-session-c-123'
    request(server, **commit(server, C))         # table full: A (idle non-owner) is evicted
    assert server._read_radar_intent()['session'] == C
    assert len(server._radar_high_water) <= 2
    request(server, **stale_b)                   # B's delayed original arrives late
    assert server._read_radar_intent()['session'] == C  # stale epoch: refused
    request(server, **commit(server, A, 2))      # A returns with a fresh claim
    assert server._read_radar_intent()['session'] == A


def test_idle_sessions_age_out(server, monkeypatch):
    request(server, **commit(server))
    request(server, **commit(server, B))
    clock = [server.time.monotonic() + server._RADAR_SESSION_IDLE_SEC + 1]
    monkeypatch.setattr(server.time, 'monotonic', lambda: clock[0])
    request(server, **commit(server, 'remote-session-c-123'))
    assert A not in server._radar_high_water      # idle non-owner aged out
    assert 'remote-session-c-123' in server._radar_high_water


def test_restarted_server_seeds_persisted_generation_and_epoch(server):
    request(server, **commit(server, A, 5))
    server._radar_owner = None
    server._radar_high_water.clear()
    request(server, **commit(server, B))
    before = server._read_radar_intent()
    request(server, **commit(server, A, 4, radarHeartbeat=99))
    assert server._read_radar_intent() == before
    assert before['epoch'] == 2


def panel_report(seq, **extra):
    return dict(radarSession=B, radarGeneration=0, radarHeartbeat=seq,
                viewSession=B, viewSeq=seq, view='radar', radarTheme='night',
                radarGeoZoom=9, radarGeoCenter='48,-123', **extra)


@pytest.mark.parametrize('remote_owner', [False, True])
def test_panel_activity_tracks_ordered_view_even_as_follower(server, tmp_path, remote_owner):
    if remote_owner:
        request(server, **commit(server))
    before = server._read_radar_intent()
    request(server, '127.0.0.1', **panel_report(1))
    assert server._read_radar_intent() == before
    activity = json.loads((tmp_path/'radar_activity').read_text())
    assert activity['theme'] == 'night' and activity['zoom'] == 9
    assert activity['center'] == {'lat': 48, 'lon': -123}
    request(server, **commit(server, A, 2, radarTheme='paper'))
    assert json.loads((tmp_path/'radar_activity').read_text()) == activity
    request(server, '127.0.0.1', **dict(panel_report(1), radarTheme='paper'))
    assert json.loads((tmp_path/'radar_activity').read_text()) == activity
    assert before.get('session') == (A if remote_owner else None)


def test_late_radar_view_cannot_refresh_hint_or_activity_after_leaving(server, tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(server.time, 'time', lambda: now[0])
    request(server, '127.0.0.1', **panel_report(1))
    activity = (tmp_path/'radar_activity').read_text()
    viewed = (tmp_path/'radar_viewed').read_text()
    request(server, '127.0.0.1', viewSession=B, viewSeq=3, view='none')
    now[0] += 100
    request(server, '127.0.0.1', **panel_report(2))
    assert not (tmp_path/'radar_viewing').exists()
    assert (tmp_path/'radar_viewed').read_text() == viewed
    assert (tmp_path/'radar_activity').read_text() == activity
    request(server, '127.0.0.1', **panel_report(4))
    assert float((tmp_path/'radar_viewed').read_text()) == now[0]


@pytest.mark.parametrize('params', [
    dict(view='radar', radarTheme='paper', radarGeoZoom=5, radarGeoCenter='10,10'),
    dict(radarSeq=5, radarZoom=5, radarSource='mosaic', radarCenter='10,10'),
    dict(radarZoom=5, radarSource='mosaic', radarCenter='10,10'),
])
def test_all_legacy_camera_paths_are_panel_only(server, tmp_path, params):
    durable = tmp_path/'durable_zoom'
    durable.write_text('8\n')
    (tmp_path/'radar_zoom').symlink_to(durable)
    request(server, **params)
    assert server._read_radar_intent() == {'seq': 0}
    assert durable.read_text() == '8\n'
    assert not (tmp_path/'radar_center').exists()
    assert not (tmp_path/'radar_source').exists()
    request(server, '127.0.0.1', **params)
    if server._camera_persist_timer:
        server._camera_persist_timer.function()
    assert durable.read_text().strip() == '5'


def test_throttled_reads_signal_retry_but_still_count_panel_renders(server, tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(server.time, 'monotonic', lambda: now[0])
    request(server, **commit(server))
    pending = commit(server, B)
    for _ in range(int(server._CONTROL_BURST)):
        server._allow_control_write('127.0.0.1')
    headers = request(server, '::ffff:127.0.0.1', **pending, touch=1, r=1)
    assert headers['X-Radar-Throttled'] == '1'
    assert json.loads(headers['X-Radar-Intent'])['session'] == A
    assert server._renders == 1 and not (tmp_path/'presence').exists()
    now[0] += 1
    headers = request(server, '127.0.0.1', **pending, touch=1)
    assert 'X-Radar-Throttled' not in headers
    assert json.loads(headers['X-Radar-Intent'])['session'] == B
    assert (tmp_path/'presence').exists()


def test_bad_tile_bucket_exhaustion_does_not_starve_controls(server, monkeypatch):
    monkeypatch.setattr(server.time, 'monotonic', lambda: 100.0)
    for _ in range(int(server._CONTROL_BURST)):
        assert server._allow_control_write('127.0.0.1', server._bad_tile_buckets)
    h = object.__new__(server.Handler)
    h.client_address, h.path = ('::ffff:127.0.0.1', 1), '/radar-bad-tile'
    body = b'radar/t/123456789abc/iem-mrms-lcref/-/202609251200/8/1/1.png'
    h.rfile, h.headers = io.BytesIO(body), {'Content-Length': str(len(body))}
    errors = []
    h.send_error = lambda code, *args: errors.append(code)
    h.do_POST()
    assert errors == [429]
    headers = request(server, '127.0.0.1', **commit(server))
    assert 'X-Radar-Throttled' not in headers
    assert server._read_radar_intent()['session'] == A


def test_default_routes_are_decoded_and_forwarding_gateways_are_read_only(server, tmp_path, monkeypatch):
    route = tmp_path/'route'
    route.write_text('Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n'
                     'eth0 00000000 0101A8C0 0003 0 0 100 00000000 0 0 0\n'
                     'wlan0 00000000 0100000A 0003 0 0 200 00000000 0 0 0\n'
                     'eth0 0001A8C0 00000000 0001 0 0 0 00FFFFFF 0 0 0\n'
                     'eth0 00000000 0102000A 0002 0 0 0 00000000 0 0 0\n'
                     'broken\neth0 00000000 nope 0003 0 0 0 00000000\n')
    gateways = server._default_gateways(route)
    assert gateways == {ipaddress.ip_address('192.168.1.1'), ipaddress.ip_address('10.0.0.1')}
    assert server._default_gateways(tmp_path/'missing') == set()
    monkeypatch.setattr(server, '_DEFAULT_GATEWAYS', gateways)
    for address in ('192.168.1.1', '::ffff:192.168.1.1', '10.0.0.1'):
        assert not server._is_controller(address)
        assert request(server, address, **commit(server), touch=1, radarRender='v2') == {}
    assert server._read_radar_intent() == {'seq': 0}
    assert not (tmp_path/'presence').exists() and not (tmp_path/'radar_render').exists()
    assert server._is_controller('192.168.1.20') and server._is_controller('127.0.0.1')


def test_panel_leaves_a_live_lan_owner_alone_and_recenters_after_it_goes():
    """A phone watching a storm is never yanked home; a closed phone does not
    strand the kiosk away from home."""
    run_remote(r'''
const remote=page(),panel=page(true);await remote.poll();
remote.run('radarBegin();radarCameraSet({lat:48,lon:-121,zoom:8});radarSettle()');await remote.poll();
const owner=server.owner;server.ownerIdle=1;await panel.poll();
panel.run("assert.equal(radarIntent.owned,false);assert.equal(delays.get(radarGesture.idleTimer),90000);now+=90000");
await panel.poll();panel.run("timers.get(radarGesture.idleTimer)()");       // fresh ack: owner alive
await panel.poll();assert.equal(server.owner,owner);assert.equal(server.intent.center.lat,48);   // kept
panel.run("now+=30000;radarIdleSync(true);timers.get(radarGesture.idleTimer)()");              // stale ack: unknown, never moves
await panel.poll();assert.equal(server.owner,owner);
server.ownerIdle=20;await panel.poll();
panel.run("radarIdleSync(true);assert.equal(delays.get(radarGesture.idleTimer),1000);timers.get(radarGesture.idleTimer)()");
await panel.poll();assert.notEqual(server.owner,owner);assert.equal(server.intent.center.lat,47);
const claim=server.requests.filter(q=>q.has('radarCommit')).at(-1);
assert.equal(claim.get('radarClaim'),owner);assert.equal(claim.get('radarClaimEpoch'),'1');
await remote.poll();remote.run("assert.equal(radarIntent.owned,false);assert.equal(radarCamera.lat,47);assert.doesNotMatch(caption(),/Updating view/)");
''')


@pytest.mark.parametrize('state', ['gesturing', 'inertia'])
def test_panel_recenter_for_a_gone_owner_checks_gestures_at_timer_fire(state):
    run_remote(r'''
server.owner='dead-panel-session-123';server.epoch=4;server.generation=3;server.ownerIdle=600;
server.intent={...server.intent,session:server.owner,generation:3,epoch:4,acceptedAt:800,center:{lat:48,lon:-121}};
const panel=page(true),lan=page();await panel.poll();await lan.poll();
lan.run('assert.equal(radarGesture.idleTimer,null)');                     // a LAN follower never recenters
panel.run("assert.equal(delays.get(radarGesture.idleTimer),1000);radarGesture.state=STATE;timers.get(radarGesture.idleTimer)();assert.equal(radarIntent.ready,false)");
panel.run("radarGesture.state='idle';radarIdleSync(true);timers.get(radarGesture.idleTimer)()");
await panel.poll();assert.equal(server.owner,panel.run('radarIntent.session'));
assert.equal(server.intent.center.lat,47);
'''.replace('STATE', "'"+state+"'"))


def test_throttled_page_keeps_commit_presence_and_preferences_for_next_poll():
    run_remote(r'''
const a=page(),b=page();await a.poll();a.run('radarZoomChange(1)');await a.poll();await b.poll();
server.throttled=true;
b.run("radarZoomChange(-1);presenceDirty=true;radarSmooth.pending=true");
await b.poll();
b.run("assert.equal(radarIntent.ready,true);assert.equal(presenceDirty,true);assert.equal(radarCamera.zoom,8);assert.equal(radarSmooth.pending,true);assert.equal(failCount,0);assert.equal(reportRender,true)");
assert.equal(server.owner,a.run('radarIntent.session'));
server.throttled=false;await b.poll();
assert.equal(server.owner,b.run('radarIntent.session'));assert.equal(server.intent.zoom,8);
assert.equal(server.requests.at(-1).get('touch'),'1');assert.equal(server.smooth,'on');
b.run("assert.equal(radarIntent.ready,false);assert.equal(presenceDirty,false);assert.equal(radarSmooth.pending,null)");
''')


@pytest.mark.parametrize('panel', [False, True])
def test_auto_switch_caption_on_every_page_without_follower_camera_update(panel):
    run_remote(r'''
const p=page(PANEL);await p.poll();
p.run("radarView.refresh={state:'newest',targetMode:'site'};assert.match(caption(),/^Switching to Camano radar · showing Region/);assert.doesNotMatch(caption(),/Updating view/);radarView.refresh={state:'newest',targetMode:'mosaic'};assert.doesNotMatch(caption(),/Switching|Updating view/)");
'''.replace('PANEL', json.dumps(panel)))


def test_keyboard_remote_zoom_and_pointer_input_report_presence():
    run_remote(r'''
const p=page();await p.poll();
p.run("for(const fn of events.keydown)fn({key:'+',target:{tagName:'DIV'},preventDefault(){}});assert.equal(presenceDirty,true)");
await p.poll();assert.equal(server.requests.at(-1).get('touch'),'1');assert.equal(server.intent.zoom,9);
p.run("assert.equal(presenceDirty,false);events.pointerdown.forEach(fn=>fn());assert.equal(presenceDirty,true)");
await p.poll();assert.equal(server.requests.at(-1).get('touch'),'1');
p.run("for(const fn of events.keydown)fn({key:'ArrowLeft',target:{tagName:'DIV'},preventDefault(){}});assert.equal(presenceDirty,true)");
await p.poll();assert.equal(server.requests.at(-1).get('touch'),'1');
''')


def test_owning_lan_page_drifts_home_after_its_own_idle_and_panel_waits():
    """The owner, wherever it is, keeps the single-page rule: 90 s untouched."""
    run_remote(r'''const lan=page(),panel=page(true);await lan.poll();
lan.run('radarBegin();radarCameraSet({lat:48,lon:-121,zoom:8});radarSettle()');await lan.poll();
lan.run("assert.equal(radarIntent.owned,true);assert.equal(delays.get(radarGesture.idleTimer),90000)");
server.ownerIdle=1;await panel.poll();
panel.run("now+=80000");server.intent={...server.intent,acceptedAt:1080};await panel.poll();
panel.run("radarIdleSync(true);assert.equal(delays.get(radarGesture.idleTimer),90000);document.hidden=true;timers.get(radarGesture.idleTimer)();assert.equal(radarIntent.ready,false);document.hidden=false;radarView.data=null;radarIdleSync(true);assert.equal(radarGesture.idleTimer,null)");
lan.run("timers.get(radarGesture.idleTimer)()");await lan.poll();
assert.equal(server.owner,lan.run('radarIntent.session'));assert.equal(server.intent.center.lat,47);   // own recenter, no takeover
''')


def test_failed_camera_write_consumes_neither_epoch_nor_generation(server, monkeypatch):
    request(server, **commit(server))
    pending = commit(server, B)
    writer = server._write_settled_camera
    monkeypatch.setattr(server, '_write_settled_camera', lambda *args: False)
    request(server, **pending)
    assert server._radar_owner['epoch'] == 1 and B not in server._radar_high_water
    monkeypatch.setattr(server, '_write_settled_camera', writer)
    request(server, **pending)
    assert server._radar_owner['epoch'] == 2 and server._radar_high_water[B]['generation'] == 1


def test_gateways_are_reread_not_frozen_at_import(server, monkeypatch, tmp_path):
    """The kiosk starts before DHCP on the dongle Pi: an empty startup read must
    not disable the router exclusion for the life of the process."""
    route = tmp_path / 'route'
    route.write_text('Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n')
    reads = []
    def gateways(route_path='/proc/net/route'):
        reads.append(1)
        return server.__dict__['_real_default_gateways'](route)
    monkeypatch.setitem(server.__dict__, '_real_default_gateways', server._default_gateways)
    monkeypatch.setattr(server, '_default_gateways', gateways)
    monkeypatch.setattr(server, '_DEFAULT_GATEWAYS', None)
    monkeypatch.setattr(server, '_gateway_cache', (float('-inf'), frozenset()))
    clock = [1000.0]
    monkeypatch.setattr(server.time, 'monotonic', lambda: clock[0])
    assert server._is_controller('192.168.1.1')                  # no route yet
    route.write_text(route.read_text() + 'wlan0 00000000 0101A8C0 0003 0 0 600 00000000 0 0 0\n')
    assert server._is_controller('192.168.1.1')                  # cached for the TTL
    clock[0] += server._GATEWAY_TTL_SEC
    assert not server._is_controller('192.168.1.1')              # re-read: router excluded
    assert server._is_controller('192.168.0.14') and len(reads) == 2


def test_intent_header_reports_owner_idle(server, monkeypatch):
    clock = [5000.0]
    monkeypatch.setattr(server.time, 'monotonic', lambda: clock[0])
    request(server, **commit(server))
    clock[0] += 12.5
    headers = request(server, **dict(commit(server), radarCommit='0', radarHeartbeat=99))
    ack = json.loads(headers['X-Radar-Intent'])
    assert ack['ownerIdleSec'] == 0.0                             # the owner's own poll refreshes it
    clock[0] += 20
    headers = request(server, '192.168.0.14', **{'radarSession': B, 'radarGeneration': '0', 'radarHeartbeat': '1',
                                                  'view': 'radar', 'radarTheme': 'night'})
    assert json.loads(headers['X-Radar-Intent'])['ownerIdleSec'] == 20.0
    assert 'seen' not in json.loads(headers['X-Radar-Intent'])


def test_site_to_site_handoff_says_switching():
    run_remote(r'''
const p=page();await p.poll();
p.run("radarView.data={...manifest(),sourceId:'iem-nexrad-n0b',sourceMode:'site',siteId:'KATX',sourcePref:'auto',native:true};radarView.pendingSource={frames:[],data:{...manifest(),sourceId:'iem-nexrad-n0b',sourceMode:'site',siteId:'KRTX'}};assert.match(caption(),/^Switching to /);radarView.data.native=false;radarView.pendingSource={frames:[],variantOnly:true,data:{...radarView.data,native:true}};assert.match(caption(),/^Restoring NOAA Level III/)");
''')


def test_page_that_takes_the_view_arms_its_own_drift_home():
    """Measured live: a LAN page's takeover gesture settles before it owns the
    view, so its 90 s drift home was never armed and the view stayed away."""
    run_remote(r'''
const a=page(),b=page();await a.poll();a.run('radarZoomChange(1)');await a.poll();await b.poll();
b.run("assert.equal(radarIntent.owned,false);radarBegin();radarCameraSet({lat:47,lon:-121.5,zoom:9});radarSettle()");
b.run("clearTimeout(radarGesture.idleTimer);radarGesture.idleTimer=null");   // the settle-time arm (as a non-owner) did nothing
await b.poll();
b.run("assert.equal(radarIntent.owned,true);assert.equal(radarIntent.ready,false);assert.equal(delays.get(radarGesture.idleTimer),90000)");
const armed=b.run('radarGesture.idleTimer');
for(let i=0;i<5;i++){b.run('now+=2000');await b.poll();}          // ordinary polls must not restart the countdown
assert.equal(b.run('radarGesture.idleTimer'),armed);
b.run("timers.get(radarGesture.idleTimer)()");
await b.poll();assert.equal(server.owner,b.run('radarIntent.session'));assert.equal(server.intent.center.lon,-122);
''')
