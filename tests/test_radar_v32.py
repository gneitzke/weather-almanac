"""Fable v3.2 K1–K6: source floor, computed contrast and alpha fidelity."""
import hashlib
import os

import pytest
from PIL import Image

from lib import almanac_emit as ae, radar_palette as rp
from lib import radar_engine
from tests.test_radar_hybrid import hybrid  # noqa: F401


# A translucent first stop exercises the remapper's coverage-alpha rule (the
# grey 5-10 dBZ clear-air band that used to supply one was removed with the v3 ramp).
TRANSLUCENT_PALETTE = ((5.0, (0x7F, 0x82, 0x95, 180)),) + rp._RADAR_LUT


def indexed(source):
    tile = Image.new('P', (256, 1)); tile.putdata(range(256))
    tile.putpalette([v for c in rp._indexed_colors(source) for v in c], rawmode='RGBA')
    return tile


def test_k1_source_legends():
    # One drawn scale for every source since 2026-09-24: 15-75 dBZ, no clear-air band.
    for source, settings in radar_engine._RADAR_SOURCES.items():
        legend = settings['legend']
        assert legend['floorDbz'] == rp.DISPLAY_FLOOR_DBZ == 15
        assert legend['bands'][0] == dict(lo=15, hi=20, start='#66A678', end='#43A05D')   # the ramp's own colour at 15
        assert legend['bands'][1:] == rp._RADAR_RAMP['bands'][1:]
        assert all('kind' not in b and 'alpha' not in b for b in legend['bands'])
    assert len(rp._RADAR_LUT) == 26                                   # the designed ramp is unchanged
    assert [d for d, c in rp._RADAR_DISPLAY_LUT if not c[3]] == [10.0, 12.5]
    assert all(rp.source_palette(s) is rp._RADAR_DISPLAY_LUT for s in rp._FILES)


def luminance(rgb):
    return sum(w*(v/255/12.92 if v/255 <= .04045 else ((v/255+.055)/1.055)**2.4)
               for w, v in zip((.2126, .7152, .0722), rgb))


def contrast(a, b):
    low, high = sorted((luminance(a), luminance(b)))
    return (high+.05)/(low+.05)


def test_k2_computed_contrast():
    for hexground, floor in [('EBE6DB', 2), ('F2EDE2', 2), ('0B0D11', 3)]:
        ground = bytes.fromhex(hexground)
        for _, color in rp._RADAR_LUT:
            assert contrast(color[:3], ground) >= floor


def test_k3_k4_index_floor_and_coverage():
    tile = indexed('iem-nexrad-n0b')
    site = rp.remap(tile, 'iem-nexrad-n0b', rp.source_palette('iem-nexrad-n0b'))
    # N0B index 96 is 15 dBZ: everything below it, clear air included, is transparent.
    assert all(site.getpixel((i, 0))[3] == 0 for i in range(0, 96))
    assert site.getpixel((96, 0)) == (102,166,120,255)
    mrms = rp.remap(indexed('iem-mrms-lcref'), 'iem-mrms-lcref', rp.source_palette('iem-mrms-lcref'))
    assert all(mrms.getpixel((i, 0))[3] == 0 for i in range(0, 94))          # MRMS index 94 is 15 dBZ
    assert mrms.getpixel((94, 0)) == (102,166,120,255)
    # RGBA coverage is independent of intensity, including fractional edge alpha.
    color = rp._indexed_colors('iem-nexrad-n0b')[76]
    for coverage in (1, 73, 128, 180, 254, 255):
        rgba = Image.new('RGBA', (1, 1), color[:3]+(coverage,))
        assert rp.remap(rgba, 'iem-nexrad-n0b', TRANSLUCENT_PALETTE).getpixel((0, 0)) == (127,130,149,round(coverage*180/255))


@pytest.mark.parametrize('source', rp._FILES)
@pytest.mark.parametrize('mode', ['P', 'RGBA'])
def test_k5_opaque_rain_byte_identity(source, mode):
    """Independent pre-v3.2 selection: targets replace RGB and retain coverage."""
    if mode == 'P' and source in rp.INDEX_DBZ:
        tile = indexed(source)
    else:
        colors = list(rp._tables(source)[0])
        colors += [c[:3]+(73,) for c in colors[:30]]
        colors += [(0,0,0,0), (19,37,53,255)]
        tile = Image.new('RGBA', (len(colors), 1)); tile.putdata(colors)
        if mode == 'P':
            tile = tile.convert('P', palette=Image.Palette.ADAPTIVE, colors=256)
    # SHA256 of raw RGBA bytes from d5ea7bf's remapper on these exact tiles.
    gold = {
        ('P', 'iem-mrms-lcref'): '79591d3d35100d1b1897c0abb4af27dbe262d90e5f89c01828d79b709ff8f0a3',
        ('P', 'iem-nexrad-n0b'): '74d77ef15de6f526dfe599fb4044b37b533263f01eb50ce50e253ca5179e2082',
        ('P', 'rainviewer'): 'c02e74c611194097262cffc054d4d613e19a93ef852f1907cde6d8d5eb12044e',
        ('RGBA', 'iem-mrms-lcref'): 'dd4b16c07654641619e10a2a41606665db295cc6ce34131cf363d597c4b89d60',
        ('RGBA', 'iem-nexrad-n0b'): '71d9f703997fe5dd01e2759afa54cb7dc13f61d7fe37f0758ec91daae8880287',
        ('RGBA', 'rainviewer'): 'cd9770a6941b20e0c954b6b90b97ff504f4cdb13c24dfa81c782844c3bd3aef9',
    }
    legacy = list(rp._RADAR_LUT)
    bands = [(10,20,'8AA3C6','4E79B4'),(20,25,'2E93A8','227F92'),(25,35,'3FA65E','2A8448')]
    for i in range(10):
        dbz = legacy[i][0]
        lo,hi,a,b = next(b for b in bands if b[0] <= dbz < b[1])
        t = (dbz-lo)/(hi-lo)
        legacy[i] = (dbz, tuple(round(a+(b-a)*t) for a,b in zip(bytes.fromhex(a),bytes.fromhex(b)))+(255,))
    assert hashlib.sha256(rp.remap(tile, source, legacy).tobytes()).hexdigest() == gold[mode, source]



