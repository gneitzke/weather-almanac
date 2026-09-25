"""Adversarial native-radar regressions. All acquisition is simulated in memory."""
import bz2
import io
import json
import socket
import struct
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from lib import almanac_emit as ae, radar_level3 as l3
from tests.test_radar_level3 import product, SITE, PALETTE, native  # noqa: F401
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401

SOURCE = 'iem-nexrad-n0b'
STAMP = 1789257600
NAME = 'NEA_N0B_' + datetime.fromtimestamp(STAMP + 24, timezone.utc).strftime('%Y_%m_%d_%H_%M_%S')


def altered(*, body_change=None, pdb_change=None, compress=False, tail=b'', gates=4):
    raw = bytearray(product(gates=gates, compress=False))
    offset = raw.index(b'\r\r\n', raw.index(b'\r\r\n') + 3) + 3
    body = raw[offset + 120:]
    if body_change:
        body_change(body)
    if pdb_change:
        pdb_change(raw, offset)
    struct.pack_into('>I', raw, offset + 102, len(body))
    raw[offset + 120:] = (bz2.compress(body) if compress else body) + tail
    struct.pack_into('>I', raw, offset + 8, len(raw) - offset)
    return bytes(raw)


@pytest.mark.parametrize('field,value', [(4, 0), (4, 31), (8, 2), (12, 0)])
def test_declared_block_and_layer_bounds_are_enforced(field, value):
    fmt = '>h' if field == 8 else '>I'
    with pytest.raises(ValueError, match='symbology header'):
        l3.decode(altered(body_change=lambda b: struct.pack_into(fmt, b, field, value)))


def test_missing_final_radial_padding_is_truncation():
    def trim(body):
        del body[-1]
        struct.pack_into('>I', body, 4, len(body))
        struct.pack_into('>I', body, 12, len(body) - 16)
    with pytest.raises(ValueError, match='radial header'):
        l3.decode(altered(body_change=trim, gates=3))


def test_extra_radial_bytes_are_not_silently_ignored():
    def append(body):
        body.extend(b'\0\0')
        struct.pack_into('>I', body, 4, len(body))
        struct.pack_into('>I', body, 12, len(body) - 16)
    with pytest.raises(ValueError, match='trailing radial'):
        l3.decode(altered(body_change=append))


@pytest.mark.parametrize('tail', [b'junk', bz2.compress(b'another stream')])
def test_compressed_trailers_are_refused(tail):
    with pytest.raises(ValueError, match='decompressed size'):
        l3.decode(altered(compress=True, tail=tail))


def test_bad_bzip_is_a_validation_error():
    raw = bytearray(altered(compress=True))
    raw[raw.index(b'BZh') + 10] ^= 255
    with pytest.raises(ValueError):
        l3.decode(bytes(raw))


@pytest.mark.parametrize('day,seconds', [(0, 100), (20709, 86400), (20709, 0xffffffff)])
def test_invalid_volume_clock_cannot_normalize_to_another_scan(day, seconds):
    with pytest.raises(ValueError, match='volume time'):
        l3.decode(altered(pdb_change=lambda b, o: struct.pack_into('>HI', b, o+40, day, seconds)))


def test_full_coverage_does_not_make_scrambled_radials_valid():
    def scramble(body):
        # Swap two intact rays; coverage is still 100%, but polar QC is wrong.
        body[40:50], body[50:60] = body[50:60], body[40:50]
    with pytest.raises(ValueError, match='radial order'):
        l3.decode(altered(body_change=scramble))


@pytest.mark.parametrize('delta', [-59, -1, 60, 119])
def test_missing_minute_never_borrows_an_adjacent_scan(delta):
    key = 'NEA_N0B_' + datetime.fromtimestamp(STAMP + delta, timezone.utc).strftime('%Y_%m_%d_%H_%M_%S')
    assert l3.match_key([key], STAMP) is None


def test_zoom8_sampling_change_has_a_new_immutable_identity(monkeypatch):
    current = ae._radar_render_revision('native')
    monkeypatch.setattr(l3, 'NATIVE_REVISION', 'level3-n0b-polar-v1')
    assert current != ae._radar_render_revision('native')
    assert len({current, ae._radar_render_revision(False), ae._radar_render_revision(True)}) == 3


