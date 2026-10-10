"""Review regressions: revision identity, wrapped inventory and cold recovery."""
from pathlib import Path
import json
import pytest
from lib import almanac_emit as ae
from lib import radar_engine
from lib import radar_basemap as bm
from tests.test_radar_hybrid import hybrid


def ctx(lat=47.61,lon=-122.33,z=7):
    return dict(center=dict(lat=lat,lon=lon),zoom=z,inventory=set())


def test_render_revision_changes_urls(monkeypatch,tmp_path):
    monkeypatch.setattr(radar_engine,'RADAR_DIR',str(tmp_path))
    before=radar_engine._radar_tile_path('iem-mrms-lcref',None,1800000000,8,40,89)
    monkeypatch.setattr(radar_engine,'REMAP_REVISION',radar_engine.REMAP_REVISION+'-new')
    after=radar_engine._radar_tile_path('iem-mrms-lcref',None,1800000000,8,40,89)
    assert before!=after
    assert before.parts[-7]!=after.parts[-7]


@pytest.mark.parametrize('lon',[179.7,-179.7])
def test_dateline_manifest_is_unwrapped_and_row_major(tmp_path,monkeypatch,lon):
    monkeypatch.setattr(radar_engine,'RADAR_DIR',str(tmp_path));c=ctx(-16.8,lon);f=radar_engine._radar_frame('rainviewer',1800000000,c)
    tiles=radar_engine._radar_grid(c)
    for x,y,_,_ in tiles:
        p=radar_engine._radar_tile_path('rainviewer',None,f['ts'],7,x,y);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'tile')
        c['inventory'].add(('rainviewer',None,radar_engine._radar_stamp_text(f['ts']),7,x % 2**7,y))
    m=radar_engine._radar_tile_manifest('rainviewer',[f],c);g=m['grid']
    assert g['w']<=5 and g['h']<=3 and g['w']*g['h']==len(tiles)
    assert int(m['newest']['mask'],16)==(1<<len(tiles))-1
    assert m['frames'][0]['levels']['7']


def test_coverage_and_missing_acquisition_are_distinct(tmp_path,monkeypatch):
    monkeypatch.setattr(radar_engine,'RADAR_DIR',str(tmp_path));c=ctx();c.update(tiles=radar_engine._radar_grid(c))
    sites=['KATX','KLGX','KRTX','KPDT'];stamp=1800000000
    f=radar_engine._radar_frame('iem-nexrad-n0b',stamp,c,[(s,stamp) for s in sites])
    paths=[]
    for site in sites:
        for x,y,_,_ in radar_engine._radar_site_tiles(c,site):
            p=radar_engine._radar_tile_path('iem-nexrad-n0b',site,stamp,7,x,y);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'tile');paths.append(p)
            c['inventory'].add(('iem-nexrad-n0b',site,radar_engine._radar_stamp_text(stamp),7,x,y))
    m=radar_engine._radar_tile_manifest('iem-nexrad-n0b',[f],c)
    assert m['newest']['expectedMask']==m['newest']['completeMask']
    assert m['frames'][0]['levels']['7']
    assert int(m['newest']['expectedMask'],16)!=(1<<(m['grid']['w']*m['grid']['h']))-1
    victim=paths[len(paths)//2];victim.unlink()
    c['inventory'].remove(('iem-nexrad-n0b',victim.parts[-5],radar_engine._radar_stamp_text(stamp),7,int(victim.parts[-2]),int(victim.stem)))
    partial=radar_engine._radar_tile_manifest('iem-nexrad-n0b',[f],c)
    assert partial['newest']['completeMask']!=partial['newest']['expectedMask']
    assert not partial['frames'][0]['levels']['7']


def test_cold_outage_retains_local_map(hybrid,make_emitter):
    e=make_emitter()
    def fail(*args,**kw):raise ValueError('provider offline')
    e.radar._iem_frames=fail;e.radar._rainviewer_frames=fail
    e.radar._acquire()
    r=e._build_payload()['radar']
    assert r['available'] and r['center'] and r['geo']['version']==bm.version()
    assert not r['observedTs'] and r['refresh']['state']=='failed'


def test_first_view_warms_missing_history_and_checks_disk(hybrid,tmp_path,make_emitter):
    e=make_emitter();e.radar._acquire()
    assert sum(f['complete'] for f in e.radar._frames)==1
    (tmp_path/'radar_viewed').write_text(str(ae.time.time()))
    e.radar._acquire(view_started=True)
    assert sum(f['complete'] for f in e.radar._frames)>=8
    assert e.radar._inventory_valid(e.radar._result)
    f=e.radar._result.frames[-1];g=e.radar._result.tiles['grid']
    p=radar_engine._radar_tile_path(e.radar._result.source_id,None,f['ts'],e.radar._result.zoom,g['x0'],g['y0']);p.unlink()
    Path(e.output_path).with_name('radar_bad_tiles').write_text(json.dumps([p.relative_to(Path(e.output_path).parent).as_posix()]))
    e.radar._consume_bad_tiles()
    assert not e.radar._inventory_valid(e.radar._result)


def test_site_table_revision_is_independent(make_emitter,monkeypatch):
    e=make_emitter();e.radar._migrate_cache();old=radar_engine._radar_sites_revision();tile_revision=radar_engine._radar_render_revision()
    monkeypatch.setattr(radar_engine,'_NEXRAD_SITES',dict(radar_engine._NEXRAD_SITES,KNEW=(1.,2.,'New test site')))
    e.radar._migrate_cache();new=radar_engine._radar_sites_revision();root=Path(radar_engine.RADAR_DIR)
    assert old!=new and radar_engine._radar_render_revision()==tile_revision
    assert (root/'.sites-revision').read_text()==new
    assert (root/('sites-'+new+'.json')).is_file() and not (root/('sites-'+old+'.json')).exists()