def test_translucent_rgb_fallback_rounds_coverage(monkeypatch):
    # Force lossless RGB fallback, including the same native RGB at many alphas.
    native = rp._indexed_colors('iem-nexrad-n0b')[76][:3]
    colors = [native+(a,) for a in range(1, 256)]
    tile = Image.new('RGBA', (len(colors), 1)); tile.putdata(colors)
    convert = Image.Image.convert
    def lose_alpha(self, mode=None, *args, **kwargs):
        if self.mode == 'RGBA' and mode == 'P':
            return convert(convert(self, 'RGB'), mode, *args, **kwargs)
        return convert(self, mode, *args, **kwargs)
    monkeypatch.setattr(Image.Image, 'convert', lose_alpha)
    result = rp.remap(tile, 'iem-nexrad-n0b', TRANSLUCENT_PALETTE)
    assert list(result.getdata()) == [(127,130,149,round(a*180/255)) for a in range(1, 256)]


def test_k6_wide_zoom_draws_region_without_clear_air(make_emitter, hybrid, tmp_path):
    hybrid.pin(None)  # Auto: zoom 5 is wider than any one radar reaches
    (tmp_path/'radar_zoom').write_text('5')
    emitter = make_emitter(); emitter.radar._acquire()
    r = emitter._build_payload()['radar']
    assert r['sourceMode'] == 'mosaic'
    assert r['legend']['floorDbz'] == 15
    assert all(b.get('kind') != 'clear-air' for b in r['legend']['bands'])




@pytest.mark.skipif(os.environ.get('RADAR_NET_TEST')!='1',reason='opt in with RADAR_NET_TEST=1')
@pytest.mark.parametrize('place,lat,lon,mode',[('Seattle',47.61,-122.33,'mosaic'),('Aberdeen',46.975,-123.815,'site')])
def test_live_tile_set_matches_provider_bytes(make_emitter,tmp_path,monkeypatch,place,lat,lon,mode):
    """Real source PNGs remap byte-for-byte to independently stored XYZ tiles."""
    import io,json,time
    from pathlib import Path
    from tests.fixtures.config import make_config
    config=make_config();config['Station']['Latitude']=str(lat);config['Station']['Longitude']=str(lon)
    monkeypatch.setattr(radar_engine.RadarEngine,'_auto_source',lambda self,ctx,site_ok:mode)
    emitter=make_emitter(config=config)
    started=time.perf_counter();emitter.radar._acquire()
    expected_source='iem-nexrad-n0b' if mode=='site' else 'iem-mrms-lcref'
    assert emitter.radar._result.source_id==expected_source and emitter.radar._result.ts_frame,emitter._build_payload()['radar']
    count=visible=0;sites=set();max_error=0
    try:
        for key,native in emitter.radar._tiles.items():
            source,site,_,stamp,z,x,y=key
            if source!=expected_source:continue
            path=radar_engine._radar_tile_path(source,site,stamp,z,x,y)
            if not path.exists():continue
            with Image.open(io.BytesIO(native)) as im:
                with rp.remap(im,source,rp.source_palette(source)) as mapped:
                    with Image.open(path) as cached:
                        assert cached.size==(256,256)
                        assert cached.convert('RGBA').tobytes()==mapped.tobytes(),str(path)
                        meta=json.loads(cached.info['radarRemap'])
                        assert set(meta)=={'remapped','unmatchedColors','opaqueColors','unmatchedPixels','opaquePixels','ambiguousPixels','revision'}
                        assert meta['revision']==radar_engine.REMAP_REVISION
                        visible+=sum(p[3]>0 for p in cached.convert('RGBA').getdata())
                        max_error=max(max_error,meta['unmatchedPixels']/max(1,meta['opaquePixels']))
            count+=1;sites.add(site or '-')
        assert count>=4,count
        print('LIVE TILE SET',json.dumps(dict(place=place,source=expected_source,tiles=count,sites=sorted(sites),visiblePixels=visible,maxUnmatchedFraction=max_error,stamp=emitter.radar._result.ts_frame,seconds=round(time.perf_counter()-started,3),byteIdentical=True)),flush=True)
    finally:
        if emitter.radar._session:emitter.radar._session.close()
