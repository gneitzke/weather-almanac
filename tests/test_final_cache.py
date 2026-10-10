"""Boot and write-pressure regressions; all paths are temporary and offline."""
from pathlib import Path

import pytest

from lib import almanac_emit as ae
from lib import radar_engine
from lib.radar_cache import TileInventory
from tests.test_radar_cache_fit import tile
from tests.test_radar_hybrid import hybrid  # noqa: F401


def assert_accounted(cache, root):
    files = {p for p in root.rglob('*') if p.is_file()}
    assert files == {r[0] for r in cache.records.values()}
    assert sum(p.stat().st_size for p in files) == cache.bytes


def test_partial_stamp_and_debris_cannot_escape_accounting(tmp_path):
    root = tmp_path/'rev'
    for y in range(20):
        p = tile(root, 'iem-mrms-lcref', '-', '202609161200', 8, 0, y)
    p.with_suffix('.png.tmp').write_bytes(b'abandoned write')
    cache = TileInventory()
    cache.scan(root, lambda *args: {}, entry_limit=10)
    assert cache.startup['bounded']
    assert_accounted(cache, root)
    cache.MAX_FILES = 0
    cache.evict()
    assert_accounted(cache, root)


def test_complete_scan_removes_unowned_files_and_symlinks(tmp_path):
    root = tmp_path/'rev'
    p = tile(root, 'iem-mrms-lcref', '-', '202609161200', 8, 0, 0)
    p.with_suffix('.png.tmp').write_bytes(b'abandoned write')
    outside = tmp_path/'outside'
    outside.write_bytes(b'must survive')
    (p.parent/'1.png').symlink_to(outside)
    cache = TileInventory()
    cache.scan(root, lambda *args: {})
    assert outside.read_bytes() == b'must survive'
    assert_accounted(cache, root)


def test_scan_does_not_follow_revision_symlink(tmp_path):
    outside = tmp_path/'outside'
    p = tile(outside, 'iem-mrms-lcref', '-', '202609161200', 8, 0, 0)
    root = tmp_path/'rev'
    root.symlink_to(outside, target_is_directory=True)
    cache = TileInventory()
    cache.MAX_FILES = 0
    cache.scan(root, lambda *args: {})
    assert p.exists(), 'boot eviction followed the revision symlink'
    assert not cache.records


@pytest.mark.parametrize('limited', [False, True])
def test_newest_wins_across_sites(tmp_path, limited):
    root = tmp_path/'rev'
    newest = tile(root, 'iem-nexrad-n0b', 'KAAA', '202609161200', 8, 0, 0)
    for i in range(8):
        tile(root, 'iem-nexrad-n0b', 'KZZZ', f'2026091600{i:02}', 8, 0, 0)
    cache = TileInventory()
    cache.MAX_FILES = 1
    cache.scan(root, lambda *args: {}, entry_limit=18 if limited else None)
    assert newest.exists()
    assert len(cache) == 1


def test_both_revisions_share_age_order_and_boot_budget(make_emitter, monkeypatch):
    e = make_emitter()
    root = Path(radar_engine.RADAR_DIR)/'t'
    new = tile(root/radar_engine._radar_render_revision(), 'iem-mrms-lcref', '-', '202609161200', 8, 0, 0)
    old = tile(root/radar_engine._radar_render_revision(True), 'iem-mrms-lcref', '-', '202609160000', 8, 0, 0)
    e.radar._disk_inventory.MAX_FILES = 1
    # Fixed 2026-09-16 stamps: ordering under the budget, not boot expiry.
    monkeypatch.setattr(radar_engine, 'RADAR_CACHE_RETENTION_SEC', 10**9)
    monkeypatch.setattr(e.radar, '_migrate_cache', lambda *args: None)
    monkeypatch.setattr(radar_engine, '_radar_tile_metadata', lambda *args: {})
    e.radar._start_inventory()
    assert e.radar._cache_ready.wait(5)
    assert new.exists() and not old.exists()
    assert e.radar._disk_inventory.startup['evicted'] == 1


def test_eviction_of_last_sibling_does_not_remove_write_destination(make_emitter, hybrid, monkeypatch):
    e = make_emitter()
    e.radar._start_inventory()
    assert e.radar._cache_ready.wait(5)
    cache = e.radar._disk_inventory
    cache.MAX_FILES = 1
    source, stamp = 'iem-mrms-lcref', hybrid.latest
    target = radar_engine._radar_tile_path(source, None, stamp, 8, 0, 0)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b'old')
    cache.add(radar_engine._radar_disk_key(source, None, stamp, 8, 0, 0), target, 3, {})
    monkeypatch.setattr(e.radar, '_request', lambda *args, **kwargs: hybrid.tile)
    monkeypatch.setattr(e.radar, '_checkpoint', lambda *args: None)
    ctx = dict(zoom=8, tiles=[(0, 1, 0, 0)], tile_workers=1)
    results = list(e.radar._tile_batch(source, stamp, ctx, 100, lambda *args: 'unused', None))
    assert len(results) == 1, ctx.get('last_error')
    assert radar_engine._radar_disk_key(source, None, stamp, 8, 0, 1) in cache


def test_migration_does_not_traverse_tile_tree_symlink(make_emitter):
    e = make_emitter()
    root = Path(radar_engine.RADAR_DIR)
    outside = root.parent/'external-tiles'
    p = tile(outside/'obsolete', 'iem-mrms-lcref', '-', '202609161200', 8, 0, 0)
    root.mkdir()
    (root/'t').symlink_to(outside, target_is_directory=True)
    e.radar._migrate_cache()
    assert p.exists(), 'migration followed t/ symlink and deleted external data'
    assert not (root/'t').is_symlink()


def test_incoming_source_write_pins_previous_displayed_tiles(make_emitter, hybrid, monkeypatch):
    e = make_emitter()
    e.radar._start_inventory()
    assert e.radar._cache_ready.wait(5)
    cache = e.radar._disk_inventory
    cache.MAX_FILES = 2
    source = 'iem-mrms-lcref'
    keys = []
    for stamp in (hybrid.latest-240, hybrid.latest-120):
        p = radar_engine._radar_tile_path(source, None, stamp, 8, 0, 0)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b'old')
        key = radar_engine._radar_disk_key(source, None, stamp, 8, 0, 0)
        keys.append(key)
        cache.add(key, p, 3, {})
    previous = radar_engine._RADAR_NONE._replace(source_id=source, zoom=8,
        frames=({'ts':hybrid.latest-240},), tiles=dict(grid=dict(x0=0,y0=0,w=1,h=1)))
    monkeypatch.setattr(e.radar, '_request', lambda *args, **kwargs: hybrid.tile)
    monkeypatch.setattr(e.radar, '_checkpoint', lambda *args: None)
    ctx = dict(zoom=8, tiles=[(0, 0, 0, 0)], tile_workers=1, previous_result=previous)
    assert list(e.radar._tile_batch(source, hybrid.latest, ctx, 100, lambda *args:'unused', None))
    assert keys[0] in cache, 'write evicted the retained displayed measurement'
    assert keys[1] not in cache
