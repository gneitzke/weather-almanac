"""Hybrid radar transport is fully simulated; only the explicit palette check is online."""
import io
import json
import os
import re
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path

import pytest
from PIL import Image

from lib import almanac_emit as ae
from tests.fixtures.config import make_config


def png(color=(0, 204, 0, 255), size=(256, 256)):
    stream = io.BytesIO()
    Image.new('RGBA', size, color).save(stream, 'PNG')
    return stream.getvalue()


@pytest.fixture
def hybrid(tmp_path, monkeypatch):
    # This transport fixture exercises Region/IEM; Auto and native have their own fixtures.
    (tmp_path / 'radar_source').write_text('mosaic')
    # Keep real policy in the general transport fixture. IEM topology tests
    # explicitly select fallback; the native fixture restores real policy.
    primary_only = ae.AlmanacEmitter._radar_primary_only
    level3_down = ae.AlmanacEmitter._radar_level3_down
    latest = int(datetime(2026, 9, 13, 0, 2, tzinfo=timezone.utc).timestamp())
    state = SimpleNamespace(latest=latest, rv=latest - 120, now=latest + 360,
        mono=0., calls=[], failure=None, tile=png(), metadata=None, conditional=False)
    os.utime(tmp_path / 'radar_source', (state.now, state.now))
    monkeypatch.setattr(ae, 'RADAR_DIR', str(tmp_path / 'radar'))
    monkeypatch.setattr(ae.time, 'time', lambda: state.now + state.mono)
    monkeypatch.setattr(ae.time, 'monotonic', lambda: state.mono)

    def fetch(req, timeout):
        url, method = req.full_url, req.get_method()
        provider = 'iem' if 'iastate.edu' in url else 'rainviewer'
        state.calls.append((provider, method, url, state.mono, timeout))
        assert 0 < timeout <= ae.RADAR_HTTP_TIMEOUT_SEC
        if state.failure:
            state.failure(req, timeout)
        if url == ae.RADAR_IEM_METADATA_URL:
            if state.conditional and req.get_header('If-none-match'):
                raise urllib.error.HTTPError(url, 304, 'unchanged', {}, None)
            meta = state.metadata or dict(end_valid=datetime.fromtimestamp(state.latest, timezone.utc).isoformat(),
                                         product='lcref', units='0.5 dBZ')
            raw = json.dumps(dict(meta=meta)).encode()
        elif url == ae.RADAR_RAINVIEWER_MANIFEST_URL:
            raw = json.dumps(dict(host='https://tiles.example', radar=dict(past=[
                dict(time=state.rv - offset, path='/v2/' + str(state.rv - offset))
                for offset in range(0, 7201, 600)]))).encode()
        elif url.startswith(ae.RADAR_SITE_LIST_URL):
            raw = b'{"scans":[]}'
        elif method == 'HEAD':
            assert '/archive/data/' in url
            raw = b''
        else:
            assert '/mrms::lcref-' in url or '/256/' in url
            raw = state.tile
        response = io.BytesIO(raw)
        response.status = 200
        response.headers = {'ETag': '"radar-test"'}
        return response

    monkeypatch.setattr(ae.RadarSession, 'open', lambda self, *a, **k: fetch(*a, **k))
    state.primary_only = primary_only
    state.level3_down = level3_down
    state.view = lambda: (tmp_path / 'radar_viewed').write_text(str(ae.time.time()))
    return state


@pytest.mark.parametrize('lat,lon,eligible', [
    (47.61, -122.33, True), (52.52, 13.4, False), (-33.87, 151.21, False),
    (21.31, -157.86, False), (61.22, -149.9, False), (18.46, -66.1, False),
    (48.95, -122.45, True), (49.05, -122.45, False),  # Blaine / Canada
    (32.72, -117.16, True), (32.45, -117.0, False),  # San Diego / Mexico
    (42.36, -83.07, True), (42.28, -82.95, False),   # Detroit / Windsor
    (45, -100, True), (float('nan'), -122, False), (0, 0, False)])
def test_conus_station_center_mask(lat, lon, eligible):
    assert ae._radar_iem_eligible(lat, lon) is eligible


@pytest.mark.parametrize('lat,lon,source', [
    (47.61, -122.33, 'iem-mrms-lcref'), (52.52, 13.4, 'rainviewer'),
    (-33.87, 151.21, 'rainviewer'), (21.31, -157.86, 'rainviewer'),
    (61.22, -149.9, 'rainviewer')])
