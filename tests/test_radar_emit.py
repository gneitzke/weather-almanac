"""Radar geometry, complete-frame cache, atomic publication and real palette fidelity."""
import builtins
import io
import json
import math
import os
import ssl
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from lib import almanac_emit as ae
from lib import radar_engine
from tests.fixtures.config import make_config


@pytest.fixture(autouse=True)
def radar_dir(tmp_path, monkeypatch):
    directory = tmp_path / 'radar'
    monkeypatch.setenv('WFP_RADAR_DIR', str(directory))
    monkeypatch.setattr(radar_engine, 'RADAR_DIR', os.environ['WFP_RADAR_DIR'])
    return directory


@pytest.fixture
def radar_net(monkeypatch):
    tile = io.BytesIO()
    Image.new('RGBA', (256, 256), (0, 163, 224, 100)).save(tile, format='PNG')
    state = dict(times=[1800000000, 1800000600], calls=[], fail=None, tile=tile.getvalue())

    def fetch(req, timeout):
        assert 0 < timeout <= radar_engine.RADAR_HTTP_TIMEOUT_SEC
        assert req.get_header('User-agent') == 'WeatherAlmanac'
        url = req.full_url
        if url == radar_engine.RADAR_IEM_METADATA_URL:
            raise urllib.error.URLError('primary unavailable in fallback fixture')
        state['calls'].append(url)
        if state['fail']:
            state['fail'](url)
        if url == radar_engine.RADAR_RAINVIEWER_MANIFEST_URL:
            return io.BytesIO(json.dumps(dict(host='https://tiles.example', radar=dict(
                past=[dict(time=t, path=f'/v2/{t}') for t in reversed(state['times'])],
                nowcast=[dict(time=1999999999, path='/never')]))).encode())
        assert url.endswith('/2/0_0.png') and int(url.split('/256/')[1].split('/')[0]) in range(4, 8)
        return io.BytesIO(state['tile'])

    # Isolate the global adapter: Auto now considers NEXRAD independently of MRMS.
    monkeypatch.setattr(radar_engine, '_radar_iem_eligible', lambda *args: False)
    monkeypatch.setattr(radar_engine, '_NEXRAD_SITES', {})
    monkeypatch.setattr(radar_engine.RadarSession, 'open', lambda self, *a, **k: fetch(*a, **k))
    monkeypatch.setattr(ae.time, 'sleep', lambda _: None)
    monkeypatch.setattr(ae.time, 'time', lambda: state['times'][-1] + 240)
    return state


def tile_calls(state):
    return [u for u in state['calls'] if u != radar_engine.RADAR_RAINVIEWER_MANIFEST_URL]


@pytest.fixture
def radar_viewed(tmp_path):
    marker = tmp_path / 'radar_viewed'
    marker.write_text(str(ae.time.time()))
    return marker


@pytest.mark.parametrize('lat,expected', [(0, 9), (47.6, 8), (60, 8), (78, 6),
                                         (-78, 6), (85, 5), (90, 4), (-90, 4)])
def test_zoom_targets_coverage_with_clamps(lat, expected):
    zoom = radar_engine._radar_zoom_for(lat)
    assert zoom == expected
    assert radar_engine.RADAR_MIN_ZOOM <= zoom <= radar_engine.RADAR_MAX_ZOOM
    raw = math.log2(156543.03392 * math.cos(math.radians(lat)) / (200000 / 490))
    if 4 <= round(raw) <= 9:
        coverage = radar_engine._radar_viewport(lat, 0, zoom, 490)[1] * 490
        assert 200000 / math.sqrt(2) <= coverage <= 200000 * math.sqrt(2)
    assert radar_engine._radar_zoom_for(0) > radar_engine._radar_zoom_for(78)


def test_zoom_recomputed_each_pass_and_latitude_change(make_emitter, radar_net, monkeypatch):
    calls = []
    original = radar_engine._radar_zoom_for
    def zoom_for(lat):
        calls.append(lat)
        return original(lat)
    monkeypatch.setattr(radar_engine, '_radar_zoom_for', zoom_for)
    emitter = make_emitter()
    emitter.radar._acquire(); emitter.radar._acquire()
    assert calls == [47.61, 47.61]
    emitter.app.config = make_config(Station={'Latitude': '78'})
    radar_net['times'] = [1800001200]
    radar_net['calls'].clear()
    emitter.radar._acquire()
    assert calls == [47.61, 47.61, 78]
    assert emitter._build_payload()['radar']['tiles']['z'] == 6
    assert all('/256/6/' in url for url in tile_calls(radar_net))


