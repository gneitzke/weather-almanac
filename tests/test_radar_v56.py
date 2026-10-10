"""Smooth reflectivity, immutable variants and durable loopback preference."""
import io
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image
from lib import almanac_emit as ae, radar_palette as rp
from lib import radar_engine
from lib.radar_cache import TileInventory
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_freshness_health import _load_serve, _payload


def gates(source, values, size=None):
    image = Image.new('P', size or (len(values), 1))
    image.putpalette([v for color in rp._indexed_colors(source) for v in color], rawmode='RGBA')
    image.putdata(values)
    return image


@pytest.mark.parametrize('source', rp.INDEX_DBZ)
def test_two_gate_step_is_reflectivity_then_lut(source):
    offset = 66 if source.endswith('n0b') else 64
    image = gates(source, [offset+40, offset+120])  # 20 and 60 dBZ
    result = rp.smooth_remap(image, source, rp.source_palette(source))
    assert result.size == (2,1)
    expected = [dict(rp.source_palette(source))[dbz] for dbz in (25,55)]
    assert list(result.getdata()) == expected
    allowed = {color[:3] for _, color in rp.source_palette(source)}
    assert all(color[:3] in allowed for color in result.getdata())
    # RGB bilinear would create these colours; our result is categorically different.
    assert result.tobytes() != rp.remap(image, source, rp.source_palette(source)).resize((2,1), Image.Resampling.BILINEAR).tobytes()


@pytest.mark.parametrize('source', rp.INDEX_DBZ)
@pytest.mark.parametrize('missing', [0, 1, 70])  # reserved/no data and below either floor
def test_transparent_gap_never_blends_opposite_gates(source, missing):
    offset = 66 if source.endswith('n0b') else 64
    image = gates(source, [offset+40, missing, missing, offset+120])
    result = rp.smooth_remap(image, source, rp.source_palette(source))
    row = [result.getpixel((x,0)) for x in range(4)]
    low, high = (dict(rp.source_palette(source))[d] for d in (20,60))
    assert row[0][:3] == low[:3] and row[-1][:3] == high[:3]
    assert all(c[:3] == low[:3] for c in row[:2])
    assert all(c[:3] == high[:3] for c in row[-2:])
    assert [c[3] for c in row] == [223,32,32,223]


@pytest.mark.parametrize('source', rp._FILES)
def test_unknown_rgba_and_partial_coverage(source):
    color = next(c for c,d in rp._tables(source)[0].items() if d == 20)
    image = Image.new('RGBA',(3,1));image.putdata([color[:3]+(128,), (19,37,53,255), (0,0,0,0)])
    result = rp.smooth_remap(image,source,rp.source_palette(source))
    low = dict(rp.source_palette(source))[20][:3]
    assert all(c[:3] == low for c in result.getdata() if c[3])
    assert [result.getpixel((x,0))[3] for x in range(3)] == [112,16,0]
    assert result.info['unmatchedPixels'] == 1 and result.info['remapped'] is False


@pytest.mark.parametrize('source', rp.INDEX_DBZ)
def test_all_indices_and_clear_air_alpha(source):
    image = gates(source,list(range(256)))
    result = rp.smooth_remap(image,source,rp.source_palette(source))
    allowed = {c[:3] for _,c in rp.source_palette(source)}
    assert all(c[:3] in allowed for c in result.getdata() if c[3])
    if source.endswith('n0b'):
        # clear air (index 80, 7 dBZ) draws nothing under the 15 dBZ display floor, smoothed or not
        clear = rp.smooth_remap(gates(source,[80,80]),source,rp.source_palette(source))
        assert all(c[3] == 0 for c in clear.getdata())


@pytest.mark.parametrize('address', ['127.0.0.1','::1','::ffff:127.0.0.1','198.51.100.1'])
@pytest.mark.parametrize('query,value', [('radarSmooth=on','on'),('radarSmooth=off','off'),
    ('radarSmooth=1',None),('radarSmooth=ON',None),('radarSmooth=',None),
    ('radarSmooth=on&radarSmooth=off',None),('x=radarSmooth%3Don',None)])
def test_loopback_only_preference(monkeypatch,tmp_path,address,query,value):
    module = _load_serve(monkeypatch,tmp_path,_payload())
    monkeypatch.setattr(module.http.server.SimpleHTTPRequestHandler,'do_GET',lambda h:None)
    # Smooth rides on the camera owner's accepted transaction (a first claim here).
    camera='radarSession=preference-session-123&radarGeneration=1&radarHeartbeat=1&radarClaim=&radarClaimEpoch=0&radarCommit=1&radarPolicy=manual&view=radar&radarTheme=paper&radarGeoZoom=8&radarGeoCenter=47,-122&radarMoving=0&'
    h=object.__new__(module.Handler);h.client_address=(address,1);h.path='/wx.json?'+camera+query
    h.do_GET();module._flush_preferences(force=True);marker=tmp_path/'radar_smooth'
    expected=value if address in module.LOOPBACK else None
    assert (marker.read_text().strip() if marker.exists() else None)==expected


def test_durable_atomic_changed_only(monkeypatch,tmp_path):
    script=Path('design/almanac/kiosk/almanac-kiosk.sh').read_text()
    setup='RADAR_STATE='+script.split('RADAR_STATE=',1)[1].split('cp -f "$APP/design/almanac/console_live.html"',1)[0]
    runtime=tmp_path/'runtime';runtime.mkdir();state=tmp_path/'durable'
    env=dict(os.environ,XDG_STATE_HOME=str(state),DATA_DIR=str(runtime))
    (runtime/'radar_smooth').write_text('off')
    subprocess.run(['bash','-c',setup],env=env,check=True)
    module=_load_serve(monkeypatch,runtime,_payload())
    module._write_radar_preference('radar_smooth',['on']);module._flush_preferences()  # writer thread: wait for it
    marker=runtime/'radar_smooth';before=marker.stat()
    module._write_radar_preference('radar_smooth',['on']);module._flush_preferences()
    assert marker.stat().st_ino==before.st_ino
    assert marker.is_symlink()
    shutil.rmtree(runtime);runtime.mkdir()
    subprocess.run(['bash','-c',setup],env=env,check=True)
    assert marker.is_symlink() and marker.read_text()=='on\n'
    assert (state/'wfpiconsole/radar_smooth').read_text()=='on\n'
    assert not list(state.rglob('*.tmp.*'))


