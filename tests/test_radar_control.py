"""Intent passes reuse control-plane knowledge without extending its lifetime."""
import urllib.error
from datetime import datetime, timezone

import pytest

from lib import almanac_emit as ae
from lib import radar_engine
from tests.fixtures.config import make_config
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401

MRMS = 'iem-mrms-lcref'
RV = 'rainviewer'
SITE = 'iem-nexrad-n0b'


def kinds(calls):
    return ['HEAD' if c[1] == 'HEAD' else 'META' if c[2] in (
        radar_engine.RADAR_IEM_METADATA_URL, radar_engine.RADAR_RAINVIEWER_MANIFEST_URL) else 'TILE' for c in calls]


def emitter_for(make_emitter, source):
    return make_emitter(config=make_config(Station={'Latitude': '52.52', 'Longitude': '13.4'})) if source == RV else make_emitter()


@pytest.mark.parametrize('source', [MRMS, RV])
@pytest.mark.parametrize('trigger', ['zoom', 'center', 'restart'])
def test_intent_only_tiles_and_validation_clock_does_not_slide(make_emitter, hybrid, tmp_path, source, trigger):
    emitter = emitter_for(make_emitter, source)
    emitter.radar._acquire()
    assert 'META' in kinds(hybrid.calls)
    validated = emitter.radar._newest[(source, None)][0]
    hybrid.mono = 50
    hybrid.calls.clear()
    if trigger == 'zoom':
        (tmp_path/'radar_zoom').write_text('7')
    elif trigger == 'center':
        (tmp_path/'radar_center').write_text('52.6,13.5' if source == RV else '47.7,-122.2')
    else:
        emitter.radar._restart = True
    # Force native requests even for a source-only/restart pass at the same crop.
    for path in (tmp_path/'radar').rglob('*.png'):
        path.unlink()
    for key in list(emitter.radar._disk_inventory.records):
        emitter.radar._invalidate_tile(key)
    emitter.radar._tiles.clear()
    emitter.radar._acquire()
    assert kinds(hybrid.calls) and set(kinds(hybrid.calls)) == {'TILE'}
    assert emitter.radar._newest[(source, None)][0] == validated
    assert emitter.radar._result.source_id == source


@pytest.mark.parametrize('source', [MRMS, RV])
@pytest.mark.parametrize('elapsed', ['before', 'boundary', 'after'])
def test_cadence_boundary(make_emitter, hybrid, source, elapsed):
    emitter = emitter_for(make_emitter, source); emitter.radar._acquire()
    cadence = radar_engine._RADAR_SOURCES[source]['cadence']
    hybrid.mono = cadence + {'before': -0.001, 'boundary': 0, 'after': 1}[elapsed]
    hybrid.calls.clear(); emitter.radar._acquire(intent_triggered=True)
    assert ('META' in kinds(hybrid.calls)) == (elapsed != 'before')
    assert 'HEAD' not in kinds(hybrid.calls)  # immutable positive probe survives TTL


@pytest.mark.parametrize('source', [MRMS, RV])
def test_scheduled_always_validates_even_changed_intent(make_emitter, hybrid, tmp_path, monkeypatch, source):
    emitter = emitter_for(make_emitter, source); emitter.radar._acquire()
    (tmp_path/'radar_zoom').write_text('7')
    hybrid.calls.clear()
    monkeypatch.setattr(emitter.radar, '_spawn', lambda key, worker: worker())
    emitter.radar._check()
    assert kinds(hybrid.calls)[0] == 'META'
    assert 'HEAD' not in kinds(hybrid.calls)


@pytest.mark.parametrize('source', [MRMS, RV])
def test_purged_remembered_tiles_revalidate_in_same_pass(make_emitter, hybrid, tmp_path, source):
    emitter = emitter_for(make_emitter, source); emitter.radar._acquire()
    old = emitter.radar._result.ts_frame
    hybrid.mono = 100
    if source == MRMS:
        hybrid.latest += 120
        purged = datetime.fromtimestamp(old, timezone.utc).strftime('%Y%m%d%H%M')
    else:
        hybrid.rv += 600
        hybrid.now += 600  # wall time can advance independently of monotonic TTL
        purged = '/v2/' + str(old) + '/'
    def fail(req, _):
        if purged in req.full_url and req.get_method() == 'GET':
            raise urllib.error.HTTPError(req.full_url, 404, 'purged', {}, None)
    hybrid.failure = fail
    (tmp_path/'radar_zoom').write_text('6' if source == RV else '7')
    hybrid.calls.clear(); emitter.radar._acquire()
    events = kinds(hybrid.calls)
    assert events[0] == 'TILE' and events.count('META') == 1
    assert emitter.radar._result.ts_frame > old
    assert emitter.radar._result.source_id == source
    assert emitter.radar._refresh['state'] == 'idle'


