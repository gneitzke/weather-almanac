"""Astra's adversarial review of the October radar fixes: emitter and server
items. Page logic is in test_radar_review_oct_adv_page.py. Local
fixtures and loopback sockets only."""
import json
import math
import os
import subprocess
import sys
import threading
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import numpy as np
import pytest

from lib import almanac_emit as ae
from lib import radar_engine
from lib import radar_level3 as l3
from lib import radar_mosaic as mosaic
from lib.radar_level3 import Scan
from lib.radar_palette import source_palette
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401
from tests.test_freshness_health import serve_at, _get, _load_serve, _payload  # noqa: F401
from tests.test_radar_remote_serve import server  # noqa: F401
from tests.test_radar_review_oct_serve import _handler

SITES = {'KNEA': (47.61, -122.33, 'nearest'), 'KMID': (47.8, -122.33, 'middle'), 'KFAR': (48, -122.33, 'far')}


# ------------------------------------- must-fix 1: expected contributors survive listing


def _site_ctx(sites, scans, primary='KNEA'):
    return dict(sites=sites, site_scans=scans, site_id=primary, bounds=dict(n=47.9, s=47.4, w=-122.8, e=-121.9))


def _drawn(frame, got):
    pairs = sorted([list(p) for p in got])
    return dict(frame, requestedPairs=pairs, acquiredSites=[dict(id=s, ts=t) for s, t in got],
                siteScans=[dict(id=s, ts=t) for s, t in got])


@pytest.fixture
def three_sites(monkeypatch):
    monkeypatch.setattr(radar_engine, '_NEXRAD_SITES', SITES)


def test_a_failed_neighbour_listing_is_partial_coverage(three_sites):
    T = int(time.time()) // 60 * 60
    sites = [dict(id='KNEA', reporting=True, reason=None),
             dict(id='KMID', reporting=None, reason='scan unavailable')]   # the listing failed
    ctx = _site_ctx(sites, {'KNEA': (T,), 'KMID': ()})
    pairs = radar_engine._radar_site_pairs(ctx, T, now=T+30)
    assert pairs == (('KNEA', T),)            # nothing to request from KMID ...
    frame = _drawn(radar_engine._radar_frame('iem-nexrad-n0b', T, ctx, pairs), pairs)
    assert frame['expectedSites'] == ['KMID', 'KNEA']
    # ... and the view lies inside KNEA's range, yet KMID is missing.
    assert radar_engine._radar_partial_coverage('iem-nexrad-n0b', frame, ctx) is True


def test_a_freshness_expired_neighbour_is_partial_coverage(three_sites):
    T = int(time.time()) // 60 * 60
    sites = [dict(id='KNEA', reporting=True, reason=None), dict(id='KMID', reporting=True, reason=None)]
    ctx = _site_ctx(sites, {'KNEA': (T-300, T), 'KMID': (T-1000,)})
    pairs = radar_engine._radar_site_pairs(ctx, T, now=T+30)
    assert pairs == (('KNEA', T),)
    frame = _drawn(radar_engine._radar_frame('iem-nexrad-n0b', T, ctx, pairs), pairs)
    assert radar_engine._radar_partial_coverage('iem-nexrad-n0b', frame, ctx) is True


def test_a_site_known_not_reporting_is_not_expected(three_sites):
    T = int(time.time()) // 60 * 60
    sites = [dict(id='KNEA', reporting=True, reason=None), dict(id='KFAR', reporting=False, reason='not reporting')]
    ctx = _site_ctx(sites, {'KNEA': (T,), 'KFAR': ()})
    pairs = radar_engine._radar_site_pairs(ctx, T, now=T+30)
    frame = _drawn(radar_engine._radar_frame('iem-nexrad-n0b', T, ctx, pairs), pairs)
    assert frame['expectedSites'] == ['KNEA']
    assert radar_engine._radar_partial_coverage('iem-nexrad-n0b', frame, ctx) is False
    # A requested pair that was not acquired is still missing.
    frame = dict(frame, acquiredSites=[], siteScans=[dict(id='KNEA', ts=T-60)])
    assert radar_engine._radar_partial_coverage('iem-nexrad-n0b', frame, ctx) is True


