"""Site discovery and opposite-mode newest share the lazy native tile cache."""
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from lib import almanac_emit as ae
from lib import radar_engine
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401

MRMS = 'iem-mrms-lcref'
SITE = 'iem-nexrad-n0b'


@pytest.fixture
def scene(make_emitter, hybrid, multisite, tmp_path, monkeypatch):
    hybrid.pin('mosaic')
    emitter = make_emitter()
    contexts = []
    publish = emitter.radar._publish_refresh
    def capture(ctx, **changes):
        publish(ctx, **changes)
        contexts.append(ctx)
    monkeypatch.setattr(emitter.radar, '_publish_refresh', capture)
    emitter.radar._acquire()  # one cold unviewed newest, no background tiers
    ctx = contexts[-1]
    ctx['viewed'] = True
    hybrid.view()
    hybrid.calls.clear(); multisite.calls.clear()
    return emitter, ctx


def test_listings_overlap_barrier_and_primary_is_not_arrival_order(scene, multisite, monkeypatch):
    emitter, ctx = scene
    barrier = threading.Barrier(3)
    opened = radar_engine.RadarSession.open
    def listing(session, req, *args, **kwargs):
        if 'operation=list' in req.full_url:
            barrier.wait(timeout=5)  # serial discovery cannot pass
        return opened(session, req, *args, **kwargs)
    monkeypatch.setattr(radar_engine.RadarSession, 'open', listing)
    stamps, _ = emitter.radar._site_discover(ctx)
    assert ctx['site_id'] == 'KNEA'
    assert stamps == tuple(multisite.scans['KNEA'])
    assert sorted(multisite.calls) == [('list','KFAR'), ('list','KMID'), ('list','KNEA')]


@pytest.mark.parametrize('gate', ['unviewed', 'off_tab', 'busy', 'unavailable', 'headroom', 'floor'])
def test_site_warming_admission(scene, hybrid, multisite, tmp_path, monkeypatch, gate):
    emitter, ctx = scene
    if gate == 'unviewed': ctx['viewed'] = False
    elif gate == 'off_tab': (tmp_path/'radar_viewed').unlink()
    elif gate == 'busy': ctx['refresh']['state'] = 'history'
    elif gate == 'unavailable': ctx['sources'][1]['available'] = False
    elif gate == 'floor': ctx['zoom'] = 6
    else: monkeypatch.setattr(emitter.radar, '_headroom_delay', lambda *args: 60)
    emitter.radar._prefetch(MRMS, ctx)
    assert not multisite.calls
    assert not any(k[0] == SITE for k in emitter.radar._tiles)


def test_site_warming_lru_identity_once_and_no_history(scene, hybrid, multisite):
    emitter, ctx = scene
    before = emitter.radar._result
    emitter.radar._prefetch(MRMS, ctx)
    assert emitter.radar._result is before  # no background crops/publication
    for zoom in (7,8,9):
        tiles, _, bounds, _ = radar_engine._radar_viewport(47.61,-122.33,zoom,956,490)
        warm = dict(ctx, zoom=zoom, tiles=tiles, bounds=bounds)
        stamps, _ = emitter.radar._site_discover(dict(warm, intent_triggered=True))
        warm.update(site_scans={s:tuple(v) for s,v in multisite.scans.items()},
                    sites=[dict(s,reporting=bool(multisite.scans[s['id']])) for s in radar_engine._radar_sites(ctx['station'],bounds)[0]])
        for site, ts in radar_engine._radar_site_pairs(warm, stamps[-1]):
            for tx,ty,_,_ in radar_engine._radar_site_tiles(warm,site):
                assert (SITE,site,None,ts,zoom,tx,ty) in emitter.radar._tiles
    assert {k[3] for k in emitter.radar._tiles if k[0] == SITE} == {hybrid.latest, hybrid.latest-60}
    hybrid.calls.clear(); multisite.calls.clear()
    emitter.radar._prefetch(MRMS, ctx)
    assert not hybrid.calls and not multisite.calls


