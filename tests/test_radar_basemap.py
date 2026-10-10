"""Hermetic basemap registration, clipping, geography, lifecycle and failure tests."""
import hashlib
import math
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET

import pytest

from lib import almanac_emit as ae
from lib import radar_engine
from lib import radar_basemap as bm
from lib.radar_geometry import world_point
from tests.test_radar_hybrid import hybrid  # noqa: F401


def context(lat=47.61, lon=-122.33, zoom=7, viewed=True):
    tiles, _, bounds, _ = radar_engine._radar_viewport(lat,lon,zoom,480)
    identity=hashlib.sha256(repr((lat,lon,zoom,480,tiles)).encode()).hexdigest()[:20]
    return dict(center=dict(lat=lat,lon=lon),zoom=zoom,bounds=bounds,tiles=tiles,identity=identity,viewed=viewed)


@pytest.mark.parametrize('lat,lon',[(47.61,-122.33),(52.52,13.4),(-33.87,151.21),(-16.8,179.7),(-16.8,-179.7)])
@pytest.mark.parametrize('zoom',[4,7,9])
def test_real_vertices_match_world_tile_registration(lat,lon,zoom):
    ctx=context(lat,lon,zoom)
    cx,cy=world_point(lat,lon,zoom); left,top=cx-240,cy-240
    path=str(bm.DATA_PATH); q,_,_=bm._index(path)
    world=256*2**zoom; tested=0
    # A 6° neighbourhood includes real coastline/admin vertices even at z9.
    for y in range(math.floor(lat)-3,math.floor(lat)+4):
        for x in range(math.floor(lon)-3,math.floor(lon)+4):
            for layer,points in bm._cell(path,(x+180)%360-180,y):
                if layer not in (2,3,4): continue
                for dx,dy in points:
                    vlat,vlon=y+dy/q,x+dx/q
                    wx=(vlon+180)/360*world
                    wy=(1-math.asinh(math.tan(math.radians(vlat)))/math.pi)/2*world
                    tx,ty=math.floor(wx/256),math.floor(wy/256)
                    # Independently recover the same world coordinate from its XYZ tile.
                    expected=(wx-left,wy-top)
                    projected=world_point(vlat,vlon,zoom)
                    actual=(projected[0]-left,projected[1]-top)
                    assert actual==pytest.approx(expected,abs=1e-8)
                    if 0<=actual[0]<480 and 0<=actual[1]<480:
                        tile=next(t for t in ctx['tiles'] if t[:2]==(tx%(2**zoom),ty))
                        assert abs(actual[0]-(tile[2]+wx-tx*256))<1
                        assert abs(actual[1]-(tile[3]+wy-ty*256))<1
                    tested+=1
    assert tested>20


def test_clip_edges_and_reentry():
    polygon=bm.clip_polygon([(-10,-10),(490,-10),(490,490),(-10,490)],480)
    assert set(polygon)=={(0,0),(480,0),(480,480),(0,480)}
    assert bm.clip_polygon([(-10,0),(-5,0),(-5,10)],480)==[]
    assert bm.clip_segment((-10,240),(490,240),480)==((0,240),(480,240))
    assert bm.clip_segment((-1,0),(-1,480),480) is None
    assert bm.clip_segment((0,-10),(0,490),480)==((0,0),(0,480))
    lines=bm.clip_line([(10,10),(490,10),(490,30),(10,30)],480)
    assert lines==[[(10,10),(480,10)],[(480,30),(10,30)]]
    assert bm.simplify([(0,0),(1,.1),(2,0)])==[(0,0),(2,0)]



import io
import time
from PIL import Image

GOLDENS = {
 ('paper',4,2,5):'59f51f0d1b7f20f5a5a615b00589fe417cd0a1474d42784a050e78be974105eb',
 ('paper',8,40,89):'68faa985f01514392f7d680ee0d87ec06145be7128a43d1cc871f578fe2deb35',
 ('paper',10,165,355):'94c7bdaa459c8309c8446e54eb33e87f3db83195e2fc234128c9b9f17f9e12ad',
 ('night',4,2,5):'47b0729709b9e6adda5063591b234ca8824de043d166f9c6c22fc51c5b5d729e',
 ('night',8,40,89):'bdae016dc36229102eb3dcb9716a32cea03d5a0e1684f1dfc1f14861366b261c',
 ('night',10,165,355):'c932053d2dac0f2a45c34208663f434ec3329a3077eefb08cc88de551ee894ac',
}

@pytest.mark.parametrize('key',GOLDENS)
def test_deterministic_opaque_fixed_palette(key):
    raw=bm.tile(*key);assert raw==bm.tile(*key)
    assert hashlib.sha256(raw).hexdigest()==GOLDENS[key]
    image=Image.open(io.BytesIO(raw))
    assert image.mode=='P' and image.size==(256,256) and len(image.getpalette())==162*3
    assert 'transparency' not in image.info and b'tRNS' not in raw
    assert set(image.convert('RGB').getdata())<=set(bm.palette(key[0]))
    assert len(raw)<=16384

@pytest.mark.parametrize('theme',['paper','night'])
def test_exact_style_and_antialias(theme):
    im=Image.open(io.BytesIO(bm.tile(theme,8,40,89)))
    pixels=list(im.getdata());rgb=set(im.convert('RGB').getdata())
    for color in ('EBE6DB','DFDCD4','95A3AA') if theme=='paper' else ('0B0D11','151B21','445A68'):
        assert tuple(bytes.fromhex(color)) in rgb
    intermediate=[p for p in pixels if p>=2 and (p-2)%16!=15]
    assert len(set(intermediate))>=6 and len(intermediate)/sum(p>=2 for p in pixels)>=.08
    other=Image.open(io.BytesIO(bm.tile('night' if theme=='paper' else 'paper',8,40,89))).convert('RGB')
    assert sum(a!=b for a,b in zip(im.convert('RGB').getdata(),other.getdata()))>65536*.3


