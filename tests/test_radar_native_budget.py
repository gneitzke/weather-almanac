"""Native acquisition is attention- and byte-bounded; no real networking."""
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lib import almanac_emit as ae
from lib import radar_native_budget as budget
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401
from tests.test_radar_level3 import native  # noqa: F401

SOURCE = 'iem-nexrad-n0b'


@pytest.mark.parametrize('tier,expected', [('live', 'native'), ('warm', 'native'), ('watch', 'native'), ('rest', False), ('dormant', False)])
@pytest.mark.parametrize('state', ['normal', 'newest-only', 'paused'])
def test_variant_is_exact_for_tier_and_ceiling(tier, expected, state):
    ctx = dict(native=True, attention=tier, native_ceiling=state)
    if state == 'paused': expected = False
    assert ae._radar_variant(ctx, SOURCE) == expected
    assert ae._radar_variant(ctx, 'iem-mrms-lcref') is False
    ctx['smooth'] = True
    assert ae._radar_variant(ctx, SOURCE) == ('native' if expected == 'native' else True)
    assert ae._radar_render_revision('native') != ae._radar_render_revision(True)


def test_ledger_boundaries_restart_concurrency_and_utc_rollover(tmp_path):
    clock = [1789257599.]
    mono = [0.]
    path = tmp_path/'radar_native_bytes.json'
    ledger = budget.NativeBudget(path, lambda: clock[0], lambda: mono[0])
    ledger.add(budget.NATIVE_NEWEST_ONLY_BYTES)
    assert ledger.snapshot()['ceilingState'] == 'normal'
    ledger.add(1)
    assert ledger.snapshot()['ceilingState'] == 'newest-only'
    ledger.persist()
    ledger = budget.NativeBudget(path, lambda: clock[0], lambda: mono[0])
    assert ledger.snapshot()['bytesToday'] == budget.NATIVE_NEWEST_ONLY_BYTES+1
    ledger.add(budget.NATIVE_PAUSE_BYTES-ledger.bytes)
    assert ledger.snapshot()['ceilingState'] == 'newest-only'
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(ledger.add, [100]*20))
    assert ledger.snapshot()['bytesToday'] == budget.NATIVE_PAUSE_BYTES+2000
    assert ledger.snapshot()['ceilingState'] == 'paused'
    mono[0] += 60
    ledger.persist()
    assert json.loads(path.read_text())['bytes'] == ledger.bytes
    assert not list(tmp_path.glob('*.tmp'))
    clock[0] += 1  # midnight UTC, independent of the station's timezone
    assert ledger.snapshot()['ceilingState'] == 'normal'
    assert ledger.snapshot()['bytesToday'] == 0
    ledger.add(20)
    mono[0] += 2
    ledger.persist()
    assert budget.NativeBudget(path, lambda: clock[0], lambda: mono[0]).snapshot()['bytesToday'] == 20


def test_atomic_counter_preserves_durable_symlink(tmp_path):
    durable = tmp_path/'durable'; durable.write_text('{}')
    path = tmp_path/'radar_native_bytes.json'; path.symlink_to(durable)
    ledger = budget.NativeBudget(path)
    ledger.add(123)
    ledger.persist()
    assert path.is_symlink() and json.loads(durable.read_text())['bytes'] == 123


@pytest.mark.parametrize('tier', ['rest', 'dormant'])
def test_unattended_tiers_never_acquire_level3(make_emitter, hybrid, multisite, native, monkeypatch, tier):
    monkeypatch.setattr(ae, 'RADAR_ATTENTION_MODE', 'active')
    emitter = make_emitter(); emitter._radar_attention.forced = tier; emitter._radar_attention.tier = tier
    emitter._do_radar()
    assert native.calls == []


def test_promotion_builds_native_even_when_listing_is_unchanged(make_emitter, hybrid, multisite, native, monkeypatch):
    monkeypatch.setattr(ae, 'RADAR_ATTENTION_MODE', 'active')
    emitter = make_emitter(); emitter._radar_attention.forced = 'watch'; emitter._radar_attention.tier = 'watch'
    emitter._do_radar()
    before = emitter._radar_result
    assert before.tiles['variant'] == 'native'
    assert len(before.frames[-1]['siteScans']) == 1
    emitter._radar_attention.forced = 'warm'; emitter._radar_attention.tier = 'warm'
    emitter._do_radar(discovery=True, intent_triggered=False)
    assert native.calls
    assert emitter._radar_result.tiles['variant'] == 'native'
    assert emitter._radar_result.ts_frame == before.ts_frame
    assert emitter._radar_result.tiles['revision'] == before.tiles['revision']
    assert len(emitter._radar_result.frames[-1]['siteScans']) > 1