@pytest.mark.parametrize('mode,zoom', [('site',7), ('site',8), ('mosaic',6)])
def test_settled_mode_press_has_zero_listing_and_newest_requests(scene, hybrid, multisite, tmp_path, monkeypatch, mode, zoom):
    emitter, ctx = scene
    if mode == 'mosaic':
        # Begin in site mode at zoom 7 with no mosaic knowledge or tiles at
        # all; Auto's Site warms Region there, so zooming out to 6 is warm.
        emitter.radar._newest.clear(); emitter.radar._tiles.clear()
        for path in (tmp_path/'radar').rglob('*.png'): path.unlink()
        (tmp_path/'radar_viewed').unlink()
        hybrid.pin('site'); (tmp_path/'radar_zoom').write_text('7'); emitter.radar._acquire()
        source, site_ctx = emitter.radar._idle_context
        assert source == SITE
        hybrid.view()
        emitter.radar._prefetch(SITE, dict(site_ctx, viewed=True, refresh=dict(state='idle')))
    else:
        emitter.radar._prefetch(MRMS, ctx)
    hybrid.calls.clear(); multisite.calls.clear()
    cached_urls={radar_engine.RADAR_SITE_TILE_TEMPLATE.format(site=k[1][1:],stamp=ae.datetime.fromtimestamp(k[3],ae.timezone.utc).strftime('%Y%m%d%H%M'),z=k[4],x=k[5],y=k[6])
        if k[0]==SITE else radar_engine.RADAR_IEM_TILE_TEMPLATE.format(stamp=ae.datetime.fromtimestamp(k[3],ae.timezone.utc).strftime('%Y%m%d%H%M'),z=k[4],x=k[5],y=k[6]) for k in emitter.radar._tiles}
    # A switch stages four frames; newest remains a cache hit, history may fetch.
    (tmp_path/'radar_viewed').unlink()
    hybrid.pin(mode)
    (tmp_path/'radar_zoom').write_text(str(zoom))
    emitter.radar._acquire()
    assert emitter.radar._result.source_mode == mode
    assert emitter.radar._result.zoom == zoom
    assert emitter.radar._result.available
    assert all(c[0]=='tile' and c[2] not in cached_urls for c in multisite.calls)
    assert all(c[2] not in cached_urls and c[2]!=radar_engine.RADAR_IEM_METADATA_URL for c in hybrid.calls)


def test_intent_cancels_site_warming_at_tile_boundaries(scene, hybrid, multisite, tmp_path):
    emitter, ctx = scene
    def change(site, url):
        (tmp_path/'radar_intent').write_text('2')
    multisite.failure = change
    with pytest.raises(radar_engine._RadarSuperseded): emitter.radar._prefetch(MRMS, ctx)
    calls = [c for c in multisite.calls if c[0] == 'tile']
    assert 1 <= len(calls) <= radar_engine.RADAR_TILE_WORKERS
    cached = [k for k in emitter.radar._tiles if k[0] == SITE]
    assert len(cached) == len(calls)
    assert not emitter.radar._negative


def test_listing_failure_is_retried_independently(scene, multisite, monkeypatch):
    emitter, ctx = scene
    original = radar_engine.RadarSession.open
    failed = []
    def fail(session, req, *args, **kwargs):
        if 'operation=list' in req.full_url and parse_qs(urlsplit(req.full_url).query)['radar'] == ['MID']:
            failed.append(True)
            raise ConnectionResetError('listing failed')
        return original(session, req, *args, **kwargs)
    monkeypatch.setattr(radar_engine.RadarSession, 'open', fail)
    emitter.radar._site_discover(ctx)
    assert failed and (SITE,'KMID') not in emitter.radar._newest
    monkeypatch.setattr(radar_engine.RadarSession, 'open', original)
    multisite.calls.clear()
    # This is a new pass; a failed listing is shared only within its own pass.
    emitter.radar._site_discover(dict(ctx, intent_triggered=True, listing_results={}))
    assert multisite.calls == [('list','KMID')]


@pytest.mark.parametrize('zoom', [7, 9])
def test_site_mode_neighbour_press_is_a_cache_hit(scene, hybrid, multisite, tmp_path, zoom):
    emitter, ctx = scene
    (tmp_path/'radar_viewed').unlink()
    hybrid.pin('site'); emitter.radar._acquire()
    ctx.update(preference_stamp=emitter.radar._preference_stamp(), refresh=dict(state='idle'))
    hybrid.view(); emitter.radar._prefetch(SITE, ctx)
    warm=set(emitter.radar._tiles);hybrid.calls.clear(); multisite.calls.clear()
    (tmp_path/'radar_viewed').unlink()
    (tmp_path/'radar_zoom').write_text(str(zoom)); emitter.radar._acquire()
    assert emitter.radar._result.source_mode == 'site' and emitter.radar._result.zoom == zoom
    assert not hybrid.calls and all(c[0]=='tile' for c in multisite.calls)
    for _,site,url in multisite.calls:
        assert not any(k[0]==SITE and k[1]==site and url.endswith('/%s/%s/%s.png' % (k[4],k[5],k[6])) for k in warm)