def test_listing_failure_end_to_end(make_emitter, hybrid, multisite):
    hybrid.view()
    e = make_emitter()
    e.radar._acquire()
    r = e._build_payload()['radar']
    assert r['siteId'] == 'KNEA' and r['partialCoverage'] is False   # KFAR is not reporting: not expected
    newest = r['tiles']['frames'][-1]
    assert newest['expectedSites'] == ['KMID', 'KNEA']

    listed = radar_engine.RadarSession.open
    def fetch(self, req, timeout):
        if 'operation=list' in req.full_url and parse_qs(urlsplit(req.full_url).query)['radar'] == ['MID']:
            raise urllib.error.HTTPError(req.full_url, 503, 'unavailable', {}, None)
        return listed(self, req, timeout)
    radar_engine.RadarSession.open = fetch
    try:
        hybrid.mono += 400
        hybrid.view()
        e = make_emitter()
        e.radar._acquire()
        r = e._build_payload()['radar']
    finally:
        radar_engine.RadarSession.open = listed
    assert r['available'] and r['siteId'] == 'KNEA'
    newest = r['tiles']['frames'][-1]
    assert [p['id'] for p in newest['siteScans']] == ['KNEA']
    assert newest['expectedSites'] == ['KMID', 'KNEA']
    assert r['partialCoverage'] is True


# ------------------------------------------- must-fix 2: a spatial grid per native tile


