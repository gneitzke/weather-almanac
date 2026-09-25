"""Radar v2 root causes: transport, immutable scans, stickiness and wide geometry."""
import io
import json
import os
import socket
import urllib.error
import urllib.request
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from lib import almanac_emit as ae
from lib import radar_http as transport
from lib import radar_basemap as bm
from tests.test_radar_hybrid import hybrid, png  # noqa: F401
from tests.test_freshness_health import _load_serve, _payload


def test_session_reuses_one_connection_and_ipv4_dns(monkeypatch):
    lookup=Mock(return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('192.0.2.1',443))])
    monkeypatch.setattr(transport.socket,'getaddrinfo',lookup)
    conn=Mock(sock=None)
    conn.getresponse.side_effect=lambda: SimpleNamespace(status=200, length=0, close=lambda:None)
    factory=Mock(return_value=conn); monkeypatch.setattr(transport,'_Connection',factory)
    session=transport.RadarSession()
    for n in range(9):
        with session.open(urllib.request.Request(f'https://radar.example/{n}'),10): pass
        session.begin_pass()
    assert lookup.call_count==factory.call_count==1
    assert lookup.call_args.args==('radar.example',443,socket.AF_INET,socket.SOCK_STREAM)
    assert conn.request.call_count==9
    session.close(); conn.close.assert_called_once()


def test_oversized_response_discards_socket(make_emitter, monkeypatch):
    emitter=make_emitter(); emitter._radar_session=transport.RadarSession()
    conn=Mock(); emitter._radar_session.connections[('radar.example',443)]=[conn]
    monkeypatch.setattr(emitter._radar_session,'open',lambda *a,**k:io.BytesIO(b'x'*(2*1024*1024+1)))
    with pytest.raises(ValueError,match='oversized'):
        emitter._radar_request('iem-mrms-lcref','https://radar.example/tile',ae.time.monotonic()+10)
    conn.close.assert_called_once()


def test_connect_preserves_sni_and_host_without_dns(monkeypatch):
    sock=Mock(); monkeypatch.setattr(transport.socket,'socket',Mock(return_value=sock))
    conn=transport._Connection('radar.example',443,[(socket.AF_INET,socket.SOCK_STREAM,6,'',('192.0.2.1',443))],10)
    context=Mock(); conn._context=context
    conn.connect()
    sock.connect.assert_called_once_with(('192.0.2.1',443))
    context.wrap_socket.assert_called_once_with(sock,server_hostname='radar.example',do_handshake_on_connect=False,session=None)
    conn.sock.do_handshake.assert_called_once_with()
    assert conn.host=='radar.example'
    conn.close()


def test_dns_failure_cached_for_pass_and_next_pass_retries(monkeypatch):
    lookup=Mock(side_effect=socket.gaierror(-3,'temporary failure'))
    monkeypatch.setattr(transport.socket,'getaddrinfo',lookup)
    req=urllib.request.Request('https://radar.example/a')
    session=transport.RadarSession()
    for _ in range(3):
        with pytest.raises(socket.gaierror): session.open(req,1)
    assert lookup.call_count==1
    with pytest.raises(socket.gaierror): transport.RadarSession().open(req,1)
    assert lookup.call_count==2


def test_newest_advertised_scan_and_bounded_failures(make_emitter,hybrid):
    hybrid.now=hybrid.latest+60
    def fail(req,_):
        if 'mrms::' in req.full_url:
            raise urllib.error.HTTPError(req.full_url,503,'rendering',{},None)
    hybrid.failure=fail
    emitter=make_emitter(); emitter._do_radar()
    gets=[c[2] for c in hybrid.calls if 'mrms::' in c[2]]
    assert gets
    stamps=[datetime.strptime(u.split('lcref-')[1].split('/')[0],'%Y%m%d%H%M').replace(tzinfo=timezone.utc).timestamp() for u in gets]
    assert stamps[0] == hybrid.latest  # no artificial five-minute readiness hold
    assert all(stamps.count(t)<=12 for t in set(stamps))  # six workers, at most two attempts
    assert emitter._radar_result.source_id=='iem-mrms-lcref'
    assert emitter._radar_transport_failures['iem-mrms-lcref'] == 1


def test_placeholder_fail_fast(make_emitter,hybrid):
    hybrid.tile=png((255,0,0,255))
    emitter=make_emitter(); emitter._do_radar()
    gets=[c[2].split('lcref-')[1].split('/')[0] for c in hybrid.calls if 'mrms::' in c[2]]
    assert gets and all(gets.count(t)<=12 for t in set(gets))  # six tiles, at most two attempts
    assert not any(k[0] == 'iem-mrms-lcref' for k in emitter._radar_tiles)