@pytest.mark.parametrize('marker', [None, 'old', 'boundary', 'garbage', '', 'nan', 'inf', '-inf', 'future', b'\xff'])
def test_unviewed_builds_only_latest(make_emitter, radar_net, radar_dir, tmp_path, marker):
    if marker is not None:
        marker = {'old': str(ae.time.time() - 901), 'boundary': str(ae.time.time() - 900),
                  'future': str(ae.time.time() + 3600)}.get(marker, marker)
        (tmp_path / 'radar_viewed').write_bytes(marker if isinstance(marker, bytes) else marker.encode())
    radar_net['times'] = [1800000000 + i * 600 for i in range(13)]
    emitter = make_emitter(); emitter.radar._acquire()
    frames = emitter.radar._frames
    assert [f['ts'] for f in frames] == radar_net['times'][-7:]
    assert all(not f['complete'] and 'url' not in f for f in frames[:-1])
    assert frames[-1]['complete'] and emitter.radar._ts_frame == frames[-1]['ts']
    grid=emitter.radar._result.tiles['grid']; visible=grid['w']*grid['h']
    assert len(list(radar_dir.rglob('*.png'))) == visible
    assert len(tile_calls(radar_net)) == visible
    assert len(radar_net['calls']) == visible+1




@pytest.mark.parametrize('lat,lon', [(47.61, -122.33), (0, 0), (-33.8, 151.2)])
def test_viewport_covers_crop_and_marker(lat, lon):
    tiles, mpp, bounds, marker = radar_engine._radar_viewport(lat, lon, 7, 480)
    assert marker == pytest.approx((240, 240))
    assert mpp == pytest.approx(2 * math.pi * 6378137 * math.cos(math.radians(lat)) / 32768, rel=.001)
    assert bounds['s'] < lat < bounds['n'] and bounds['w'] < lon < bounds['e']
    mask = Image.new('1', (480, 480))
    for tx, ty, x, y in tiles:
        assert 0 <= tx < 128 and 0 <= ty < 128
        mask.paste(1, (x, y, x + 256, y + 256))
    assert mask.getextrema() == (1, 1)
    left = (lon + 180) / 360 * 32768 - 240
    top = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * 32768 - 240
    for tx, ty, x, y in tiles:
        assert x == int(tx * 256 - left) and y == int(ty * 256 - top)


@pytest.mark.parametrize('lat,lon', [(0, 179.99), (0, -179.99), (90, 0), (-90, 0)])
def test_viewport_wraps_and_clamps(lat, lon):
    tiles, mpp, bounds, marker = radar_engine._radar_viewport(lat, lon, 7, 480)
    assert all(0 <= x < 128 and 0 <= y < 128 for x, y, _, _ in tiles)
    assert math.isfinite(mpp) and mpp > 0
    if abs(lon) > 179:
        assert {0, 127} <= {t[0] for t in tiles}
        assert bounds['e'] < bounds['w']


@pytest.mark.parametrize('setting,unit,factor', [('mi', 'mi', 1609.344), ('miles', 'mi', 1609.344), ('km', 'km', 1000)])
def test_scale_and_rings(setting, unit, factor):
    mpp = radar_engine._radar_viewport(47.61, -122.33, 7, 480)[1]
    assert radar_engine._radar_distance_unit(make_config(Units={'Distance': setting})) == unit
    bar, rings = radar_engine._radar_scale(mpp, 480, unit)
    dist = int(bar['distDisp'].split()[0])
    assert dist == max(d for d in [5, 10, 20, 25, 50, 100, 150, 200, 250] if d * factor / mpp <= 192)
    assert bar['pixels'] == pytest.approx(bar['meters'] / mpp)
    assert bar['meters'] == dist * factor and bar['unit'] == unit
    assert all(r['px'] <= 480 / math.sqrt(2) for r in rings)
    assert rings[0]['px'] == bar['pixels']


def test_nexrad_is_caption_only_and_uses_station_unit():
    assert len(radar_engine._NEXRAD_SITES) == 160
    r = radar_engine._radar_nexrad(47.61, -122.33, 'mi')
    assert r['id'] == 'KATX' and r['bearing'] == 'N' and r['distanceDisp'].endswith(' mi')
    assert radar_engine._radar_nexrad(0, 0, 'km') is None
    assert radar_engine._radar_nexrad(47.61, -122.33, 'km')['distanceDisp'].endswith(' km')






@pytest.mark.parametrize('field', ['Latitude', 'Longitude'])
def test_missing_location(make_emitter, field):
    emitter = make_emitter(config=make_config(Station={field: ''})); emitter.radar._acquire()
    assert not emitter.radar._available and emitter.radar._reason == 'no location'