def test_source_selection(make_emitter, hybrid, lat, lon, source):
    emitter = make_emitter(config=make_config(Station={'Latitude': str(lat), 'Longitude': str(lon)}))
    emitter._do_radar()
    r = emitter._build_payload()['radar']
    assert r['available'] and r['sourceId'] == source
    assert r['completeFrameCount'] == 1 and r['frameSpacingSec'] is None
    assert r['cadenceSec'] == (120 if source.startswith('iem') else 600)
    assert r['legend']['id'] == ae._RADAR_SOURCES[source]['legend']['id']
    assert r['updatedAt'] != r['observedAt'] and r['tiles']['frames'][-1]['at'] == r['observedAt']
    if source == 'rainviewer':
        assert all(c[0] == 'rainviewer' for c in hybrid.calls)


def test_primary_first_fallback_and_recovery(make_emitter, hybrid):
    def fail(req, _):
        if 'iastate.edu' in req.full_url:
            raise urllib.error.URLError('IEM down')
    hybrid.failure = fail
    emitter = make_emitter()
    for _ in range(3): emitter._do_radar(intent_triggered=False)
    assert emitter._radar_result.source_id == 'rainviewer'
    assert [c[0] for c in hybrid.calls[:3]] == ['iem']*3
    hybrid.failure = None; hybrid.calls.clear()
    hybrid.mono += 301; hybrid.latest += 240
    emitter._do_radar()
    assert emitter._radar_result.source_id == 'iem-mrms-lcref'
    assert all(c[0] == 'iem' for c in hybrid.calls)


def test_partial_iem_history_wins_and_backprobe_rolls_midnight(make_emitter, hybrid):
    hybrid.view()
    def fail(req, _):
        if '202609130002' in req.full_url or '202609130000' in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 503, 'processing', {}, None)
    hybrid.failure = fail
    emitter = make_emitter(); emitter._do_radar()
    r = emitter._build_payload()['radar']
    assert r['sourceId'] == 'iem-mrms-lcref' and r['observedTs'] == hybrid.latest - 240
    assert any('/2026/09/12/GIS/mrms/lcref_202609122358.png' in c[2] for c in hybrid.calls)
    assert any('mrms::lcref-202609122358/' in c[2] for c in hybrid.calls)
    assert all(c[0] == 'iem' for c in hybrid.calls)
    assert r['frameSpacingSec'] == 120 and r['historySpanSec'] < 3600
    assert r['completeFrameCount'] < 31 and r['frameCount'] == 31
    assert r['legend'].get('snow') is None


@pytest.mark.parametrize('bad', ['stale', 'future', 'odd', 'schema', 'red', 'corrupt', 'size', 'missing'])
def test_unusable_primary_falls_back_after_three_passes(make_emitter, hybrid, bad):
    if bad == 'stale': hybrid.latest -= 7200
    if bad == 'future': hybrid.latest += 7200
    if bad == 'odd': hybrid.latest -= 60
    if bad == 'schema': hybrid.metadata = dict(end_valid='bad')
    def fail(req, _):
        if 'mrms::' in req.full_url:
            if bad == 'red': hybrid.tile = png((255, 0, 0, 255))
            if bad == 'corrupt': hybrid.tile = b'bad'
            if bad == 'size': hybrid.tile = png(size=(128, 256))
        if '/256/' in req.full_url: hybrid.tile = png()
        if bad == 'missing' and req.get_method() == 'HEAD':
            raise urllib.error.HTTPError(req.full_url, 404, 'missing archive', {}, None)
    hybrid.failure = fail
    emitter = make_emitter()
    for _ in range(3): emitter._do_radar(intent_triggered=False)
    assert emitter._radar_result.source_id == 'rainviewer' and emitter._radar_available
    assert hybrid.calls[0][0] == 'iem' and any(c[0] == 'rainviewer' for c in hybrid.calls)


def test_valid_clear_iem_does_not_fallback(make_emitter, hybrid):
    hybrid.tile = png((0, 0, 0, 0))
    emitter = make_emitter(); emitter._do_radar()
    assert emitter._radar_result.source_id == 'iem-mrms-lcref'
    assert all(c[0] == 'iem' for c in hybrid.calls)