def test_evenodd_island_hole_and_stitch():
    rings=[[(0,0),(256,0),(256,256),(0,256)],[(50,50),(200,50),(200,200),(50,200)]]
    m=bm.evenodd_mask(rings);assert m.getpixel((20,20))==1 and m.getpixel((100,100))==0
    assert len(bm.stitch([[(0,0),(1,0)],[(1,0),(2,0)]]))==1
    assert len(list(bm.ocean_runs([(x,0) for x in range(5)])))==1

@pytest.mark.parametrize('z',range(4,11))
@pytest.mark.parametrize('theme',['paper','night'])
def test_membership_pipeline(z,theme,monkeypatch):
    monkeypatch.setattr(bm,'_index',lambda path:(1000,bytes(360*180*8),0))
    # Separate horizontal lines across the viewport avoid painter overlap.
    cx,cy=world_point(47.6,-122.3,z);x,y=int(cx//256),int(cy//256)
    def source(path,sx,sy):
        out=[]
        for layer in range(2,7):
            lat,lon=bm.world_inverse(x*256+20,y*256+30*(layer-1),z)
            lat2,lon2=bm.world_inverse(x*256+230,y*256+30*(layer-1),z)
            if sx==math.floor(lon) and sy==math.floor(lat):
                out.append((layer,[((lon-sx)*1000,(lat-sy)*1000),((lon2-sx)*1000,(lat2-sy)*1000)]))
        return out
    monkeypatch.setattr(bm,'_cell',source)
    indices=set(Image.open(io.BytesIO(bm.tile(theme,z,x,y))).getdata())
    layers={(i-2)//32+2 for i in indices if i>=2}
    expected={2,3}|({4} if z>=5 else set())|({5} if z>=6 else set())|({6} if z>=8 else set())
    assert layers==expected


def test_cache_atomic_and_home_order(tmp_path,monkeypatch):
    # Real renderer once, then a deterministic cheap tile isolates scheduling.
    raw=bm.tile('paper',8,40,89);monkeypatch.setattr(bm,'tile',lambda *a:raw)
    requests=list(bm.home_requests((47.61,-122.33),8,'night'));assert len(requests)==490
    assert all(r[0]=='night' and r[1]==8 for r in requests[:35])
    assert max(i for i,r in enumerate(requests[:245]) if r[1]==4)<min(i for i,r in enumerate(requests[:245]) if r[1]==10)
    assert bm.warm(tmp_path,(47.61,-122.33),dict(lat=47.61,lon=-122.33),8,theme='night',limit=490)==490
    paths=[bm.tile_path(tmp_path,*r) for r in requests];assert all(p.exists() for p in paths)
    assert [p.stat().st_mtime_ns for p in paths]==sorted(p.stat().st_mtime_ns for p in paths)
    before=paths[0].stat().st_mtime_ns;bm.cache_tile(tmp_path,*requests[0]);assert paths[0].stat().st_mtime_ns==before
    monkeypatch.setattr(bm.os,'replace',lambda *a:(_ for _ in ()).throw(OSError('failed')))
    with pytest.raises(OSError):bm.cache_tile(tmp_path,'paper',8,0,0)
    assert not list(tmp_path.rglob('.tile-*'))


def test_independent_disk_cap_pins_and_empty_directories(tmp_path):
    pinned=bm.tile_path(tmp_path,'paper',8,40,89);pinned.parent.mkdir(parents=True);pinned.write_bytes(b'pinned');os.utime(pinned,(1,1))
    oldest=None
    for i in range(6100):
        p=tmp_path/'geo'/bm.version()/'night'/'10'/str(i//1024)/f'{i%1024}.png';p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'x'*5500);os.utime(p,(i+2,i+2))
        if i==0:oldest=p
    radar=tmp_path/'t'/'unrelated';radar.mkdir(parents=True);(radar/'full.png').write_bytes(b'x')
    assert bm.prune(tmp_path,{pinned})
    files=list((tmp_path/'geo').rglob('*.png'))
    assert len(files)<=6000 and sum(p.stat().st_size for p in files)<=32_000_000
    assert pinned.exists() and not oldest.exists() and (radar/'full.png').exists()


def test_raster_http_revision_and_404(tmp_path,monkeypatch):
    import threading,urllib.request,urllib.error
    from tests.test_freshness_health import _load_serve
    module=_load_serve(monkeypatch,tmp_path,{});monkeypatch.setattr(module,'WEB',str(tmp_path))
    bm.cache_tile(tmp_path/'radar','paper',8,40,89)
    (tmp_path/'radar'/'.geo-revision').write_text(bm.version())
    server=module.http.server.ThreadingHTTPServer(('127.0.0.1',0),module.Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}/radar/geo/'
    try:
        for suffix in [bm.version()+'/paper/8/0/0.png','0'*12+'/paper/8/40/89.png',bm.version()+'/wrong/8/40/89.png']:
            with pytest.raises(urllib.error.HTTPError) as err:urllib.request.urlopen(base+suffix)
            assert err.value.code==404 and 'Retry-After' not in err.value.headers
        with urllib.request.urlopen(base+bm.version()+'/paper/8/40/89.png') as response:
            assert response.headers['Cache-Control']=='public, max-age=31536000, immutable'
            assert Image.open(io.BytesIO(response.read())).size==(256,256)
    finally:server.shutdown();server.server_close();thread.join()