def test_stickiness_logs_failure_then_switch_and_recovery(make_emitter,hybrid,monkeypatch):
    emitter=make_emitter(); emitter._do_radar(); first=emitter._radar_result
    warnings=[]; infos=[]
    monkeypatch.setattr(ae.Logger,'warning',warnings.append);monkeypatch.setattr(ae.Logger,'info',infos.append)
    def fail(req,_):
        if 'iastate.edu' in req.full_url: raise OSError('test source down')
    hybrid.failure=fail;hybrid.calls.clear();emitter._do_radar()
    assert emitter._radar_result is first and not any(c[0]=='rainviewer' for c in hybrid.calls)
    assert any('iem-mrms-lcref' in w and 'test source down' in w and 'suppressed=0' in w for w in warnings)
    assert any('radar pass outcome=failed' in line and 'elapsed=' in line for line in infos)
    hybrid.mono=241;emitter._do_radar();emitter._do_radar()
    assert emitter._radar_result.source_id=='rainviewer'
    assert any('SWITCH iem-mrms-lcref -> rainviewer' in m for m in infos)
    hybrid.failure=None;hybrid.latest+=600;hybrid.mono+=301;emitter._do_radar()
    assert emitter._radar_result.source_id=='iem-mrms-lcref'
    assert any('SWITCH rainviewer -> iem-mrms-lcref' in m for m in infos)


def test_regressed_primary_retains_newest(make_emitter,hybrid):
    emitter=make_emitter();emitter._do_radar();snap=emitter._radar_result
    hybrid.latest-=120;emitter._do_radar()
    assert emitter._radar_result is snap


@pytest.mark.parametrize('mode,expected', [('site','iem-nexrad-n0b'),('mosaic','iem-mrms-lcref')])
def test_site_actual_scans_and_restart(make_emitter,hybrid,tmp_path,monkeypatch,mode,expected):
    # This test owns an IEM ridge transport and asserts its per-site layers.
    monkeypatch.setattr(ae.AlmanacEmitter, '_radar_level3_down', lambda self: True)
    monkeypatch.setattr(ae, '_NEXRAD_SITES', {'KATX': ae._NEXRAD_SITES['KATX']})
    original=ae.RadarSession.open
    scans=[hybrid.latest-1800,hybrid.latest-1200,hybrid.latest-600,hybrid.latest]
    def fetch(self,req,timeout):
        if 'operation=list' in req.full_url:
            assert 'radar=ATX' in req.full_url and 'product=N0B' in req.full_url
            return io.BytesIO(json.dumps(dict(scans=[dict(ts=datetime.fromtimestamp(t,timezone.utc).strftime('%Y-%m-%dT%H:%MZ')) for t in scans])).encode())
        if 'ridge::' in req.full_url:
            assert 'ridge::ATX-N0B-2026' in req.full_url
            hybrid.calls.append(('site','GET',req.full_url,hybrid.mono,timeout))
            return io.BytesIO(png())
        return original(self,req,timeout)
    monkeypatch.setattr(ae.RadarSession,'open',fetch)
    (tmp_path/'radar_source').write_text(mode);(tmp_path/'radar_zoom').write_text('7');hybrid.view()
    for _ in range(2):
        emitter=make_emitter();emitter._do_radar();r=emitter._build_payload()['radar']
        assert r['sourceId']==expected and r['sourceMode']==mode
        if mode=='site':
            assert [f['ts'] for f in r['tiles']['frames']]==scans
            assert r['completeFrameCount']==4 and r['siteId']=='KATX'
            assert r['tiles']['z']==7 and r['zoomMin']==4 and r['zoomMax']==10 and not r['zoomCapped']
            assert r['cadenceSec']==300 and not r['scanningSlowly'] and r['scanMode']=='clear-air' and r['scanCadenceSec']==600 and not r['latestOnly']
            assert r['legend']['id']=='almanac-reflectivity-v3'