def test_newest_only_has_no_native_history_or_prefetch(make_emitter, hybrid, multisite, native):
    hybrid.view()
    emitter = make_emitter()
    emitter._radar_native_budget.add(budget.NATIVE_NEWEST_ONLY_BYTES+1)
    emitter._do_radar()
    assert emitter._radar_result.tiles['variant'] == 'native'
    assert len(emitter._radar_result.frames) == 1
    products = [key for kind, key in native.calls if kind == 'get']
    assert products
    from lib.radar_level3 import s3_key_time
    assert all(s3_key_time(key) >= hybrid.latest-60 for key in products)
    assert emitter._radar_native_budget.snapshot()['bytesToday'] > budget.NATIVE_NEWEST_ONLY_BYTES+1


def test_hard_ceiling_uses_v1_until_next_utc_day(make_emitter, hybrid, multisite, native):
    emitter = make_emitter()
    emitter._radar_native_budget.add(budget.NATIVE_PAUSE_BYTES+1)
    emitter._do_radar()
    assert not native.calls
    assert emitter._radar_result.tiles['variant'] is False
    wire = emitter._build_payload()['radar']
    assert wire['nativeBudget']['ceilingState'] == 'paused'
    assert wire['health']['native'] == wire['nativeBudget']
    assert wire['nativeFallback'] == dict(active=True, reason='daily-limit', recovering=False) and not wire['native']
    with pytest.raises(ae._RadarSuperseded):
        emitter._radar_request(ae.RADAR_LEVEL3_TRANSPORT, ae.RADAR_LEVEL3_BUCKET+'blocked', 10)
    hybrid.now += 86400
    assert emitter._radar_native_budget.snapshot()['ceilingState'] == 'normal'


@pytest.mark.parametrize('body,invalid', [(b'<bad-listing/>', True), (b'product-bytes', True), (b'valid', False)])
def test_transport_counts_invalid_and_valid_bodies(make_emitter, monkeypatch, body, invalid):
    emitter = make_emitter(); emitter._radar_begin_log_pass()
    emitter._radar_session = ae.RadarSession()
    monkeypatch.setattr(emitter._radar_session, 'open', lambda *args, **kwargs: io.BytesIO(body))
    def validate(raw):
        if invalid: raise ValueError('invalid product or listing')
    if invalid:
        with pytest.raises(ValueError):
            emitter._radar_request(ae.RADAR_LEVEL3_TRANSPORT, ae.RADAR_LEVEL3_BUCKET+'object', ae.time.monotonic()+10, validate=validate)
    else:
        emitter._radar_request(ae.RADAR_LEVEL3_TRANSPORT, ae.RADAR_LEVEL3_BUCKET+'object', ae.time.monotonic()+10, validate=validate)
    assert emitter._radar_native_budget.snapshot()['bytesToday'] == len(body)
    assert emitter._radar_native_budget.flush()
    assert make_emitter()._radar_native_budget.snapshot()['bytesToday'] == len(body)


def test_threshold_crossing_supersedes_whole_variant_not_individual_tiles(make_emitter):
    emitter = make_emitter()
    emitter._radar_native_budget.add(budget.NATIVE_PAUSE_BYTES)
    ctx = dict(native=True, native_ceiling='newest-only', target_source=SOURCE)
    emitter._radar_native_budget.add(1)
    with pytest.raises(ae._RadarSuperseded, match='native daily budget'):
        emitter._radar_checkpoint(ctx)


def test_unknown_attention_never_grants_native():
    assert ae._radar_variant(dict(native=True), SOURCE) is False


def test_promotion_wakes_even_when_target_frame_count_falls(make_emitter, monkeypatch):
    monkeypatch.setattr(ae, 'RADAR_ATTENTION_MODE', 'active')
    emitter = make_emitter(); emitter._running = True; emitter._radar_native_requested = True
    emitter._radar_target_source = SOURCE
    emitter._radar_attention.tier = 'watch'
    before = emitter._radar_attention_knobs()
    emitter._radar_attention.tier = 'warm'
    wakes, discovery = [], []
    monkeypatch.setattr(emitter, '_schedule', lambda work, delay: wakes.append(delay))
    monkeypatch.setattr(emitter, '_radar_arm_discovery', lambda **kwargs: discovery.append(kwargs))
    emitter._radar_attention_changed(before, ae.time.time())
    assert wakes == [.1] and discovery == [dict(prompt=True)]