def test_engine_variants_reuse_native_bytes_and_restart(make_emitter,hybrid,tmp_path):
    image=gates('iem-mrms-lcref',[84,144]*32768,(256,256))
    out=io.BytesIO();image.save(out,'PNG');hybrid.tile=out.getvalue()
    emitter=make_emitter();emitter.radar._acquire()
    old=emitter.radar._result.tiles
    assert old['smooth'] is False and old['tileSize']==256
    (tmp_path/'radar_smooth').write_text('on')
    hybrid.calls.clear();emitter.radar._acquire()
    new=emitter.radar._result.tiles
    assert new['smooth'] is True and new['tileSize']==256
    assert new['revision'] != old['revision']
    assert new['geometry']==old['geometry'] and emitter._build_payload()['radar']['smooth'] is True
    assert not any('/tile.py/' in c[2] for c in hybrid.calls)
    assert any(len(k)==7 for k in emitter.radar._disk_inventory.records)
    assert any(len(k)==6 for k in emitter.radar._disk_inventory.records)
    for key,(path,_,_) in emitter.radar._disk_inventory.records.items():
        radar_engine._radar_tile_metadata(path,key[0])
    hybrid.calls.clear();restart=make_emitter();restart.radar._acquire()
    assert not any('/tile.py/' in c[2] for c in hybrid.calls)
    assert restart.radar._disk_inventory.startup['files']==len(emitter.radar._disk_inventory)
    assert any(len(k)==7 for k in restart.radar._disk_inventory.records)
    assert restart.radar._result.tiles['smooth'] is True
    assert restart.radar._disk_inventory.bytes==emitter.radar._disk_inventory.bytes
    (tmp_path/'radar_smooth').write_text('off');emitter.radar._acquire()
    assert emitter.radar._result.tiles['revision']==old['revision']
    assert TileInventory.MAX_BYTES==64_000_000 and TileInventory.MAX_FILES==8000


def test_smooth_stamp_supersedes_ordered_camera(make_emitter,hybrid,tmp_path):
    import json
    (tmp_path/'radar_intent').write_text(json.dumps(dict(seq=1,zoom=8,source='mosaic',center='station')))
    e=make_emitter();before=e.radar._preference_stamp()
    (tmp_path/'radar_smooth').write_text('on')
    assert before!=e.radar._preference_stamp()



def test_both_variants_share_one_eviction_budget(tmp_path):
    inventory=TileInventory();inventory.MAX_BYTES=10
    native=('iem-mrms-lcref',None,'202609150000',8,1,1)
    for key,name in [(native,'native.png'),(native+(True,),'smooth.png')]:
        path=tmp_path/name;path.write_bytes(b'123456')
        inventory.evict(incoming_size=6,incoming_files=1)
        inventory.add(key,path,6,{})
    assert not (tmp_path/'native.png').exists()
    assert (tmp_path/'smooth.png').exists() and inventory.bytes==6


@pytest.mark.parametrize('raw', [None,'off','ON','yes','on'+(' '*128),'on\njunk'])
def test_engine_invalid_or_absent_defaults_off(make_emitter,hybrid,tmp_path,raw):
    if raw is not None:(tmp_path/'radar_smooth').write_text(raw)
    e=make_emitter();e.radar._acquire()
    assert e.radar._result.tiles['smooth'] is False


def test_high_mrms_index_not_clipped_before_interpolation():
    # 95.5 and 10.5 dBZ average back to 84.875 and 21.125. Use a diagnostic
    # half-dBZ LUT so saturation in the ordinary legend cannot hide a lost index.
    palette=[(i/2-32,(i,0,0,255)) for i in range(256)]
    result=rp.smooth_remap(gates('iem-mrms-lcref',[255,85]),'iem-mrms-lcref',palette)
    assert [result.getpixel((x,0))[0] for x in range(2)]==[233,106]


@pytest.mark.parametrize('source', rp.INDEX_DBZ)
def test_bilinear_four_gate_field_and_single_gate_gap(source):
    offset=66 if source.endswith('n0b') else 64
    values=[offset+40,offset+120,offset+80,offset+140]
    palette=[(i/2-offset/2,(i,0,0,255)) for i in range(256)]
    result=rp.smooth_remap(gates(source,values,(2,2)),source,palette)
    # Independent bilinear oracle in native index space, including edge clamping.
    for y,ty in enumerate((.125,.875)):
        for x,tx in enumerate((.125,.875)):
            expected=int(values[0]*(1-tx)*(1-ty)+values[1]*tx*(1-ty)+values[2]*(1-tx)*ty+values[3]*tx*ty)
            assert result.getpixel((x,y))==(expected,0,0,255)
    gap=rp.smooth_remap(gates(source,[offset+40,0,offset+120]),source,rp.source_palette(source))
    colors=dict(rp.source_palette(source))
    # Box reduction can combine the covered halves of a missing output gate;
    # no missing sample supplies intensity. Its quarter coverage stays honest.
    assert [gap.getpixel((x,0)) for x in range(3)]==[
        colors[20][:3]+(223,),colors[40][:3]+(64,),colors[60][:3]+(223,)]