def test_site_failure_reports_disabled_and_falls_back(make_emitter,hybrid,tmp_path,monkeypatch):
    (tmp_path/'radar_source').write_text('site')
    original=ae.RadarSession.open
    def fetch(self,req,timeout):
        if 'operation=list' in req.full_url: return io.BytesIO(b'{"scans":[]}')
        return original(self,req,timeout)
    monkeypatch.setattr(ae.RadarSession,'open',fetch)
    emitter=make_emitter()
    for _ in range(3): emitter._do_radar(intent_triggered=False)
    r=emitter._build_payload()['radar']
    assert r['sourceMode']=='mosaic' and r['sourceId']=='iem-mrms-lcref'
    assert r['sources'][1]['reason']=='not reporting' and not r['sources'][1]['available']


@pytest.mark.parametrize('address,query,value', [('127.0.0.1','radarSource=site','site'),
    ('::1','radarSource=mosaic','mosaic'),('198.51.100.1','radarSource=site',None),
    ('127.0.0.1','radarSource=site&radarSource=mosaic',None),('127.0.0.1','radarSource=KATX',None)])
def test_source_marker_loopback_validation(monkeypatch,tmp_path,address,query,value):
    module=_load_serve(monkeypatch,tmp_path,_payload())
    monkeypatch.setattr(module.http.server.SimpleHTTPRequestHandler,'do_GET',lambda h:None)
    handler=object.__new__(module.Handler);handler.client_address=(address,1);handler.path='/wx.json?'+query
    handler.do_GET();pref=tmp_path/'radar_source'
    assert (pref.read_text().strip() if pref.exists() else None)==value




@pytest.mark.parametrize('source', ['iem-mrms-lcref', 'iem-nexrad-n0b', 'rainviewer'])
def test_stale_uses_source_tuned_threshold_not_bare_cadence(make_emitter, hybrid, source):
    # Stale is the source's own stale_sec, which sits ABOVE that source's freshest
    # possible frame — a bare 3*cadence would flag every healthy MRMS scan, since
    # the adapter skips frames younger than RADAR_IEM_READY_LAG_SEC (IEM 503s them).
    settings = ae._RADAR_SOURCES[source]
    ss = settings['stale_sec']
    emitter = make_emitter(); emitter._do_radar()
    snap = emitter._radar_result._replace(cadence=settings['cadence'], stale_sec=ss)
    at = lambda age: ae.AlmanacEmitter._radar_payload(snap, snap.ts_frame + age, timezone.utc)
    assert at(ss)['staleSec'] == ss
    assert not at(ss - 1)['stale'] and at(ss)['stale']
    if source.startswith('iem'):        # the freshest frame we ever show is not stale
        assert not at(ae.RADAR_IEM_READY_LAG_SEC + settings['cadence'])['stale']


@pytest.mark.skipif(os.environ.get('RADAR_NET_TEST')!='1',reason='opt in with RADAR_NET_TEST=1')
def test_live_n0b_native_palette():
    """Immutable archived raw indexed raster: all representative colors, not just wet pixels."""
    import ssl
    context=ssl.create_default_context()
    try:
        import certifi
        context=ssl.create_default_context(cafile=certifi.where())
    except ImportError: pass
    url='https://mesonet.agron.iastate.edu/archive/data/2026/09/13/GIS/ridge/ATX/N0B/ATX_N0B_202609132140.png'
    with urllib.request.urlopen(url,timeout=25,context=context) as response: raw=response.read()
    image=Image.open(io.BytesIO(raw)); assert image.mode=='P'
    palette=image.getpalette()
    for dbz,color in ((5,'#6c7daa'),(20,'#52d6a2'),(30,'#0c9110'),(40,'#d6c704'),(50,'#ff8000'),(60,'#ffffff'),(70,'#b200ff')):
        index=int((dbz+33)*2)
        assert '#'+bytes(palette[index*3:index*3+3]).hex()==color

    # Independent dBZ-to-RGB mapping from the provider's curve, so this test
    # cannot pass merely by repeating an incorrect PNG index offset.
    import xml.etree.ElementTree as ET
    curve='https://raw.githubusercontent.com/akrherz/iem/main/scripts/ridge/ReflectivityColorCurveManager.xml'
    with urllib.request.urlopen(curve,timeout=25,context=context) as response:
        levels=ET.fromstring(response.read())
    for dbz,color in ((5,'#6c7daa'),(20,'#52d6a2'),(30,'#0c9110'),(40,'#d6c704'),(50,'#ff8000'),(60,'#ffffff'),(70,'#b200ff')):
        level=next(el for el in levels if float(el.get('lowerValue','-999'))<=dbz<float(el.get('upperValue','999')))
        assert bytes(int(level.findtext(channel)) for channel in ('red','green','blue')).hex()==color[1:]
