"""Astra's adversarial review, page side: coverage is judged on the cells the
viewport shows, from each native tile's radarMeasuredGrid. Production
console_live.html functions in the Node harness; tile grids come from the real
mosaic renderer. No browser, no network."""
import json
import math

import numpy as np

from lib import almanac_emit as ae
from lib import radar_engine
from lib import radar_mosaic as mosaic
from lib.radar_level3 import Scan
from lib.radar_palette import source_palette
from tests.test_radar_buffer_page import run_page
from tests.test_radar_review_oct_page import PNG


def flat_scan(code, lat=47.61, lon=-122.33):
    codes = np.full((720, 1840), code, np.uint8)
    return Scan(lat, lon, 100., .5, 215, 1789257600, codes, (np.arange(3600) // 5).astype(np.int32))


def grids(lat, lon, z, scans, spread=3):
    """Real native tile chunks around (lat, lon): {'x/y': [uncovered, grid]}."""
    n = 2**z
    cx = int((lon+180)/360*n)
    cy = int((1-math.asinh(math.tan(math.radians(lat)))/math.pi)/2*n)
    palette = source_palette('iem-nexrad-n0b')
    out = {}
    for x in range(cx-spread, cx+spread+1):
        for y in range(cy-spread, cy+spread+1):
            image, _ = mosaic.render_mosaic(scans, z, x, y, palette)
            out[f'{x}/{y}'] = [image.info['radarUncoveredPixels'], image.info['radarMeasuredGrid']]
    return out


# Paint the newest frame from tiles whose metadata the page decodes itself.
PAINT = PNG + r'''
radarCamera={lat:LAT,lon:LON,zoom:ZOOM};radarView.data.partialCoverage=false;
const chunks=GRIDS,f=radarView.loaded.at(-1);f.bitmap.close();delete f.bitmap;f.ready=false;
let wholeTileUncovered=0;
for(const t of radarTileSet(radarCamera,radarLevel())){
  const [uncovered,grid]=chunks[t.x+'/'+t.y];wholeTileUncovered+=uncovered;
  const meta=radarPNGMeta(png([['radarRemap',remap],['radarVisiblePixels','0'],['radarUncoveredPixels',String(uncovered)],['radarMeasuredGrid',grid]]),{remapRevision:'rv1'});
  radarTiles.set(radarTileKey(f,t.z,t.x,t.y),{bitmap:bitmap(256,256),meta,hasEcho:false,measured:meta.measured,sites:[]});
}
radarEchoPaint(f);
'''


def paint(lat, lon, zoom, scans):
    return PAINT.replace('GRIDS', json.dumps(grids(lat, lon, zoom, scans))).replace(
        'LAT', repr(lat)).replace('LON', repr(lon)).replace('ZOOM', str(zoom))


def test_a_covered_viewport_is_clear_although_its_tiles_reach_beyond_range():
    # The review's reproduction: valid-clear scan at KATX's position, zoom 8,
    # the kiosk's 956x490 view. Every visible pixel was measured; the tiles'
    # off-screen parts were not.
    run_page(paint(47.61, -122.33, 8, [flat_scan(0)]) + r'''
assert.ok(wholeTileUncovered>0,'the reproduction needs tiles with unmeasured pixels off screen');
assert.equal(f.uncovered,0,'cells clipped outside the viewport were counted');
assert.equal(radarView.clear,true);
''')


def test_unmeasured_cells_in_view_are_partial():
    # 205 km east of the radar: the eastern part of the view is past 230 km.
    run_page(paint(47.61, -119.6, 8, [flat_scan(0)]) + r'''
assert.ok(f.uncovered>0);assert.equal(radarView.clear,false);
''')
    # Missing gates over the whole view: transparent, but nothing was measured.
    run_page(paint(47.61, -122.33, 8, [flat_scan(1)]) + r'''
assert.ok(f.uncovered>0);assert.equal(radarView.clear,false);
''')


def test_gaps_count_only_the_visible_crop_of_a_tile():
    run_page(r'''
const east=new Uint8Array(256).fill(1);for(let r=0;r<16;r++)east[r*16+15]=0;    // column 15 unmeasured
assert.equal(radarGaps(null,0,0,256,0,0,256,256),0);
assert.equal(radarGaps(east,0,0,256,0,0,256,256),16);
assert.equal(radarGaps(east,0,0,256,956-100,0,256,256),0,'only columns 0-6 are on screen');
assert.equal(radarGaps(east,0,0,256,-16,0,256,256),16);
assert.equal(radarGaps(east,0,0,256,-241,0,256,256),16,'one visible pixel column is cell 15');
assert.equal(radarGaps(east,0,0,256,-240,0,256,256),16);
assert.equal(radarGaps(east,0,0,256,0,490-32,256,256),2,'rows 0-1 only');
// Scaled draws (fractional zoom) and a parent tile's sub-square.
assert.equal(radarGaps(east,0,0,256,956-400,0,512,512),0);
assert.equal(radarGaps(east,128,0,128,0,0,256,256),8,'the north-east quarter of a parent tile: rows 0-7');
assert.equal(radarGaps(east,0,0,128,0,0,256,256),0,'its north-west quarter');
''')


def test_composite_job_counts_the_visible_crop():
    run_page(PNG + r'''
radarCamera={lat:47,lon:-122,zoom:8};
const f=radarView.loaded.at(-1);f.bitmap.close();delete f.bitmap;f.levels={8:true};f.ready=false;
radarView.loaded=[f];radarView.cycle=[f];radarView.current=f;radarView.holdingWindow=false;
const z=radarLevel(),p=radarWorldPoint(radarCamera.lat,radarCamera.lon,z),tiles=radarTileSet(radarCamera,z);
// Every tile is unmeasured in the cells that lie off screen, measured in those on screen.
for(const t of tiles){const m=new Uint8Array(256);
  for(let r=0;r<16;r++)for(let c=0;c<16;c++){const sx=Math.round(478+t.x*256-p[0])+c*16,sy=Math.round(245+t.y*256-p[1])+r*16;
    m[r*16+c]=sx+16>0&&sx<956&&sy+16>0&&sy<490?1:0;}
  radarTiles.set(radarTileKey(f,z,t.x,t.y),{bitmap:bitmap(256,256),meta:{opaquePixels:0,unmatchedPixels:0,ambiguousPixels:0},hasEcho:false,measured:m,sites:[]});}
radarCompositeJob=null;radarHistoryWork();
assert.equal(f.ready,true);assert.equal(f.uncovered,0);
// One visible unmeasured cell makes the plate partial.
const t=tiles[0],m=radarTiles.get(radarTileKey(f,z,t.x,t.y)).measured,i=m.indexOf(1);m[i]=0;
f.bitmap.close();delete f.bitmap;f.ready=false;radarCompositeJob=null;radarHistoryWork();
assert.equal(f.uncovered,1);
''')


def test_site_layer_disc_cells_match_the_engine_and_union():
    z = 8
    n = 2**z
    cx = int((-122.33+180)/360*n)
    cy = int((1-math.asinh(math.tan(math.radians(47.61)))/math.pi)/2*n)
    old = radar_engine._NEXRAD_SITES
    radar_engine._NEXRAD_SITES = {'KNEA': (47.61, -122.33, 'n')}
    try:
        expected = {f'{x}/{y}': radar_engine._radar_disc_grid('KNEA', z, x, y) for x in range(cx-4, cx+5) for y in range(cy-3, cy+4)}
    finally:
        radar_engine._NEXRAD_SITES = old
    assert len(set(expected.values())) > 3      # inside, outside and edge tiles
    run_page(r'''
radarSiteTable=[{id:'KNEA',lat:47.61,lon:-122.33}];
const hex=a=>Array.from({length:64},(_,i)=>(a[4*i]<<3|a[4*i+1]<<2|a[4*i+2]<<1|a[4*i+3]).toString(16)).join('');
for(const [key,grid] of Object.entries(EXPECTED)){const [x,y]=key.split('/').map(Number);
  assert.equal(hex(radarDiscCells('KNEA',ZOOM,x,y)),grid,key);}
assert.equal(hex(radarDiscCells('KZZZ',ZOOM,0,0)),'0'.repeat(64),'an unknown site measures nothing');
const west=new Uint8Array(256),east=new Uint8Array(256);for(let i=0;i<256;i++){west[i]=i%16<8;east[i]=i%16>=8;}
assert.ok(radarUnionCells(west,east).every(b=>b===1));
assert.equal(radarUnionCells(null,west),null);assert.equal(radarUnionCells(west,null),null);
assert.equal(radarUnionCells(west,west).reduce((n,b)=>n+b,0),128);assert.equal(west.reduce((n,b)=>n+b,0),128,'inputs untouched');
'''.replace('EXPECTED', json.dumps(expected)).replace('ZOOM', str(z)))


def test_png_grid_must_agree_with_its_count():
    run_page(PNG + r'''
const f={remapRevision:'rv1'},meta=(u,g)=>radarPNGMeta(png([['radarRemap',remap],['radarVisiblePixels','0'],['radarUncoveredPixels',u],['radarMeasuredGrid',g]]),f);
const m=meta('256','7'+'f'.repeat(63)).measured;
assert.equal(m.length,256);assert.equal(m[0],0);assert.equal(m[1],1);assert.equal(m.reduce((n,b)=>n+b,0),255);
assert.equal(meta('0','f'.repeat(64)).measured.every(b=>b===1),true);
for(const [u,g] of [['0','7'+'f'.repeat(63)],['1','f'.repeat(64)],['65280','f'.repeat(63)+'e'],['256','F'.repeat(64)],['256','f'.repeat(63)]])
  assert.throws(()=>meta(u,g),/tile coverage/,u+' '+g);
assert.throws(()=>radarPNGMeta(png([['radarRemap',remap],['radarVisiblePixels','0'],['radarMeasuredGrid','f'.repeat(64)]]),f),/tile coverage/);
''')