def test_same_frame_refresh_updated_failed_refresh_neither(make_emitter, hybrid):
    emitter = make_emitter(); emitter._do_radar()
    first = emitter._build_payload()['radar']
    hybrid.mono += 60; hybrid.calls.clear(); hybrid.conditional = True
    emitter._do_radar()
    second = emitter._build_payload()['radar']
    assert second['observedAt'] == first['observedAt']
    assert second['updatedAt'] != first['updatedAt'] and second['fetchedAt'] == first['fetchedAt'] + 60
    assert len(hybrid.calls) == 1 and hybrid.calls[0][2] == ae.RADAR_IEM_METADATA_URL
    previous = emitter._radar_result
    hybrid.mono += 60
    hybrid.failure = lambda *_: (_ for _ in ()).throw(urllib.error.URLError('all down'))
    emitter._do_radar()
    assert emitter._radar_result is previous
    assert emitter._build_payload()['radar']['fetchedAt'] == second['fetchedAt']


def test_failed_advertised_frame_does_not_advance_updated(make_emitter, hybrid):
    emitter = make_emitter(); emitter._do_radar()
    first = emitter._radar_result
    hybrid.mono += 120; hybrid.latest += 120
    def fail(req, _):
        if '202609130004' in req.full_url:
            raise urllib.error.URLError('new slot processing')
    hybrid.failure = fail
    emitter._do_radar()
    assert emitter._radar_result.source_id == 'iem-mrms-lcref'
    assert emitter._radar_result.ts_frame == first.ts_frame
    assert emitter._radar_result.ts_fetch == first.ts_fetch






def test_unviewed_counts_and_open_warms_unchanged(make_emitter, hybrid, monkeypatch):
    monkeypatch.setattr(ae, 'RADAR_REQUESTS_PER_MIN', 90)  # the counts below are budget-relative
    monkeypatch.setattr(ae, 'RADAR_HISTORY_RESERVE', 17)
    emitter = make_emitter(); emitter._do_radar()
    assert len(hybrid.calls) == 14  # metadata + HEAD + 12 visible tiles
    assert sum(f['complete'] for f in emitter._radar_frames) == 1
    hybrid.calls.clear(); emitter._do_radar()
    assert len(hybrid.calls) == 1
    hybrid.view(); hybrid.mono += 60; hybrid.calls.clear(); emitter._do_radar()
    assert len(hybrid.calls) <= 90 and sum(f['complete'] for f in emitter._radar_frames) > 1


def test_build_limit_and_real_gap_spacing(make_emitter, hybrid, monkeypatch):
    # Isolate mosaic history/cache accounting from cross-mode discovery.
    monkeypatch.setattr(ae, '_NEXRAD_SITES', {})
    monkeypatch.setattr(ae, 'RADAR_REQUESTS_PER_MIN', 1000)
    monkeypatch.setattr(ae, 'RADAR_MAX_FRAME_BUILDS_PER_PASS', 3)
    hybrid.view()
    def fail(req, _):
        if '202609130000' in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 404, 'hole', {}, None)
    hybrid.failure = fail
    emitter = make_emitter(); emitter._do_radar()
    r = emitter._build_payload()['radar']
    assert r['completeFrameCount'] == 2 and r['historyGaps']
    assert r['frameSpacingSec'] == 240 and r['cadenceSec'] == 120
    assert len(hybrid.calls) == 28  # metadata, two visible grids, three HEADs
    assert sum('/7/' in c[2] or '/9/' in c[2] for c in hybrid.calls) == 0  # no optional work before four


def test_negative_archive_cache_and_expiry(make_emitter, hybrid):
    def fail(req, _):
        if '202609130002' in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 404, 'missing', {}, None)
    hybrid.failure = fail
    emitter = make_emitter(); emitter._do_radar()
    hybrid.calls.clear(); emitter._do_radar()
    assert not any('202609130002' in c[2] for c in hybrid.calls)
    hybrid.mono += 120; hybrid.calls.clear(); hybrid.failure = None
    emitter._do_radar()
    assert emitter._radar_ts_frame == hybrid.latest
    assert any('202609130002' in c[2] for c in hybrid.calls)