@pytest.fixture
def scan_engine(make_emitter, monkeypatch):
    emitter = make_emitter()
    monkeypatch.setattr(emitter, '_radar_checkpoint', lambda ctx: None)
    monkeypatch.setattr(ae, '_NEXRAD_SITES', {'KNEA': (*SITE, 'nearest')})
    emitter._radar_session = SimpleNamespace(discard=lambda url: None)
    return emitter


def fake_acquisition(monkeypatch, emitter, fetch):
    # Exercise the real rate gate, body limits, validation and health accounting.
    monkeypatch.setattr(emitter._radar_session, 'open', fetch, raising=False)


def listing():
    return ('<ListBucketResult><Contents><Key>' + NAME + '</Key></Contents></ListBucketResult>').encode()


def test_simultaneous_tiles_share_one_scan_and_account_bytes(scan_engine, monkeypatch):
    emitter = scan_engine
    entered, release = threading.Event(), threading.Event()
    raw = product(volume_ts=STAMP + 24, gates=4)
    calls = []
    def fetch(req, timeout):
        calls.append(req.full_url)
        if '?' in req.full_url:
            return io.BytesIO(listing())
        entered.set()
        assert release.wait(3)
        return io.BytesIO(raw)
    fake_acquisition(monkeypatch, emitter, fetch)
    with ThreadPoolExecutor(6) as pool:
        owner = pool.submit(emitter._radar_level3_scan, 'KNEA', STAMP, {}, time.monotonic()+5)
        assert entered.wait(2)
        rest = [pool.submit(emitter._radar_level3_scan, 'KNEA', STAMP, {}, time.monotonic()+5) for _ in range(5)]
        release.set()
        scans = [f.result(3) for f in [owner] + rest]
    assert all(scan is scans[0] for scan in scans)
    assert len(calls) == len(emitter._radar_request_times) == 2
    assert emitter._radar_received_bytes == len(raw) + len(listing())
    assert not emitter._radar_level3_flights
    state = next(iter(emitter._radar_health.hosts.values()))
    assert state['metadata'], 'S3 recovery probes must use its listing'