def test_zero_location_is_valid(make_emitter, radar_net):
    emitter = make_emitter(config=make_config(Station={'Latitude': '0', 'Longitude': '0'}))
    emitter.radar._acquire()
    assert emitter.radar._available and emitter.radar._nexrad is None


def test_pillow_absent(make_emitter, monkeypatch):
    real = builtins.__import__
    def importing(name, *args, **kwargs):
        if name == 'PIL':
            raise ImportError('Pillow absent')
        return real(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', importing)
    emitter = make_emitter(); emitter.radar._acquire()
    assert not emitter.radar._available and emitter.radar._reason == 'compositor unavailable'


def test_stale_uses_frame_not_manifest(make_emitter, radar_net, monkeypatch):
    ss = radar_engine.RADAR_RAINVIEWER_STALE_SEC
    clock = {'now': radar_net['times'][-1] + ss - 1}
    monkeypatch.setattr(ae.time, 'time', lambda: clock['now'])
    emitter = make_emitter(); emitter.radar._acquire()
    r = emitter._build_payload()['radar']
    assert r['ageSec'] == ss - 1 and not r['stale'] and r['fetchedAt'] == clock['now']
    clock['now'] += 1                       # one second past the source's stale_sec
    r = emitter._build_payload()['radar']
    assert r['stale'] and r['ageSec'] == ss and r['staleSec'] == ss


@pytest.mark.parametrize('failure', ['manifest', 'tile', 'decode', 'save'])
def test_never_raises_keeps_last_good_and_warns_once(make_emitter, radar_net, monkeypatch, failure):
    emitter = make_emitter(); emitter.radar._acquire(); previous = emitter.radar._result
    radar_net['times'] = [1800001200]
    warnings, retries = [], []
    monkeypatch.setattr(ae.Logger, 'warning', warnings.append)
    monkeypatch.setattr(emitter.radar, '_schedule_retry', lambda *a, **kw: retries.append(a))
    def fail(url):
        if failure == 'manifest' or (failure == 'tile' and url != radar_engine.RADAR_RAINVIEWER_MANIFEST_URL):
            raise urllib.error.URLError('offline')
    radar_net['fail'] = fail
    if failure == 'decode': radar_net['tile'] = b'bad PNG'
    if failure == 'save':
        monkeypatch.setattr(Image.Image, 'save', lambda *a, **k: (_ for _ in ()).throw(OSError('disk full')))
    emitter.radar._acquire()
    if failure == 'manifest':
        assert emitter.radar._result is previous
    else:
        # v4.7: validated discovery lists a pending newest even when its tiles
        # fail. Last-good frames and fetchedAt survive; no completed scan is lost.
        result = emitter.radar._result
        assert result.frames[:-1] == previous.frames
        assert result.frames[-1]['ts'] == radar_net['times'][-1]
        assert not result.frames[-1]['complete']
        assert result.ts_fetch == previous.ts_fetch
    assert len(warnings) == 1 and len(retries) == 1
    assert 'radar rainviewer failed:' in warnings[0] and 'suppressed=0' in warnings[0]
    assert retries[0][:2] == ('radar', emitter.radar._check)
    if failure == 'tile':
        assert 29 <= retries[0][2] <= 30  # probe the newly opened host circuit
    else:
        assert retries[0][2] == 2  # bounded retry before the failed-pass threshold




def test_radar_schedules_are_registered_and_cancelled(make_emitter, monkeypatch):
    from tests.test_emitter_lifecycle import FakeClock, HangingThread
    from types import SimpleNamespace
    clock = FakeClock(); monkeypatch.setattr(ae, 'Clock', clock)
    monkeypatch.setattr(ae, 'threading', SimpleNamespace(Thread=HangingThread))
    emitter = make_emitter(); emitter.start()
    assert sorted(e.timeout for e in clock.events if e.timeout in (60, 180)) == [60]
    emitter.radar._schedule_retry('radar', emitter.radar._check, 120)
    emitter.radar._schedule_retry('radar', emitter.radar._check, 120)
    assert len(emitter._runtime.retries) == 1
    emitter.radar._check(); emitter.radar._check()
    assert emitter._runtime.inflight == {'radar'}
    emitter.stop(); assert not clock.events and not emitter._runtime.retries


# Authoritative RainViewer stops, transcribed from rainviewer_api_colors_table.csv
# (the "Universal Blue" column). The file lists a colour PER dBZ; the snow ramp is a
# second block keyed on the same dBZ axis, so its 20 dBZ stop is our snow swatch.
# Pinning the published table — not a sampled tile — is what makes the fidelity check
# deterministic: RainViewer's scale is continuous, so a rare anchor intensity (60/65
# dBZ severe cores) is often simply not falling anywhere on Earth at fetch time, and a
# live tile then paints 59/64 dBZ instead. That is weather, not a palette mismatch.
_UNIVERSAL_BLUE_RAIN = {5: '#92887164', 20: '#00a3e0ff', 30: '#005588ff',
                        40: '#ffaa00ff', 50: '#c10000ff', 60: '#ff77ffff', 65: '#ffffffff'}
_UNIVERSAL_BLUE_SNOW = {20: '#7fbfffff'}


def test_shared_legend_replaces_native_scales():
    assert all(settings['legend'] is radar_engine._RADAR_DISPLAY_RAMP for settings in radar_engine._RADAR_SOURCES.values())
    assert all(source['legend'].get('snow') is None for source in radar_engine._RADAR_SOURCES.values())


@pytest.mark.skipif(os.environ.get('RADAR_NET_TEST') != '1',
                    reason='fetches the RainViewer colour table; opt in with RADAR_NET_TEST=1 (kept out of CI)')
def test_legend_fidelity_against_published_colortable():
    """Online: the provider still publishes exactly the stops we render.

    Fetches rainviewer_api_colors_table.csv and checks each anchor against the
    Universal Blue column (rain) and the snow block. Deterministic — it verifies
    the source of truth, so it catches a real scale change yet never flakes on the
    weather. Skip transport outages only, never a colour mismatch. Opt-in so CI
    stays hermetic.
    """
    context = ssl.create_default_context()
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    url = 'https://www.rainviewer.com/files/rainviewer_api_colors_table.csv'
    request = urllib.request.Request(url, headers={'User-Agent': 'WeatherAlmanac'})
    try:
        with urllib.request.urlopen(request, timeout=25, context=context) as response:
            rows = response.read().decode().splitlines()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        pytest.skip(f'RainViewer colour table offline: {error}')
    header = rows[0].split(',')
    blue = header.index('Universal Blue')
    # The file is two dBZ-keyed blocks (rain, then snow); split on the dBZ reset.
    rain, snow, prev = {}, {}, None
    target = rain
    for row in rows[1:]:
        cells = row.split(',')
        dbz = int(cells[0])
        if prev is not None and dbz < prev:
            target = snow
        target[dbz] = cells[blue]
        prev = dbz
    for dbz, hexa in _UNIVERSAL_BLUE_RAIN.items():
        assert rain[dbz] == hexa, f'{dbz} dBZ drifted: table {rain[dbz]} vs legend {hexa}'
    assert snow[20] == _UNIVERSAL_BLUE_SNOW[20], 'snow stop drifted from the table'


def test_cold_start_rate_limit_covers_all_frames(make_emitter, radar_net, monkeypatch, radar_viewed, radar_dir):
    monkeypatch.setattr(radar_engine, 'RADAR_REQUESTS_PER_MIN', 90)  # frame counts below are budget-relative
    monkeypatch.setattr(radar_engine, 'RADAR_HISTORY_RESERVE', 17)
    clock = [0.0]
    starts = []
    original_fetch = radar_engine.RadarSession().open
    def fetch(*args, **kwargs):
        if '/256/' in args[0].full_url:
            starts.append(clock[0])
        return original_fetch(*args, **kwargs)
    monkeypatch.setattr(ae.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(ae.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(radar_engine.RadarSession, 'open', lambda self, *a, **k: fetch(*a, **k))
    radar_net['times'] = [1800000000 + i * 600 for i in range(13)]
    radar_viewed.write_text(str(ae.time.time()))
    emitter = make_emitter(); emitter.radar._acquire()
    assert len(emitter.radar._frames) == 7
    assert sum(f['complete'] for f in emitter.radar._frames) == 7  # visible native footprint fits the cap
    clock[0] += 60; emitter.radar._acquire()
    assert all(f['complete'] for f in emitter.radar._frames)
    assert len(list(radar_dir.rglob('*.png'))) == len(starts)
    assert len(radar_net['calls']) == len(starts)+2
    assert all(sum(t <= v < t + 60 for v in starts) <= 90 for t in starts)


def test_r7_tile_service_remaps_once_and_metadata(make_emitter,radar_net,radar_dir,monkeypatch):
    emitter=make_emitter();emitter.radar._acquire()
    paths=list((radar_dir/'t').rglob('*.png'));assert paths
    expected=None
    from lib.radar_palette import remap,source_palette
    with Image.open(io.BytesIO(radar_net['tile'])) as native:
        with remap(native,'rainviewer',source_palette('rainviewer')) as mapped:expected=mapped.tobytes()
    for path in paths:
        with Image.open(path) as tile:
            assert tile.size==(256,256) and tile.convert('RGBA').tobytes()==expected
            meta=json.loads(tile.info['radarRemap'])
            assert set(meta)=={'revision','remapped','unmatchedColors','opaqueColors','unmatchedPixels','opaquePixels','ambiguousPixels'}
    radar_net['calls'].clear()
    monkeypatch.setattr(radar_engine,'remap',lambda *a:(_ for _ in ()).throw(AssertionError('warm tile remapped twice')))
    emitter.radar._acquire(intent_triggered=True)
    assert not radar_net['calls']


def test_r8_manifest_exact_disk_mask_and_levels(make_emitter,radar_net,radar_dir):
    emitter=make_emitter();emitter.radar._acquire();r=emitter._build_payload()['radar'];m=r['tiles'];g=m['grid'];mask=int(m['newest']['mask'],16)
    for i in range(g['w']*g['h']):
        x,y=g['x0']+i%g['w'],g['y0']+i//g['w']
        assert bool(mask&(1<<i))==radar_engine._radar_tile_path(m['source'],None,m['newest']['stamp'],m['z'],x,y).is_file()
    assert mask>>(g['w']*g['h'])==0
    for f in m['frames']:
        for z,complete in f['levels'].items():
            scale=2**(int(z)-m['z'])
            expected=all(radar_engine._radar_tile_path(m['source'],None,f['stamp'],int(z),x,y).is_file()
                for y in range(math.floor(g['y0']*scale),math.ceil((g['y0']+g['h'])*scale))
                for x in range(math.floor(g['x0']*scale),math.ceil((g['x0']+g['w'])*scale)))
            assert complete==expected
    retired={'latest','frames','geometryOnly','basemap','marker','centered','bounds','viewport','metersPerPixel','scaleBar'}
    assert not retired.intersection(r)
    assert all(not {'id','url','complete'}.intersection(f) for f in m['frames'])
    assert all(set(ring)=={'meters','label'} for ring in r['rings'])


def test_r9_disk_served_lru_protects_current_hour(make_emitter,radar_dir):
    emitter=make_emitter();now=1800000000
    emitter.radar._disk_inventory.MAX_FILES=8000  # the cap this test was written for; live caps follow free space
    emitter.radar._result=radar_engine._RADAR_NONE._replace(ts_frame=now,zoom=8,source_id='iem-mrms-lcref',
        frames=({'ts':now,'siteScans':[]},),tiles={'grid':dict(x0=0,y0=0,w=1,h=1)})
    old=datetime.fromtimestamp(now-7200,ae.timezone.utc).strftime('%Y%m%d%H%M')
    protected=radar_engine._radar_tile_path('iem-mrms-lcref',None,now,8,0,0)
    protected.parent.mkdir(parents=True);protected.write_bytes(b'p');os.utime(protected,(1,1))
    emitter.radar._disk_inventory.add(('iem-mrms-lcref',None,radar_engine._radar_stamp_text(now),8,0,0),protected,1,{})
    paths=[]
    for i in range(8099):
        path=radar_engine._radar_tile_path('iem-mrms-lcref',None,old,9,i//256,i%256)
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b't');os.utime(path,(10+i,10+i));paths.append(path)
        emitter.radar._disk_inventory.add(('iem-mrms-lcref',None,old,9,i//256,i%256),path,1,{})
    emitter.radar._prune()
    remaining=list((radar_dir/'t').rglob('*.png'))
    assert len(remaining)==8000 and sum(p.stat().st_size for p in remaining)<=64_000_000
    assert protected.exists() and not any(p.exists() for p in paths[:100]) and all(p.exists() for p in paths[100:])


def test_partial_tiles_publish_before_full_view(make_emitter,radar_net,monkeypatch):
    monkeypatch.setattr(radar_engine,'_radar_zoom_for',lambda lat:7)  # more than one six-worker batch
    emitter=make_emitter();seen=[]
    monkeypatch.setattr(emitter.radar,'_emit_now',lambda:seen.append(emitter.radar._result))
    emitter.radar._acquire()
    partial=[s for s in seen if s.tiles and 0<int(s.tiles['newest']['mask'],16)<(1<<(s.tiles['grid']['w']*s.tiles['grid']['h']))-1]
    assert partial and all(s.ts_frame==radar_net['times'][-1] for s in partial)