@pytest.mark.parametrize('retry', ['180', 'Sun, 13 Sep 2026 00:09:00 GMT'])
def test_429_cooldown_retains_source_until_retry(make_emitter, hybrid, retry):
    def fail(req, _):
        if 'iastate.edu' in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 429, 'slow down', {'Retry-After': retry}, None)
    hybrid.failure = fail
    emitter = make_emitter(); emitter._do_radar()
    assert not emitter._radar_result.available
    assert sum(c[0] == 'iem' for c in hybrid.calls) == 1
    hybrid.failure = None; hybrid.calls.clear(); emitter._do_radar()
    assert not hybrid.calls
    hybrid.mono = 180; hybrid.calls.clear(); emitter._do_radar()
    assert hybrid.calls[0][0] == 'iem' and emitter._radar_result.source_id == 'iem-mrms-lcref'


def test_primary_deadline_leaves_time_for_fallback(make_emitter, hybrid):
    def fail(req, timeout):
        if req.get_method() == 'HEAD':
            hybrid.mono += timeout
            raise TimeoutError('archive stalled')
    hybrid.failure = fail
    emitter = make_emitter()
    # A timeout becomes an outage only after consecutive failed passes.
    for _ in range(3):
        pass_start = hybrid.mono
        emitter._do_radar()
        assert hybrid.mono - pass_start <= ae.RADAR_BUILD_DEADLINE_SEC
    assert emitter._radar_result.available and emitter._radar_result.source_id == 'rainviewer'
    # The third pass still has time for fallback after its 10-second request
    # fails. Measure from that pass's start, not the sum of three passes.
    fallback_at = next(c[3] for c in hybrid.calls if c[0] == 'rainviewer')
    assert fallback_at - pass_start < ae.RADAR_BUILD_DEADLINE_SEC


def test_source_stale_thresholds_and_dst_local_labels(make_emitter, hybrid):
    emitter = make_emitter(); emitter._do_radar()
    snap = emitter._radar_result
    tz = ae.AlmanacEmitter._station_tz(emitter.app.config)
    ss = ae._RADAR_SOURCES['iem-mrms-lcref']['stale_sec']
    assert not ae.AlmanacEmitter._radar_payload(snap, snap.ts_frame + ss - 1, tz)['stale']
    assert ae.AlmanacEmitter._radar_payload(snap, snap.ts_frame + ss, tz)['stale']
    times = [int(datetime(2026, 11, 1, h, 30, tzinfo=timezone.utc).timestamp()) for h in (8, 9)]
    dst = snap._replace(tiles=dict(snap.tiles,frames=[dict(ts=t,stamp=datetime.fromtimestamp(t,timezone.utc).strftime('%Y%m%d%H%M'),siteScans=[],levels={'8':True}) for t in times]))
    assert [f['at'] for f in ae.AlmanacEmitter._radar_payload(dst, times[-1], tz)['tiles']['frames']] == ['01:30', '01:30']


@pytest.mark.skipif(os.environ.get('RADAR_NET_TEST') != '1', reason='opt in with RADAR_NET_TEST=1')
def test_legend_fidelity_real_iem_native_colortable():
    """Each representative stop must match the documented index/value/RGB row.

    A live wet crop need not contain 71 dBZ; the provider's full native table
    covers the entire reflectivity range deterministically, including extremes.
    """
    url = 'https://mesonet.agron.iastate.edu/GIS/rasters.php?rid=4'
    context = ssl.create_default_context()
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    req = urllib.request.Request(url, headers={'User-Agent': 'WeatherAlmanac'})
    try:
        with urllib.request.urlopen(req, timeout=25, context=context) as response:
            html = response.read().decode()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        pytest.skip(f'IEM offline: {error}')
    rows = [re.findall(r'<td[^>]*>(.*?)</td>', row, re.S) for row in re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.S)]
    for dbz, hexa in ((11, '#a4a4ff'), (25, '#3366cc'), (35, '#00cc00'), (45, '#ffcc00'), (55, '#d90000'), (65, '#cc00cc'), (71, '#ffffff')):
        index = int((dbz + 32) * 2)
        matches = [row for row in rows if len(row) >= 6 and re.sub('<[^>]+>', '', row[0]).strip() == str(index)]
        assert len(matches) == 1, f'index {index} missing from native table'
        text = [re.sub('<[^>]+>', '', cell).strip() for cell in matches[0]]
        assert float(text[1]) == dbz and text[5].lower() == hexa
        assert tuple(map(int, text[2:5])) == tuple(bytes.fromhex(hexa[1:]))






@pytest.mark.parametrize('lat,lon', [(25.76, -80.19), (40.71, -74.0), (42.88, -78.88)])
def test_coastal_conus_cities_eligible(lat, lon):
    assert ae._radar_iem_eligible(lat, lon)
