"""Restored closest-site evidence and cross-mode warming; simulated transport."""
import pytest

from lib import almanac_emit as ae, radar_native_budget as budget
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401
from tests.test_radar_level3 import native  # noqa: F401
from tests.test_radar_site_cache import scene, MRMS, SITE  # noqa: F401
from tests.test_radar_auto import intent
from tests.test_radar_auto_review2 import fail_listings


@pytest.mark.parametrize('source,zoom,opposite', [
    (MRMS, 6, False), (MRMS, 7, True), (MRMS, 8, True),
    (SITE, 7, True), (SITE, 8, False), (SITE, 9, False),
])
def test_auto_warms_next_source_at_settled_zoom(scene, monkeypatch, source, zoom, opposite):
    emitter, ctx = scene
    warmed = []
    def tiles(target, stamp, warm, *args):
        warmed.append((target, warm['zoom'], warm['center']))
        return iter(())
    monkeypatch.setattr(emitter, '_radar_tile_batch', tiles)
    monkeypatch.setattr(emitter, '_radar_headroom_delay', lambda *args: 0)
    emitter._radar_prefetch(source, dict(ctx, source_pref='auto', zoom=zoom, camera_zoom=zoom))
    other = SITE if source == MRMS else MRMS
    actual = {z for target, z, center in warmed if target == other}
    floor, ceiling = (7, 10) if other == SITE else (ae.RADAR_MIN_ZOOM, 9)
    expected = {z for z in (zoom-1, zoom, zoom+1) if floor <= z <= ceiling} if opposite else set()
    assert actual == expected
    assert all(center == ctx['center'] for _, _, center in warmed)


@pytest.mark.parametrize('attention,tier,ceiling,variant', [
    ('active', 'live', 0, 'native'),
    ('active', 'live', budget.NATIVE_NEWEST_ONLY_BYTES+1, 'native'),
    ('active', 'live', budget.NATIVE_PAUSE_BYTES+1, False),
    ('shadow', 'watch', 0, 'native'),
    ('shadow', 'rest', 0, 'native'),
    ('shadow', 'dormant', 0, 'native'),
])
def test_region_warming_uses_native_until_paused(
        make_emitter, hybrid, multisite, native, tmp_path, monkeypatch,
        attention, tier, ceiling, variant):
    (tmp_path/'radar_source').write_text('mosaic')
    (tmp_path/'radar_viewed').unlink()
    (tmp_path/'radar_viewing').unlink()
    monkeypatch.setattr(ae, 'RADAR_ATTENTION_MODE', attention)
    emitter = make_emitter()
    emitter._radar_attention.forced = emitter._radar_attention.tier = tier
    emitter._radar_native_budget.add(ceiling)
    emitter._do_radar()
    source, ctx = emitter._radar_idle_context
    assert source == MRMS and not native.calls
    hybrid.view()
    monkeypatch.setattr(emitter, '_radar_headroom_delay', lambda *args: 0)
    emitter._radar_prefetch(source, dict(ctx, viewed=True, refresh=dict(state='idle')))
    keys = [k for k in emitter._radar_disk_inventory.records if k[0] == SITE]
    assert keys and all((k[-1] == 'native' if len(k) == 7 else False) is
                        (variant == 'native') for k in keys)
    assert bool(native.calls) is (variant == 'native')
    assert bool([c for c in multisite.calls if c[0] == 'tile']) is (variant is False)
    assert emitter._radar_result.source_mode == 'mosaic'


@pytest.mark.parametrize('mode', ['mosaic', 'auto'])
def test_region_failed_listing_replaces_old_evidence_on_original_cadence(
        make_emitter, hybrid, multisite, tmp_path, monkeypatch, mode):
    intent(tmp_path, 6, mode)
    emitter = make_emitter(); emitter._do_radar()
    # A Level III cooldown cannot suppress the independent IEM evidence check.
    emitter._radar_cooldowns[ae.RADAR_LEVEL3_TRANSPORT] = hybrid.mono+1000
    emitter._do_radar(discovery=True, intent_triggered=False)
    before = emitter._build_payload()['radar']['nexrad']
    assert before['reporting'] is True and before['newestTs'] is not None
    hybrid.mono += 20
    # Even Auto's successful acquisition cache must not suppress this refresh.
    multisite.calls.clear()
    emitter._do_radar(discovery=True, intent_triggered=False)
    assert multisite.calls == [('list', 'KNEA')]
    assert emitter._build_payload()['radar']['nexrad']['checkedTs'] == before['checkedTs']+20
    fail_listings(emitter, monkeypatch)
    for elapsed, reporting in [(40, None), (340, False)]:
        hybrid.mono = elapsed
        emitter._do_radar(discovery=True, intent_triggered=False)
        evidence = emitter._build_payload()['radar']['nexrad']
        assert evidence['reporting'] is reporting
        assert evidence['newestTs'] is None
        assert evidence['reason'] == 'scan unavailable'
        assert evidence['checkedTs'] == hybrid.now+elapsed