@pytest.mark.parametrize('source', [MRMS, RV])
def test_failure_discards_knowledge_for_next_intent(make_emitter, hybrid, tmp_path, source):
    emitter = emitter_for(make_emitter, source); emitter.radar._acquire()
    def fail(req, _):
        if req.full_url in (radar_engine.RADAR_IEM_METADATA_URL, radar_engine.RADAR_RAINVIEWER_MANIFEST_URL):
            raise ConnectionResetError('outage')
    hybrid.failure = fail
    emitter.radar._acquire(intent_triggered=False)
    assert (source, None) not in emitter.radar._newest
    hybrid.failure = None; hybrid.calls.clear()
    (tmp_path/'radar_zoom').write_text('7'); emitter.radar._acquire()
    assert kinds(hybrid.calls)[0] == 'META'


def test_archive_positive_and_negative_are_per_stamp(make_emitter, hybrid):
    emitter = make_emitter(); emitter.radar._acquire()
    url = next(c[2] for c in hybrid.calls if c[1] == 'HEAD')
    hybrid.calls.clear()
    hybrid.mono = 1000
    emitter.radar._archive_probe(MRMS, url, 1010)
    assert not hybrid.calls
    missing = url + '?missing'
    hybrid.failure = lambda req, _: (_ for _ in ()).throw(urllib.error.HTTPError(req.full_url, 404, 'missing', {}, None))
    for _ in range(2):
        with pytest.raises((ValueError, urllib.error.HTTPError)):
            emitter.radar._archive_probe(MRMS, missing, 1010)
    assert kinds(hybrid.calls) == ['HEAD']
    hybrid.mono += radar_engine.RADAR_NEGATIVE_CACHE_SEC
    with pytest.raises(urllib.error.HTTPError):
        emitter.radar._archive_probe(MRMS, missing, hybrid.mono+10)
    assert kinds(hybrid.calls) == ['HEAD', 'HEAD']


def test_site_listing_reuse_expiry_and_independent_site_failure(make_emitter, hybrid, multisite, tmp_path):
    emitter = make_emitter(); emitter.radar._acquire()
    original = dict(emitter.radar._newest)
    assert {c[1] for c in multisite.calls if c[0] == 'list'} == {'KNEA','KMID','KFAR'}
    multisite.calls.clear(); (tmp_path/'radar_zoom').write_text('9')
    hybrid.mono = 299; emitter.radar._acquire()
    assert multisite.calls and {c[0] for c in multisite.calls} == {'tile'}
    assert emitter.radar._newest == original
    # Only one site's invalidated knowledge needs a new listing.
    emitter.radar._forget(SITE, 'KMID')
    multisite.calls.clear(); emitter.radar._acquire(intent_triggered=True)
    assert [c for c in multisite.calls if c[0] == 'list'] == [('list','KMID')]
    hybrid.mono = 300; multisite.calls.clear()
    emitter.radar._acquire(intent_triggered=True)
    assert {c[1] for c in multisite.calls if c[0] == 'list'} == {'KNEA','KFAR'}
    multisite.calls.clear(); emitter.radar._acquire(intent_triggered=False)
    assert {c[1] for c in multisite.calls if c[0] == 'list'} == {'KNEA','KMID','KFAR'}


def test_purged_site_relists_in_pass(make_emitter, hybrid, multisite, tmp_path):
    emitter = make_emitter(); emitter.radar._acquire()
    old = hybrid.latest
    multisite.scans['KNEA'].append(old+120)
    purged = datetime.fromtimestamp(old, timezone.utc).strftime('%Y%m%d%H%M')
    def fail(site, url):
        if site == 'KNEA' and purged in url:
            raise urllib.error.HTTPError(url, 404, 'purged', {}, None)
    multisite.failure = fail
    multisite.calls.clear(); (tmp_path/'radar_zoom').write_text('9')
    emitter.radar._acquire()
    assert multisite.calls[0][0] == 'tile'
    assert {c[1] for c in multisite.calls if c[0]=='list'} == {'KNEA','KMID','KFAR'}
    assert emitter.radar._result.ts_frame == old+120