def test_settled_mosaic_newest_precedes_cross_mode_warm_and_press(make_emitter, hybrid, multisite, tmp_path, monkeypatch):
    hybrid.pin('mosaic'); hybrid.view()
    emitter = make_emitter()
    request = emitter.radar._request
    events = []
    def record(source, url, *args, **kwargs):
        events.append((source,url))
        return request(source,url,*args,**kwargs)
    monkeypatch.setattr(emitter.radar, '_request', record)
    emitter.radar._acquire()
    first_site = next(i for i,(s,u) in enumerate(events) if s == SITE)
    preceding = [u for s,u in events[:first_site] if 'mrms::' in u]
    assert len(preceding) == 114  # eight visible grids, then newest margin
    assert 2<=sum(f['complete'] for f in emitter.radar._result.frames)<=8  # cross-mode tier may consume this pass's reserve
    # Let the real rolling window expire, retaining continuous viewed demand.
    hybrid.mono = 60; hybrid.view(); emitter.radar._acquire(intent_triggered=False)
    events.clear()
    publications = []
    publish = emitter.radar._publish_refresh
    def capture(ctx, **changes):
        publish(ctx, **changes)
        if emitter.radar._result.source_mode=='site': publications.append(sum(f['complete'] for f in emitter.radar._result.frames))
    monkeypatch.setattr(emitter.radar, '_publish_refresh', capture)
    hybrid.pin('site'); (tmp_path/'radar_zoom').write_text('7')
    emitter.radar._acquire()
    assert emitter.radar._result.source_mode == 'site'
    # Auto publishes a source switch once its newest frame is publishable; the
    # page keeps the old loop until four of the new one are decoded.
    assert publications and publications[0] >= 1
    assert not any('operation=list' in u for _,u in events)


def test_other_mode_metadata_and_tiles_use_background_reserve(scene, hybrid, tmp_path, monkeypatch):
    emitter, ctx = scene
    (tmp_path/'radar_viewed').unlink()
    hybrid.pin('site'); emitter.radar._acquire()
    ctx.update(preference_stamp=emitter.radar._preference_stamp())
    emitter.radar._forget(MRMS)
    hybrid.view()
    request = emitter.radar._request
    reserves = []
    def record(source, url, *args, **kwargs):
        reserves.append(kwargs.get('reserve', 0))
        return request(source,url,*args,**kwargs)
    monkeypatch.setattr(emitter.radar, '_request', record)
    emitter.radar._prefetch(SITE, ctx)
    assert reserves and set(reserves) == {emitter.radar._mandatory_reserve(SITE,ctx,[emitter.radar._result.ts_frame])}


def test_site_loop_cannot_evict_warmed_mode_or_zoom_tiles(scene, hybrid, multisite, tmp_path, monkeypatch):
    emitter, ctx = scene
    emitter.radar._prefetch(MRMS, ctx)
    protected = {k for k in emitter.radar._tiles if k[0] == SITE and k[4] in (7,9)}
    assert protected
    # Fill the LRU after the warm set: subsequent cold loop slots put real pressure
    # on the oldest entries. History crops can outlive their disposable raw tiles.
    for i in range(radar_engine.RADAR_TILE_CACHE_SIZE-len(emitter.radar._tiles)):
        emitter.radar._tiles[(MRMS,None,None,hybrid.latest-7200,6,i,1)] = hybrid.tile
    for site in ('KNEA','KMID'):
        multisite.scans[site] = [hybrid.latest-480*i-(60 if site=='KMID' else 0) for i in range(8)]
    emitter.radar._forget(SITE)
    monkeypatch.setattr(radar_engine, 'RADAR_REQUESTS_PER_MIN', 1000)
    multisite.calls.clear()
    hybrid.pin('site'); emitter.radar._acquire()
    assert not any(c[0]=='tile' and ('/7/' in c[2] or '/9/' in c[2]) for c in multisite.calls)
    assert len(emitter.radar._frames) == 8
    assert all(f['complete'] for f in emitter.radar._frames)
    assert protected <= emitter.radar._tiles.keys()
    assert len(emitter.radar._tiles) == 400


def test_sixty_spare_slots_admit_a_source_round_not_each_zoom(scene, hybrid):
    emitter, ctx = scene
    emitter.radar._prefetch(MRMS, ctx)
    wanted = {k for k in emitter.radar._tiles if k[0] == SITE and k[4] == 7}
    for key in list(emitter.radar._tiles):
        if key[0] == SITE: del emitter.radar._tiles[key]
    emitter.radar._prefetched = {k:v for k,v in emitter.radar._prefetched.items() if k[0] != SITE}
    emitter.radar._request_times = [0.] * (240-34-60)
    emitter.radar._prefetch(MRMS, ctx)
    assert wanted and all(Path(radar_engine._radar_tile_path(k[0],k[1],k[3],k[4],k[5],k[6])).exists() for k in wanted)
    assert len(emitter.radar._request_times) <= 240-34