def test_native_ceiling_rollover_wakes_existing_watcher(make_emitter, monkeypatch):
    emitter = make_emitter(); emitter._running = True
    emitter._radar_zoom_stamp = emitter._radar_preference_stamp()
    emitter._radar_policy_ceiling = 'paused'
    emitter._radar_target_source = SOURCE
    wakes = []
    monkeypatch.setattr(emitter, '_spawn', lambda key, work: wakes.append(key))
    emitter._check_radar_zoom()
    assert wakes == ['radar']


def test_partial_body_is_counted_on_read_failure(make_emitter, monkeypatch):
    from http.client import IncompleteRead
    class Partial(io.BytesIO):
        def read(self, count=-1):
            chunk = super().read(count)
            if chunk: return chunk
            raise IncompleteRead(b'partial', 100)
    emitter = make_emitter(); emitter._radar_begin_log_pass(); emitter._radar_session = ae.RadarSession()
    monkeypatch.setattr(emitter._radar_session, 'open', lambda *args, **kwargs: Partial(b'first'))
    with pytest.raises(IncompleteRead):
        emitter._radar_request(ae.RADAR_LEVEL3_TRANSPORT, ae.RADAR_LEVEL3_BUCKET+'partial', ae.time.monotonic()+10)
    assert emitter._radar_native_budget.snapshot()['bytesToday'] == len(b'firstpartial')


def test_304_reuse_does_not_recount_cached_listing(make_emitter, monkeypatch):
    from urllib.error import HTTPError
    emitter = make_emitter(); emitter._radar_begin_log_pass(); emitter._radar_session = ae.RadarSession()
    url = ae.RADAR_LEVEL3_BUCKET+'?list-type=2'
    emitter._radar_metadata[url] = (b'cached-listing', {})
    def unchanged(*args, **kwargs): raise HTTPError(url, 304, 'unchanged', {}, None)
    monkeypatch.setattr(emitter._radar_session, 'open', unchanged)
    assert emitter._radar_request(ae.RADAR_LEVEL3_TRANSPORT, url, ae.time.monotonic()+10, metadata=True) == b'cached-listing'
    assert emitter._radar_native_budget.snapshot()['bytesToday'] == 0


def test_newest_failure_above_soft_ceiling_builds_only_one_native_frame(make_emitter, hybrid, multisite, native):
    emitter = make_emitter(); emitter._radar_native_budget.add(budget.NATIVE_NEWEST_ONLY_BYTES+1)
    # Failed newest scans may fall back, but must not build a history loop.
    multisite.scans['KNEA'].insert(-1, hybrid.latest-120)
    multisite.scans['KMID'].insert(-2, hybrid.latest-180)
    native.bad.update({hybrid.latest+24, hybrid.latest-60+24})
    emitter._do_radar()
    assert len(emitter._radar_result.frames) == 1


def test_unwritable_ledger_reports_persistence_failure(make_emitter, monkeypatch):
    emitter = make_emitter()
    monkeypatch.setattr(budget.os, 'replace', lambda *args: (_ for _ in ()).throw(OSError('read-only ledger')))
    emitter._radar_native_budget.add(20)
    emitter._radar_native_budget.persist()
    assert emitter._radar_native_budget.snapshot()['ledgerState'] == 'retrying'


@pytest.mark.parametrize('ceiling', [budget.NATIVE_NEWEST_ONLY_BYTES, budget.NATIVE_PAUSE_BYTES])
def test_native_ceiling_preserves_iem_prefetch(make_emitter, hybrid, multisite, native, monkeypatch, ceiling):
    emitter = make_emitter(); emitter._radar_native_budget.add(ceiling+1)
    emitter._do_radar()
    source, ctx = emitter._radar_idle_context
    warmed = []
    emitter._radar_prefetched.clear()
    original = emitter._radar_tile_batch
    def fill(target, *args, **kwargs):
        warmed.append((target, ae._radar_variant(args[1], target)))
        return original(target, *args, **kwargs)
    monkeypatch.setattr(emitter, '_radar_tile_batch', fill)
    monkeypatch.setattr(emitter, '_radar_headroom_delay', lambda *args: 0)
    emitter._radar_prefetch(source, dict(ctx, viewed=True, refresh=dict(state='idle')))
    assert warmed
    assert all(variant == ('native' if ceiling == budget.NATIVE_NEWEST_ONLY_BYTES else False)
               for target, variant in warmed if target == SOURCE)
    assert any(target == 'iem-mrms-lcref' for target, _ in warmed)