def test_prefetch_uses_validated_stamp_and_stops_at_expiry(make_emitter, hybrid, monkeypatch):
    # This test isolates mosaic tiers; cross-mode budgets have dedicated coverage.
    monkeypatch.setattr(radar_engine, '_NEXRAD_SITES', {})
    emitter = make_emitter(); hybrid.view()
    monkeypatch.setattr(radar_engine, 'RADAR_HISTORY_SEC', 0)
    original = emitter.radar._prefetch
    records = []
    def prefetch(source, ctx):
        hybrid.calls.clear()
        original(source, ctx)
        records.extend(hybrid.calls)
        emitter.radar._prefetched.clear()
        hybrid.mono = 120
        hybrid.view(); hybrid.calls.clear()
        original(source, ctx)
        assert not hybrid.calls
    monkeypatch.setattr(emitter.radar, '_prefetch', prefetch)
    emitter.radar._acquire()
    assert records and set(kinds(records)) == {'TILE'}
    stamp = datetime.fromtimestamp(hybrid.latest, timezone.utc).strftime('%Y%m%d%H%M')
    assert all(stamp in c[2] for c in records)


def test_remembered_viewed_history_has_no_control_requests(make_emitter, hybrid, tmp_path, monkeypatch):
    # This test isolates mosaic tiers; cross-mode budgets have dedicated coverage.
    monkeypatch.setattr(radar_engine, '_NEXRAD_SITES', {})
    emitter = make_emitter(); emitter.radar._acquire()
    hybrid.view(); monkeypatch.setattr(radar_engine, 'RADAR_HISTORY_SEC', 240)
    (tmp_path/'radar_zoom').write_text('7'); hybrid.calls.clear()
    emitter.radar._acquire()
    assert set(kinds(hybrid.calls)) == {'TILE'}
    assert sum(f['complete'] for f in emitter.radar._frames) == 3


@pytest.mark.parametrize('source', [MRMS, RV])
def test_first_intent_validates_then_supersede_during_tiles_reuses(make_emitter, hybrid, tmp_path, source):
    emitter = emitter_for(make_emitter, source)
    changed = []
    def supersede(req, _):
        if not changed and ('mrms::' in req.full_url or '/256/' in req.full_url):
            changed.append(True)
            (tmp_path/'radar_zoom').write_text('6')
    hybrid.failure = supersede
    emitter.radar._acquire(intent_triggered=True)
    assert kinds(hybrid.calls)[0] == 'META' and changed
    assert emitter.radar._restart and emitter.radar._result.ts_frame is None
    validated = emitter.radar._newest[(source, None)][0]
    hybrid.failure = None; hybrid.calls.clear(); hybrid.mono = 30
    emitter.radar._acquire()
    assert set(kinds(hybrid.calls)) == {'TILE'}
    assert emitter.radar._newest[(source, None)][0] == validated
    assert emitter.radar._result.available and emitter.radar._result.zoom == 6


@pytest.mark.parametrize('source', [MRMS, RV])
def test_tile_time_does_not_extend_validation_lifetime(make_emitter, hybrid, source):
    emitter = emitter_for(make_emitter, source)
    def slow(req, _):
        if 'mrms::' in req.full_url or '/256/' in req.full_url:
            hybrid.mono = 1
    hybrid.failure = slow
    emitter.radar._acquire(intent_triggered=True)
    assert emitter.radar._newest[(source, None)][0] == 0


def test_switch_back_reuses_each_sources_knowledge(make_emitter, hybrid, multisite, tmp_path):
    emitter = make_emitter()
    hybrid.pin('mosaic'); emitter.radar._acquire()
    mrms_validation = emitter.radar._newest[(MRMS, None)][0]
    hybrid.pin('site'); emitter.radar._acquire()
    assert emitter.radar._result.source_id == SITE
    assert any(c[0] == 'list' for c in multisite.calls)
    hybrid.mono = 10
    hybrid.calls.clear(); multisite.calls.clear()
    hybrid.pin('mosaic')
    (tmp_path/'radar_zoom').write_text('7'); emitter.radar._acquire()
    assert emitter.radar._result.source_id == MRMS
    assert set(kinds(hybrid.calls)) == {'TILE'}
    assert emitter.radar._newest[(MRMS, None)][0] == mrms_validation
    hybrid.pin('site')
    (tmp_path/'radar_zoom').write_text('9'); emitter.radar._acquire()
    assert emitter.radar._result.source_id == SITE
    assert multisite.calls and {c[0] for c in multisite.calls} == {'tile'}