def flat_scan(code, lat=47.61, lon=-122.33):
    codes = np.full((720, 1840), code, np.uint8)
    return Scan(lat, lon, 100., .5, 215, 1789257600, codes, (np.arange(3600) // 5).astype(np.int32))


def tile_of(lat, lon, z):
    n = 2**z
    return int((lon+180)/360*n), int((1-math.asinh(math.tan(math.radians(lat)))/math.pi)/2*n)


def test_grid_bits_are_cells_wholly_measured():
    covered = np.ones((256, 256), bool)
    covered[3*16+5, 5*16+9] = False          # one pixel of cell (row 3, column 5)
    cells = mosaic.grid_cells(mosaic.measured_grid(covered))
    assert cells.sum() == 255 and not cells[3, 5]
    assert mosaic.measured_grid(np.ones((256, 256), bool)) == 'f' * 64
    for bad in ('F'*64, 'f'*63, 'g'*64, ' '+'f'*63, None):
        with pytest.raises(ValueError):
            mosaic.grid_cells(bad)


def test_native_tiles_carry_a_grid_from_the_validity_mask():
    palette = source_palette('iem-nexrad-n0b')
    x, y = tile_of(47.61, -122.33, 7)
    inside, _ = mosaic.render_mosaic([flat_scan(0)], 7, x, y, palette)
    edge, _ = mosaic.render_mosaic([flat_scan(0)], 7, x+1, y, palette)       # reaches past 230 km
    missing, _ = mosaic.render_mosaic([flat_scan(1)], 7, x, y, palette)      # missing gates
    assert inside.info['radarMeasuredGrid'] == 'f'*64 and inside.info['radarUncoveredPixels'] == 0
    assert missing.info['radarMeasuredGrid'] == '0'*64
    cells = mosaic.grid_cells(edge.info['radarMeasuredGrid'])
    assert 0 < cells.sum() < 256 and edge.info['radarUncoveredPixels'] > 0
    assert cells[:, 0].all() and not cells[:, -1].all()    # measured to the west, not to the east
    assert l3.NATIVE_REVISION.endswith('v7')


def test_cached_tile_grid_must_agree_with_its_count(tmp_path, monkeypatch):
    from PIL.PngImagePlugin import PngInfo
    monkeypatch.setattr(radar_engine, 'RADAR_DIR', str(tmp_path/'radar'))
    x, y = tile_of(47.61, -122.33, 7)
    image, visible = mosaic.render_mosaic([flat_scan(0)], 7, x+1, y, source_palette('iem-nexrad-n0b'))
    good = dict(uncovered=str(image.info['radarUncoveredPixels']), grid=image.info['radarMeasuredGrid'])
    meta = dict(remapped=True, unmatchedColors=0, opaqueColors=0, unmatchedPixels=0, opaquePixels=visible,
                ambiguousPixels=0, revision=l3.NATIVE_REVISION)
    path = (Path(radar_engine.RADAR_DIR)/'t'/radar_engine._radar_render_revision('native')/'iem-nexrad-n0b'/('M'+'a'*24)/
            '202609130000'/'7'/str(x+1)/f'{y}.png')
    path.parent.mkdir(parents=True)
    def save(uncovered, grid):
        info = PngInfo()
        info.add_text('radarRemap', json.dumps(meta))
        info.add_text('radarVisiblePixels', str(visible))
        if uncovered is not None:
            info.add_text('radarUncoveredPixels', uncovered)
        if grid is not None:
            info.add_text('radarMeasuredGrid', grid)
        image.save(path, format='PNG', pnginfo=info)
    save(**good)
    assert radar_engine._radar_tile_metadata(path, 'iem-nexrad-n0b')['measuredGrid'] == good['grid']
    for bad in (dict(good, grid=None), dict(good, grid='f'*64), dict(good, grid='0'*63),
                dict(uncovered='0', grid=good['grid'])):
        save(**bad)
        with pytest.raises((KeyError, ValueError)):
            radar_engine._radar_tile_metadata(path, 'iem-nexrad-n0b')


# ---------------------- should-fix: health coverage follows the tile grids, not sampling


def _view_snapshot(lat, lon, zoom, frame):
    _, _, bounds, _ = radar_engine._radar_viewport(lat, lon, zoom, radar_engine.RADAR_VIEWPORT_W, radar_engine.RADAR_VIEWPORT_H)
    return radar_engine._RADAR_NONE._replace(available=True, reason=None, frames=(frame,), ts_frame=frame['ts'],
        source_id='iem-nexrad-n0b', source_mode='site', zoom=zoom, bounds=bounds, partial_coverage=False)


def _records(scans, lat, lon, zoom, key, stamp):
    records = {}
    tiles, _, _, _ = radar_engine._radar_viewport(lat, lon, zoom, radar_engine.RADAR_VIEWPORT_W, radar_engine.RADAR_VIEWPORT_H)
    palette = source_palette('iem-nexrad-n0b')
    for x, y, _, _ in tiles:
        image, _ = mosaic.render_mosaic(scans, zoom, x, y, palette)
        records[radar_engine._radar_disk_key('iem-nexrad-n0b', key, stamp, zoom, x, y, 'native')] = (
            None, 0, dict(measuredGrid=image.info['radarMeasuredGrid'], uncoveredPixels=image.info['radarUncoveredPixels']))
    return records


def _coverage(snap, records):
    e = object.__new__(radar_engine.RadarEngine)
    e._disk_inventory = SimpleNamespace(records=records)
    return e._health_coverage(snap)


def test_health_coverage_reads_measurement_holes(three_sites):
    T, key = 1789257600, 'M' + 'b'*24
    frame = dict(ts=T, mosaicKey=key, siteScans=[dict(id='KNEA', ts=T)], requestedPairs=[['KNEA', T]],
                 expectedSites=['KNEA'])
    snap = _view_snapshot(47.61, -122.33, 8, frame)
    measured = _records([flat_scan(0)], 47.61, -122.33, 8, key, T)
    assert _coverage(snap, measured) == 'full'
    # The same geometry with missing gates was 'full' before: now the holes count.
    holes = _records([flat_scan(1)], 47.61, -122.33, 8, key, T)
    assert _coverage(snap, holes) == 'partial'
    # A view tile not on disk yet proves nothing.
    assert _coverage(snap, dict(list(measured.items())[1:])) == 'unknown'
    assert _coverage(snap._replace(partial_coverage=True), measured) == 'partial'
    assert _coverage(snap._replace(available=False), measured) == 'unknown'


def test_health_coverage_sees_a_narrow_gap_between_discs(monkeypatch):
    # Two radars 470 km apart: their 230 km discs leave a 10 km gap that a
    # 17x9 sample grid can step over. The tiles' own masks cannot.
    lat, west = 40.0, -100.0
    east = west + math.degrees(470000 / (6371008.8 * math.cos(math.radians(lat))))
    monkeypatch.setattr(radar_engine, '_NEXRAD_SITES', {'KWWW': (lat, west, 'w'), 'KEEE': (lat, east, 'e')})
    T, key, mid = 1789257600, 'M' + 'c'*24, (west + east) / 2
    frame = dict(ts=T, mosaicKey=key, siteScans=[dict(id='KWWW', ts=T), dict(id='KEEE', ts=T)],
                 requestedPairs=[['KEEE', T], ['KWWW', T]], expectedSites=['KEEE', 'KWWW'])
    snap = _view_snapshot(lat, mid, 7, frame)
    records = _records([flat_scan(0, lat, west), flat_scan(0, lat, east)], lat, mid, 7, key, T)
    assert _coverage(snap, records) == 'partial'


def test_iem_site_layers_use_their_range_discs(three_sites):
    T = 1789257600
    frame = dict(ts=T, siteScans=[dict(id='KNEA', ts=T)], requestedPairs=[['KNEA', T]], expectedSites=['KNEA'])
    assert _coverage(_view_snapshot(47.61, -122.33, 8, frame), {}) == 'full'
    assert _coverage(_view_snapshot(47.61, -119.6, 8, frame), {}) == 'partial'   # ~205 km east: the edge shows
    x, y = tile_of(47.61, -122.33, 8)
    assert radar_engine._radar_disc_grid('KNEA', 8, x, y) == 'f'*64
    assert radar_engine._radar_disc_grid('KZZZ', 8, x, y) == '0'*64


# ----------------------------------- must-fix 3: radar health can never change /health


@pytest.mark.parametrize('content', [
    '{"summary": {"age": 1e999}}', '{"summary": {"age": -1e999}}', '{"writtenTs": 1e999}',
    '{"writtenTs": -1e999}', '{"hosts": [[1, 2, 1e999]]}', '{"writtenTs": %d}' % 10**400,
])
def test_overflowing_radar_numbers_leave_health_alone(serve_at, tmp_path, content):
    _, url = serve_at(_payload())
    (tmp_path/'radar-health.json').write_text(content)
    status, health = _get(url + '/health')
    assert (status, health['status']) == (200, 'ok')
    assert health['radar']['available'] is False and 'unreadable' in health['radar']['reason']


def test_radar_health_object_is_always_serializable(monkeypatch, tmp_path):
    module = _load_serve(monkeypatch, tmp_path, _payload())
    for content in ('{"a": 1e999}', '{"a": {"b": [-1e999]}}', '{"a": NaN}', '[' * 100000):
        (tmp_path/'radar-health.json').write_text(content)
        radar = module._radar_health()
        json.dumps(radar, allow_nan=False)
        assert radar['available'] is False
    (tmp_path/'radar-health.json').write_text('{"writtenTs": %r, "summary": {"state": "current"}}' % time.time())
    assert module._radar_health()['available'] is True


# ---------------------------------- should-fix: the forecast stamp means acquisition


def _forecast(monkeypatch, now):
    from lib import forecast as forecast_module
    from lib import properties
    clock = [now]
    monkeypatch.setattr(forecast_module.UNIX, 'time', lambda: clock[0])
    f = object.__new__(forecast_module.forecast)
    f.met_data = properties.Met()
    f.app = SimpleNamespace(
        config={'Station': {'Timezone': 'UTC'}, 'Display': {'TimeFormat': '24 hr'},
                'System': {'Hardware': 'Pi4', 'rest_api': '0'},
                'Units': {'Temp': 'c', 'Wind': 'mps', 'Direction': 'degrees', 'Precip': 'mm'}},
        CurrentConditions=SimpleNamespace(Met={}),
        Sched=SimpleNamespace(metDownload=SimpleNamespace(cancel=lambda: None)))
    hour = lambda t: dict(time=t, local_day=1, air_temperature=12.0, wind_avg=3.0, wind_gust=5.0,
                          wind_direction=180, icon='rainy', conditions='Rain', precip_probability=80,
                          precip=1.2, precip_type='rain')
    response = dict(forecast=dict(hourly=[hour(now-1800+3600*i) for i in range(6)],
                                  daily=[dict(day_num=1, air_temp_high=14.0, air_temp_low=8.0, precip_probability=90)]))
    return f, clock, response


def test_a_reformat_reparse_never_renews_the_forecast(monkeypatch):
    T = 1_800_000_000
    f, clock, response = _forecast(monkeypatch, T)
    f.success_forecast(None, response)
    assert f.met_data['UpdatedTs'] == T and f.met_data['PrecipPercnt'] != '--'
    clock[0] = T + 600
    f.fail_forecast()
    assert f.met_data['PrecipPercnt'] == '--' and f.met_data['UpdatedTs'] == T
    # An outage, then the user changes temperature units: main.py re-parses the
    # cached response. The values come back; their acquisition time does not move.
    clock[0] = T + 3000
    f.parse_forecast()
    assert f.met_data['PrecipPercnt'] != '--'
    assert f.met_data['UpdatedTs'] == T
    assert f.app.CurrentConditions.Met['UpdatedTs'] == T
    clock[0] = T + 3600
    f.success_forecast(None, response)
    assert f.met_data['UpdatedTs'] == T + 3600


# ------------------------------------------ should-fix: the writer thread owns fsync


def test_the_initiating_request_never_waits_on_fsync(server, tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    real = os.fsync
    def stalled(fd):
        entered.set()
        assert release.wait(10)
        real(fd)
    monkeypatch.setattr(server.os, 'fsync', stalled)
    done = threading.Event()
    def legacy_poll():
        # Loopback legacy preference path: the request that stages the values.
        _handler(server, '127.0.0.1', radarSeq=5, radarZoom=7, radarCenter='station')
        done.set()
    threading.Thread(target=legacy_poll, daemon=True).start()
    try:
        assert entered.wait(5), 'nothing was written'
        assert done.wait(2), 'the initiating request waited on an SD-card fsync'
        assert server._read_preference('radar_zoom') == '7'    # staged values read back at once
        # A newer decision staged during the stalled write wins.
        server._stage_preference('radar_zoom', ['6'])
    finally:
        release.set()
    server._flush_preferences()
    assert (tmp_path/'radar_zoom').read_text() == '6\n'
    assert not server._pref_pending


def test_a_touch_is_written_at_once_while_a_preference_write_stalls(server, tmp_path, monkeypatch):
    # The touch (presence, tmpfs) is no longer ordered behind any durable
    # preference: with the manual source gone there is nothing it could renew.
    entered, release = threading.Event(), threading.Event()
    persist = server._persist_preference
    def slow(name, value):
        entered.set()
        assert release.wait(10)
        persist(name, value)
    monkeypatch.setattr(server, '_persist_preference', slow)
    server._write_radar_zoom(['7'])
    try:
        assert entered.wait(5)
        server._note_presence()
        assert float((tmp_path/'presence').read_text()) > 0
        assert 'presence' not in server._pref_pending
    finally:
        release.set()
    server._flush_preferences()
    assert (tmp_path/'radar_zoom').read_text() == '7\n'


def test_shutdown_writes_a_debounced_preference(server, tmp_path):
    server._stage_preference('radar_smooth', ['on'], delay=3600)
    time.sleep(.05)
    assert not (tmp_path/'radar_smooth').exists()          # debounced: not due
    server._close_preferences()
    assert (tmp_path/'radar_smooth').read_text() == 'on\n'
    assert not server._pref_writer.is_alive()


def test_sigterm_flushes_staged_preferences(tmp_path):
    (tmp_path/'wx.json').write_text('{"ts": 1}')
    script = r'''
import os, runpy, signal, sys, threading, time
def stage():
    for _ in range(500):
        module = sys.modules.get('__main__')
        if getattr(module, '_pref_writer', 'x') is None and hasattr(module, 'Server'):
            break
        time.sleep(.01)
    time.sleep(.2)
    module._stage_preference('radar_smooth', ['on'], delay=3600)
    os.kill(os.getpid(), signal.SIGTERM)
threading.Thread(target=stage, daemon=True).start()
runpy.run_path(sys.argv[1], run_name='__main__')
'''
    env = dict(os.environ, WFP_DATA=str(tmp_path/'wx.json'), WFP_WEB=str(tmp_path), WFP_PORT='0', WFP_BIND='127.0.0.1')
    result = subprocess.run([sys.executable, '-c', script, str(Path('design/almanac/kiosk/serve.py').resolve())],
                            env=env, capture_output=True, text=True, timeout=30, cwd=str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert (tmp_path/'radar_smooth').read_text() == 'on\n'