def test_cancelled_owner_does_not_turn_waiters_into_new_owners(scan_engine, monkeypatch):
    emitter = scan_engine
    entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def request(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        raise ae._RadarSuperseded('changed variant')
    monkeypatch.setattr(emitter, '_radar_request', request)
    with ThreadPoolExecutor(2) as pool:
        owner = pool.submit(emitter._radar_level3_scan, 'KNEA', STAMP, {}, time.monotonic()+5)
        assert entered.wait(2)
        flight = emitter._radar_level3_flights[('KNEA', STAMP)]
        wait = flight['done'].wait
        def observe(timeout):
            waiting.set()
            return wait(timeout)
        monkeypatch.setattr(flight['done'], 'wait', observe)
        waiter = pool.submit(emitter._radar_level3_scan, 'KNEA', STAMP, {}, time.monotonic()+5)
        assert waiting.wait(2)
        release.set()
        for future in (owner, waiter):
            with pytest.raises(ae._RadarSuperseded):
                future.result(3)
    assert len(calls) == 1
    assert not emitter._radar_level3_flights and not emitter._radar_level3_failed


def test_waiter_honours_own_deadline_without_holding_radar_lock(scan_engine, monkeypatch):
    emitter = scan_engine
    entered, release = threading.Event(), threading.Event()
    def request(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        raise ae._RadarBudget('budget')
    monkeypatch.setattr(emitter, '_radar_request', request)
    with ThreadPoolExecutor(1) as pool:
        owner = pool.submit(emitter._radar_level3_scan, 'KNEA', STAMP, {}, time.monotonic()+5)
        assert entered.wait(2)
        try:
            with pytest.raises(TimeoutError, match='wait deadline'):
                emitter._radar_level3_scan('KNEA', STAMP, {}, time.monotonic()+.03)
            assert emitter._radar_lock.acquire(timeout=.1)
            emitter._radar_lock.release()
        finally:
            release.set()
        with pytest.raises(ae._RadarBudget):
            owner.result(3)
    assert not emitter._radar_level3_flights


def test_negative_cache_expires_and_preserves_local_failure_class(scan_engine, monkeypatch):
    emitter = scan_engine
    now, calls = [0.], []
    monkeypatch.setattr(ae.time, 'monotonic', lambda: now[0])
    def request(*args, **kwargs):
        calls.append(1)
        raise socket.gaierror('offline')
    monkeypatch.setattr(emitter, '_radar_request', request)
    for _ in range(2):
        with pytest.raises(OSError) as caught:
            emitter._radar_level3_scan('KNEA', STAMP, {}, 100)
        assert ae.failure_class(caught.value) == 'local'
    assert len(calls) == 1
    now[0] = 11
    with pytest.raises(socket.gaierror):
        emitter._radar_level3_scan('KNEA', STAMP, {}, 100)
    assert len(calls) == 2 and not emitter._radar_level3_flights
    assert not any(isinstance(v, BaseException) for v in emitter._radar_level3_failed[('KNEA', STAMP)])


@pytest.mark.parametrize('damage', ['corrupt', 'wrong-minute', 'truncated-listing'])
def test_invalid_products_and_listings_are_health_failures(scan_engine, monkeypatch, damage):
    emitter = scan_engine
    raw = product(volume_ts=STAMP + (60 if damage == 'wrong-minute' else 24), gates=4)
    def fetch(req, timeout):
        if '?' in req.full_url:
            return io.BytesIO(listing()[:-5] if damage == 'truncated-listing' else listing())
        return io.BytesIO(b'not level3' * 50 if damage == 'corrupt' else raw)
    fake_acquisition(monkeypatch, emitter, fetch)
    with pytest.raises(ValueError):
        emitter._radar_level3_scan('KNEA', STAMP, {}, time.monotonic()+10)
    state = next(iter(emitter._radar_health.hosts.values()))
    assert state['samples'][-1][1] is False
    assert emitter._radar_pass['counts']['host'] == 1
    assert emitter._radar_request_metrics[-1]['failureClass'] == 'host'
    assert not emitter._radar_level3_scans


def test_scan_cache_is_bounded_and_hits_update_lru(scan_engine, monkeypatch):
    emitter = scan_engine
    size = ae.RADAR_LEVEL3_SCAN_CACHE
    raw = product(volume_ts=STAMP+24, gates=4)
    def request(source, url, deadline, validate=None, **kwargs):
        if '?' in url:
            keys = ''.join('<Key>NEA_N0B_' + datetime.fromtimestamp(STAMP+i*60+24, timezone.utc).strftime('%Y_%m_%d_%H_%M_%S') + '</Key>' for i in range(size+2))
            payload = ('<ListBucketResult>'+keys+'</ListBucketResult>').encode()
        else:
            ts = l3.s3_key_time(url)
            payload = product(volume_ts=ts, gates=4)
        validate(payload)
        return payload
    monkeypatch.setattr(emitter, '_radar_request', request)
    for i in range(size):
        emitter._radar_level3_scan('KNEA', STAMP+i*60, {}, time.monotonic()+10)
    first = emitter._radar_level3_scan('KNEA', STAMP, {}, time.monotonic()+10)
    emitter._radar_level3_scan('KNEA', STAMP+size*60, {}, time.monotonic()+10)
    assert len(emitter._radar_level3_scans) == size
    assert emitter._radar_level3_scans[('KNEA', STAMP)] is first
    assert ('KNEA', STAMP+60) not in emitter._radar_level3_scans


def test_native_budget_prices_scans_instead_of_output_tiles(scan_engine, monkeypatch):
    emitter = scan_engine
    tiles = [(x, 1, 0, 0) for x in range(30)]
    monkeypatch.setattr(ae, '_radar_site_tiles', lambda ctx, site: tiles)
    ctx = dict(native=True, attention='live', zoom=7, inventory=emitter._radar_disk_inventory)
    pairs = [('KNEA', STAMP), ('KNEA', STAMP+60)]
    assert emitter._radar_frame_request_cost(SOURCE, ctx, pairs) == 6  # two hourly listings, two products per volume
    emitter._radar_level3_scans[('KNEA', STAMP)] = SimpleNamespace(volume_ts=STAMP+24)
    assert emitter._radar_frame_request_cost(SOURCE, ctx, pairs) == 5
    emitter._radar_level3_scans[('KNEA', STAMP+60)] = SimpleNamespace(volume_ts=STAMP+84)
    assert emitter._radar_frame_request_cost(SOURCE, ctx, pairs) == 3
    emitter._radar_level3_scans[('KNEA', STAMP+24, 'N0H')] = object()
    emitter._radar_level3_scans[('KNEA', STAMP+84, 'N0H')] = object()
    assert emitter._radar_frame_request_cost(SOURCE, ctx, pairs) == 0
    assert emitter._radar_frame_request_cost(SOURCE, dict(ctx, native=False), pairs) == 60


def test_native_transient_failure_keeps_published_frames(make_emitter, hybrid, multisite, native, monkeypatch):
    hybrid.view()
    emitter = make_emitter()
    emitter._do_radar()
    before = emitter._radar_result
    assert before.tiles['variant'] == 'native' and before.frames
    hybrid.mono += 61
    hybrid.latest += 60
    multisite.scans['KNEA'].append(hybrid.latest)
    opened = ae.RadarSession.open
    def fail(self, req, timeout):
        if req.full_url.startswith(ae.RADAR_LEVEL3_BUCKET):
            raise socket.gaierror('temporary outage')
        return opened(self, req, timeout)
    monkeypatch.setattr(ae.RadarSession, 'open', fail)
    emitter._do_radar(intent_triggered=False)
    after = emitter._radar_result
    assert after.tiles['variant'] == 'native'
    assert any(frame['complete'] for frame in after.frames)
    assert {f['ts'] for f in before.frames if f['complete']} <= {f['ts'] for f in after.frames}


def test_overlapping_azimuths_cannot_overwrite_other_rays():
    def overlap(body):
        struct.pack_into('>h', body, 34, 20)
    with pytest.raises(ValueError, match='radial order'):
        l3.decode(altered(body_change=overlap))


@pytest.mark.parametrize('key', [None, 123, 'NEA_N0B_99999999999999999999999_01_01_00_00_00', 'OTHER_N0B_2026_01_01_00_00_00'])
def test_hostile_key_is_not_a_timestamp(key):
    assert l3.s3_key_time(key) is None


def test_bzip_expansion_limit_is_enforced():
    raw = bytearray(altered(compress=True))
    offset = raw.index(b'\r\r\n', raw.index(b'\r\r\n')+3)+3
    raw[offset+120:] = bz2.compress(b'\0' * (l3.MAX_SYMBOLOGY_BYTES+100))
    struct.pack_into('>I', raw, offset+8, len(raw)-offset)
    struct.pack_into('>I', raw, offset+102, l3.MAX_SYMBOLOGY_BYTES)
    with pytest.raises(ValueError):
        l3.decode(bytes(raw))


@pytest.mark.parametrize('failure', ['breaker', 'outage', 'cooldown'])
def test_s3_outage_does_not_block_automatic_iem_fallback(make_emitter, hybrid, multisite, native, tmp_path, failure):
    hybrid.view()
    emitter = make_emitter()
    emitter._do_radar()
    assert emitter._radar_result.tiles['variant'] == 'native'
    if failure == 'cooldown':
        emitter._radar_cooldowns[ae.RADAR_LEVEL3_TRANSPORT] = hybrid.mono + 60
    elif failure == 'breaker':
        emitter._radar_health._host(ae.RADAR_LEVEL3_TRANSPORT, ae.RADAR_LEVEL3_BUCKET)['until'] = hybrid.mono + 60
    else:
        emitter._radar_level3_fallback(ConnectionError('unreachable'))
    assert emitter._radar_headroom_delay(SOURCE, 1) >= (60 if failure == 'cooldown' else 0)
    emitter._do_radar()
    assert emitter._radar_result.tiles['variant'] is False
    assert emitter._radar_result.frames and any(f['complete'] for f in emitter._radar_result.frames)
    assert ae.RADAR_LEVEL3_TRANSPORT not in emitter._radar_transport_sources(SOURCE)


def test_s3_recovery_is_a_required_dependency_only_for_native(scan_engine):
    emitter = scan_engine
    emitter._radar_native_requested = True
    emitter._radar_attention.tier = 'live'
    assert set(emitter._radar_transport_sources(SOURCE)) == {SOURCE, ae.RADAR_LEVEL3_TRANSPORT}
    assert emitter._radar_transport_sources('iem-mrms-lcref') == ('iem-mrms-lcref',)
