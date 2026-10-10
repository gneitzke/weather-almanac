"""Inventory ownership and work proportional to changed tiles, offline only."""
import builtins
import io
import json
import os
from pathlib import Path
import threading

import pytest

from lib import almanac_emit as ae
from lib import radar_engine
from lib.radar_cache import TileInventory
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_freshness_health import serve_at  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401


def count_cache_io(monkeypatch, root):
    counts = dict(stat=0, open=0)
    for owner, name, kind in ((os,'stat','stat'), (builtins,'open','open'), (io,'open','open')):
        original = getattr(owner, name)
        def counted(path, *args, _original=original, _kind=kind, **kwargs):
            if isinstance(path,(str,bytes,os.PathLike)) and str(path).startswith(str(root)+'/'):
                counts[_kind] += 1
            return _original(path,*args,**kwargs)
        monkeypatch.setattr(owner,name,counted)
    return counts


@pytest.mark.parametrize('viewed',[False,True])
def test_warm_pass_does_no_cache_io(make_emitter,hybrid,multisite,tmp_path,monkeypatch,viewed):
    multisite.scans={site:[hybrid.latest-300*i for i in reversed(range(8))] for site in multisite.scans}
    multisite.colors['KFAR']=(12,145,16,255)
    if viewed: hybrid.view()
    e=make_emitter()
    for mode in ('mosaic','site'):
        hybrid.pin(mode)
        e.radar._request_times.clear();e.radar._acquire()
    assert e.radar._result.source_id=='iem-nexrad-n0b'
    # Make all optional rounds warm before measuring the unchanged pass.
    for _ in range(3):
        e.radar._request_times.clear();e.radar._acquire(intent_triggered=True)
    counts=count_cache_io(monkeypatch,tmp_path/'radar')
    e.radar._tiles.clear()  # warm DISK/index, not the native byte LRU
    for _ in range(3):
        e.radar._request_times.clear();e.radar._acquire(intent_triggered=True)
        if viewed: assert e.radar._inventory_valid(e.radar._result)
    assert counts==dict(stat=0,open=0)
    if viewed: assert all(f['complete'] for f in e.radar._frames[-8:])


def test_cold_pass_io_is_one_open_per_written_tile(make_emitter,hybrid,tmp_path,monkeypatch):
    e=make_emitter();e.radar._start_inventory();assert e.radar._cache_ready.wait(5)
    counts=count_cache_io(monkeypatch,tmp_path/'radar')
    e.radar._acquire()
    assert e.radar._available
    assert counts['stat']+counts['open'] <= len(e.radar._disk_inventory)+2
    assert counts['open']==len(e.radar._disk_inventory)


def test_startup_scans_once_off_worker_and_reuses_validated_metadata(make_emitter,hybrid,tmp_path,monkeypatch):
    e=make_emitter();e.radar._acquire()
    count=len(e.radar._disk_inventory)
    threads=[];validate=radar_engine._radar_tile_metadata
    monkeypatch.setattr(radar_engine,'_radar_tile_metadata',lambda *args:(threads.append(threading.current_thread().name),validate(*args))[1])
    restarted=make_emitter();restarted.radar._acquire()
    assert len(threads)==count and set(threads)=={'radar-inventory'}
    assert restarted.radar._disk_inventory.startup['files']==count
    before=len(hybrid.calls);restarted.radar._acquire(intent_triggered=True)
    assert len(threads)==count
    assert len(hybrid.calls)==before


def test_bad_tile_hint_validates_only_reported_path_and_repairs(make_emitter,hybrid,tmp_path,monkeypatch):
    e=make_emitter();e.radar._acquire()
    key=next(iter(e.radar._disk_inventory.records));path=e.radar._disk_inventory.records[key][0]
    path.write_bytes(b'broken')
    # External damage is reported, never discovered by rescanning the cache.
    assert key in e.radar._disk_inventory
    marker=tmp_path/'radar_bad_tiles'
    marker.write_text(json.dumps([path.relative_to(tmp_path).as_posix()]))
    seen=[];validate=radar_engine._radar_tile_metadata
    monkeypatch.setattr(radar_engine,'_radar_tile_metadata',lambda *args:(seen.append(args[0]),validate(*args))[1])
    e.radar._consume_bad_tiles()
    assert seen==[path] and key not in e.radar._disk_inventory
    assert e.radar._acquisition_pending
    e.radar._acquire(intent_triggered=True)
    assert key in e.radar._disk_inventory
    validate(path,key[0])
    e.radar._consume_bad_tiles();assert seen==[path]


def test_pressure_evicts_from_index_without_stat_or_open(tmp_path,monkeypatch):
    cache=TileInventory();cache.MAX_FILES=3
    for i in range(4):
        path=tmp_path/f'{i}.png';path.write_bytes(b'x')
        cache.add(('source',None,'stamp',8,0,i),path,1,{})
    counts=count_cache_io(monkeypatch,tmp_path)
    cache.evict({('source',None,'stamp',8,0,0)})
    assert counts==dict(stat=0,open=0)
    assert len(cache)==cache.bytes==3
    assert ('source',None,'stamp',8,0,0) in cache
    assert ('source',None,'stamp',8,0,1) not in cache


def test_boot_scanner_bounds_enumeration(tmp_path,monkeypatch):
    cache=TileInventory();cache.MAX_ENTRIES=40
    for i in range(100): (tmp_path/str(i)).mkdir()
    cache.scan(tmp_path,lambda *args:pytest.fail('no tiles here'))
    assert cache.startup['entries']<=41
    assert cache.startup['bounded']


def test_loopback_bad_tile_report_is_bounded_and_repairs(make_emitter,hybrid,tmp_path,monkeypatch,serve_at):
    import urllib.request
    import urllib.error
    e=make_emitter();e.radar._acquire()
    key=next(iter(e.radar._disk_inventory.records));path=e.radar._disk_inventory.records[key][0]
    path.unlink()
    module,url=serve_at({})
    relative=path.relative_to(tmp_path).as_posix().encode()
    def post(body):
        return urllib.request.urlopen(urllib.request.Request(url+'/radar-bad-tile',data=body,method='POST'))
    with post(relative) as response: assert response.status==204
    e.radar._consume_bad_tiles();assert key not in e.radar._disk_inventory
    with pytest.raises(urllib.error.HTTPError) as bad: post(b'../wx.json')
    assert bad.value.code==400
    # Only the panel reports bad tiles: a LAN controller (remote control) and a
    # public client are refused, whatever the path.
    for address in ('192.168.0.14', '198.51.100.7'):
        sent = []
        h = object.__new__(module.Handler); h.client_address = (address, 1); h.path = '/radar-bad-tile'
        h.send_error = lambda code, *a: sent.append(code)
        h.do_POST()
        assert sent == [403], address
