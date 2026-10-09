"""Immutable opaque Natural Earth raster tiles; Pillow is used only in the worker."""
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import struct
import tempfile
import zlib
import threading
import time
from collections import deque

RENDER_TIMES = deque(maxlen=512)

from lib.radar_geometry import world_point, world_inverse
import hashlib

DATA_PATH = Path(__file__).with_name('data') / 'radar-natural-earth.bin'
CLASSES = ('bm-ocean', 'bm-lake', 'bm-coast', 'bm-admin0', 'bm-admin1', 'bm-road', 'bm-road')


def clip_polygon(points, size):
    """Sutherland–Hodgman, against the four edges of the plate."""
    width, height = (size, size) if isinstance(size, (int, float)) else size
    for axis, bound, sign in ((0,0,1), (0,width,-1), (1,0,1), (1,height,-1)):
        if not points:
            break
        output = []
        a = points[-1]
        for b in points:
            ai, bi = sign*(a[axis]-bound) >= 0, sign*(b[axis]-bound) >= 0
            if ai != bi:
                t = (bound-a[axis]) / (b[axis]-a[axis])
                p = [a[0]+t*(b[0]-a[0]), a[1]+t*(b[1]-a[1])]
                p[axis] = bound
                output.append(tuple(p))
            if bi:
                output.append(b)
            a = b
        points = output
    return points


def clip_segment(a, b, size):
    """Liang–Barsky; None for a segment wholly outside the plate."""
    width, height = (size, size) if isinstance(size, (int, float)) else size
    dx, dy = b[0]-a[0], b[1]-a[1]
    lo, hi = 0., 1.
    for p, q in ((-dx,a[0]), (dx,width-a[0]), (-dy,a[1]), (dy,height-a[1])):
        if p == 0:
            if q < 0:
                return None
        elif p < 0:
            lo = max(lo, q/p)
        else:
            hi = min(hi, q/p)
        if lo > hi:
            return None
    return ((a[0]+lo*dx, a[1]+lo*dy), (a[0]+hi*dx, a[1]+hi*dy))


def clip_line(points, size):
    result, current = [], []
    for a, b in zip(points, points[1:]):
        segment = clip_segment(a, b, size)
        if segment is None:
            if current: result.append(current)
            current = []
        else:
            a, b = segment
            if current and current[-1] == a:
                current.append(b)
            else:
                if current: result.append(current)
                current = [a,b]
    if current: result.append(current)
    return result


def simplify(points, tolerance=1.):
    """Iterative Douglas–Peucker; bounded stack, including closed polygon rings."""
    if len(points) < 3:
        return points
    keep = {0,len(points)-1}
    stack = [(0,len(points)-1)]
    while stack:
        start,end = stack.pop()
        a,b = points[start],points[end]
        dx,dy = b[0]-a[0],b[1]-a[1]
        length = dx*dx+dy*dy
        maximum, index = tolerance*tolerance, None
        for i in range(start+1,end):
            p = points[i]
            t = max(0,min(1,((p[0]-a[0])*dx+(p[1]-a[1])*dy)/length)) if length else 0
            dist = (p[0]-a[0]-t*dx)**2+(p[1]-a[1]-t*dy)**2
            if dist > maximum:
                maximum,index = dist,i
        if index is not None:
            keep.add(index); stack.extend(((start,index),(index,end)))
    return [points[i] for i in sorted(keep)]


@lru_cache(maxsize=1)
def _index(path):
    with open(path,'rb') as stream:
        if stream.read(8) != b'NEBM0001':
            raise ValueError('invalid basemap artifact')
        length = struct.unpack('<I',stream.read(4))[0]
        metadata = json.loads(stream.read(length))
        index_length = struct.unpack('<I',stream.read(4))[0]
        index = zlib.decompress(stream.read(index_length))
        if len(index) != 360*180*8:
            raise ValueError('truncated basemap index')
        return metadata['quant'], index, stream.tell()


@lru_cache(maxsize=256)
def _cell(path, x, y):
    quant, index, start = _index(path)
    offset, length = struct.unpack_from('<II',index,((y+90)*360+x+180)*8)
    if not length:
        return ((0, (((0,0),(quant,0),(quant,quant),(0,quant),(0,0)),)),) if offset == 1 else ()
    with open(path,'rb') as stream:
        stream.seek(start+offset)
        raw = zlib.decompress(stream.read(length))
    entries, pos = [], 0
    while pos < len(raw):
        layer, count = struct.unpack_from('<BH',raw,pos); pos += 3
        rings = []
        for _ in range(count):
            length = struct.unpack_from('<H',raw,pos)[0]; pos += 2
            ring = list(struct.iter_unpack('<HH',raw[pos:pos+length*4])); pos += length*4
            rings.append(ring)
        entries.append((layer,rings if layer < 2 else rings[0]))
    return entries


def stitch(lines):
    """Join degree-cell fragments at shared endpoints, without joining branches.

    Quantized geographic endpoints (not rounded projected pixels) avoid joining
    unrelated lines merely because they land on the same display pixel.
    """
    fragments = {i: list(line) for i, line in enumerate(lines) if len(line) >= 2}
    ends = {}
    key = lambda p: (round(p[0], 7), round(p[1], 7))
    for i, line in fragments.items():
        for p in (line[0], line[-1]):
            ends.setdefault(key(p), set()).add(i)
    output = []
    while fragments:
        ident, line = fragments.popitem()
        for reverse in (False, True):
            if reverse: line.reverse()
            while True:
                ids = ends.get(key(line[-1]), set())
                candidates = ids.intersection(fragments)
                if len(candidates) != 1 or len(ids) != 2: break
                other = fragments.pop(candidates.pop())
                if key(other[-1]) == key(line[-1]): other.reverse()
                line.extend(other[1:])
        output.append(line)
    return output


def ocean_runs(cells):
    """Maximal horizontal runs of full-ocean degree cells."""
    rows = {}
    for x, y in cells: rows.setdefault(y, []).append(x)
    for y, xs in sorted(rows.items()):
        xs = sorted(set(xs)); start = end = xs[0]
        for x in xs[1:] + [None]:
            if x == end + 1:
                end = x; continue
            yield [(start,y), (end+1,y), (end+1,y+1), (start,y+1), (start,y)]
            start = end = x


# Both tables are normative resolved composites: land, water, then each stroke
# over land/water. Coverage overwrites previous strokes, never blends with them.
STYLE_REVISION = b'plate-paper-inset-night:1'
RENDER_REVISION = b'png8-evenodd-dp05-box4-overwrite:1'
STYLES = {
    'paper': ('EBE6DB','DFDCD4', [('9BA9AE','95A3AA'),('9F9B90','99968C'),
                ('CFCBC0','C5C2BA'),('BAB6AB','B2AFA6'),('BAB6AB','B2AFA6')]),
    'night': ('0B0D11','151B21', [('3F525F','445A68'),('686766','6D6E6E'),
                ('575756','5E6061'),('464747','4D5052'),('464747','4D5052')]),
}

@lru_cache(maxsize=1)
def version(data_path=None):
    return hashlib.sha256(Path(data_path or DATA_PATH).read_bytes()+STYLE_REVISION+RENDER_REVISION+
                          repr(STYLES).encode()).hexdigest()[:12]

@lru_cache(maxsize=2)
def palette(theme):
    ground, water, strokes = STYLES[theme]
    rgb = lambda h: tuple(bytes.fromhex(h))
    backdrops = [rgb(ground),rgb(water)]
    colors = list(backdrops)
    for pair in strokes:
        for backdrop, full in zip(backdrops,map(rgb,pair)):
            for coverage in range(1,17):
                colors.append(tuple((b*(16-coverage)+f*coverage+8)//16 for b,f in zip(backdrop,full)))
    return colors


def tile_layers(z,x,y,data_path=None):
    path=str(data_path or DATA_PATH);quant,index,_=_index(path)
    # A one-pixel gutter gives strokes continuous coverage across tile edges.
    north,west=world_inverse(x*256-1,y*256-1,z)
    south,east=world_inverse((x+1)*256+1,(y+1)*256+1,z)
    layers=[[] for _ in CLASSES];ocean=[]
    for sy in range(max(-86,math.floor(south)),min(86,math.ceil(north))):
        for sx in range(math.floor(west),math.ceil(east)):
            wrapped=(sx+180)%360-180
            offset,length=struct.unpack_from('<II',index,((sy+90)*360+wrapped+180)*8)
            if not length and offset==1:
                ocean.append((sx,sy));continue
            for layer,geometry in _cell(path,wrapped,sy):
                if layer==4 and z<5 or layer==5 and z<6 or layer==6 and z<8:continue
                for ring in geometry if layer<2 else [geometry]:
                    layers[layer].append([(sx+dx/quant,sy+dy/quant) for dx,dy in ring])
    layers[0].extend(ocean_runs(ocean))
    for layer in range(2,7):layers[layer]=stitch(layers[layer])
    for layer,rings in enumerate(layers):
        output=[]
        for ring in rings:
            points=[(px-x*256+1,py-y*256+1) for lon,lat in ring for px,py in [world_point(lat,lon,z)]]
            pieces=[clip_polygon(points,258)] if layer<2 else clip_line(points,258)
            for piece in pieces:
                simplified=simplify(piece,.5)
                if len(simplified)>=(3 if layer<2 else 2):output.append([(px-1,py-1) for px,py in simplified])
        layers[layer]=output
    return layers


def evenodd_mask(rings):
    """One scanline pass for all compound rings; pixel centres, half-open edges."""
    from PIL import Image
    rows=[[] for _ in range(256)]
    for ring in rings:
        for a,b in zip(ring,ring[1:]+ring[:1]):
            if a[1]==b[1]:continue
            if a[1]>b[1]:a,b=b,a
            for y in range(max(0,math.ceil(a[1]-.5)),min(256,math.ceil(b[1]-.5))):
                rows[y].append(math.ceil(a[0]+(y+.5-a[1])*(b[0]-a[0])/(b[1]-a[1])-.5))
    raw=bytearray(65536)
    for y,crossings in enumerate(rows):
        crossings.sort()
        for left,right in zip(crossings[::2],crossings[1::2]):
            left,right=max(0,left),min(256,right)
            if right>left:raw[y*256+left:y*256+right]=bytes([1])*(right-left)
    return Image.frombytes('L',(256,256),bytes(raw))


def tile(theme,z,x,y,data_path=None):
    """256px opaque PNG-8; fixed 162-entry palette, no dither or alpha."""
    from PIL import Image,ImageDraw,ImageChops
    import io
    if theme not in STYLES or type(z) is not int or not 4<=z<=10 or any(type(n) is not int or not 0<=n<2**z for n in (x,y)):
        raise ValueError('invalid basemap tile')
    started=time.perf_counter()
    layers=tile_layers(z,x,y,data_path)
    fill=evenodd_mask(layers[0]+layers[1]);pixels=fill.copy()
    backdrop=fill.point([v*16 if v<2 else 0 for v in range(256)])
    coverage=Image.new('L',(1024,1024));draw=ImageDraw.Draw(coverage)
    for layer in range(2,7):
        draw.rectangle((0,0,1024,1024),fill=0)
        for line in layers[layer]:
            if layer!=4:draw.line([(round(px*4),round(py*4)) for px,py in line],fill=255,width=4)
            else:
                phase=0.
                for a,b in zip(line,line[1:]):
                    length=math.hypot(b[0]-a[0],b[1]-a[1]);pos=0.
                    while pos<length:
                        step=min(length-pos,(4 if phase<4 else 7)-phase)
                        if phase<4 and length:
                            points=[(round((a[0]+(b[0]-a[0])*t/length)*4),round((a[1]+(b[1]-a[1])*t/length)*4)) for t in (pos,pos+step)]
                            draw.line(points,fill=255,width=4)
                        pos+=step;phase=(phase+step)%7
        reduced=coverage.reduce(4)
        # The exact former integer palette expression, evaluated in Pillow C
        # across the raster. Only the fixed 256-entry LUT is built in Python.
        indices=reduced.point([1+(layer-2)*32+min(16,(v+8)//16) for v in range(256)])
        combined=ImageChops.add(indices,backdrop)
        mask=reduced.point([0]+[255]*255)
        pixels.paste(combined,(0,0),mask)
        indices.close();combined.close();mask.close()
        reduced.close()
    image=pixels.convert('P');image.putpalette([v for color in palette(theme) for v in color])
    out=io.BytesIO();image.save(out,'PNG',optimize=False,compress_level=9)
    image.close();fill.close();coverage.close();pixels.close();backdrop.close()
    raw=out.getvalue()
    RENDER_TIMES.append(dict(theme=theme,z=z,x=x,y=y,ms=(time.perf_counter()-started)*1000,bytes=len(raw)))
    return raw


def tile_path(radar_dir,theme,z,x,y):
    return Path(radar_dir)/'geo'/version()/theme/str(z)/str(x%2**z)/f'{y}.png'


def remove_empty_parents(path,root):
    parent=path.parent
    while parent!=root and root in parent.parents:
        try:parent.rmdir()
        except OSError:break
        parent=parent.parent


def home_requests(station,home_zoom=8,theme='paper'):
    home_zoom=max(4,min(10,home_zoom))
    levels=list(dict.fromkeys([home_zoom,home_zoom-1,home_zoom+1,4]+sorted(range(4,11),key=lambda z:abs(z-home_zoom))))
    for theme in (theme,'night' if theme=='paper' else 'paper'):
        for z in levels:
            if not 4<=z<=10:continue
            px,py=world_point(*station,z);cx,cy=int(px//256),int(py//256)
            offsets=sorted(((dx,dy) for dy in range(-2,3) for dx in range(-3,4)),key=lambda d:(abs(d[0])>2 or abs(d[1])>1,math.hypot(*d)))
            for dx,dy in offsets:
                if 0<=cy+dy<2**z:yield theme,z,(cx+dx)%2**z,cy+dy


GEO_MAX_FILES,GEO_MAX_BYTES=6000,32_000_000
GEO_LOW_WATER=.9  # an eviction pass frees a tenth of the cap, so passes are rare


class GeoCache:
    """Running file/byte totals for one geo cache; the disk is walked once,
    then again only when an admission would exceed a cap. That walk resyncs
    the totals and evicts least-recently-served tiles (atime) down to the
    low-water mark, so a full cache pays one walk per ~600 new tiles instead
    of one per tile. Only the geo worker writes here; the lock is a guard."""
    def __init__(self,radar_dir):
        self.root=Path(radar_dir)/'geo';self.count=None;self.size=0;self.lock=threading.Lock()
    def _walk(self):
        records=[]
        for p in self.root.glob('*/*/*/*/*.png'):
            try:st=p.stat()
            except FileNotFoundError:continue
            records.append((st.st_atime,st.st_size,p))
        self.count,self.size=len(records),sum(r[1] for r in records)
        return records
    def fits(self,incoming_size=0,incoming_files=0,low=1.):
        return self.count+incoming_files<=GEO_MAX_FILES*low and self.size+incoming_size<=GEO_MAX_BYTES*low
    def prune(self,pinned=(),incoming_size=0,incoming_files=0):
        with self.lock:
            if self.count is None:self._walk()
            if self.fits(incoming_size,incoming_files):return True
            pinned=set(pinned)
            for _,length,p in sorted(self._walk()):
                if self.fits(incoming_size,incoming_files,GEO_LOW_WATER):break
                if p in pinned:continue
                p.unlink(missing_ok=True);remove_empty_parents(p,self.root);self.count-=1;self.size-=length
            return self.fits(incoming_size,incoming_files)
    def added(self,length):
        with self.lock:
            if self.count is not None:self.count+=1;self.size+=length


_GEO_CACHES={}
_GEO_CACHES_LOCK=threading.Lock()


def geo_cache(radar_dir):
    with _GEO_CACHES_LOCK:
        key=str(Path(radar_dir))
        if key not in _GEO_CACHES:_GEO_CACHES[key]=GeoCache(radar_dir)
        return _GEO_CACHES[key]


def prune(radar_dir,pinned=(),incoming_size=0,incoming_files=0):
    return geo_cache(radar_dir).prune(pinned,incoming_size,incoming_files)


def cache_tile(radar_dir,theme,z,x,y,pinned=()):
    target=tile_path(radar_dir,theme,z,x,y)
    if target.is_file():return target
    raw=tile(theme,z,x,y)
    cache=geo_cache(radar_dir)
    if not cache.prune(pinned,len(raw),1):return None
    atomic_write(target,raw)
    cache.added(len(raw))
    return target


def atomic_write(target,raw):
    target.parent.mkdir(parents=True,exist_ok=True);temp=None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent,prefix='.tile-',delete=False) as out:
            temp=out.name;out.write(raw)
        os.replace(temp,target)
    finally:
        if temp and os.path.exists(temp):os.unlink(temp)


def publish_revision(radar_dir):
    target=Path(radar_dir)/'.geo-revision'
    if not target.is_file() or target.read_text()!=version():
        atomic_write(target,version().encode())


class WarmState:
    """Only the geo worker owns this queue; completed home sets need no rescans."""
    def __init__(self):
        self.identity=None
        self.home=deque()
        self.pinned=set()

    def prepare(self,radar_dir,station,home_zoom,theme):
        identity=(str(radar_dir),version(),tuple(station),home_zoom)
        if self.identity==identity:return
        requests=list(home_requests(station,home_zoom,theme))
        publish_revision(radar_dir)
        self.home=deque(requests)
        self.pinned={tile_path(radar_dir,*r) for r in requests}
        self.identity=identity


def viewport_requests(center,zoom,theme):
    # The same central 5x3, then 7x5 margin order as home, at this camera only.
    return (r for r in home_requests((center['lat'],center['lon']),zoom,theme)
            if r[0]==theme and r[1]==zoom)


def warm(radar_dir,station,center=None,zoom=None,home_zoom=8,theme='paper',limit=1,state=None):
    """One tile by default: current viewport first, then resumable home order."""
    state=state if state is not None else WarmState()
    state.prepare(radar_dir,station,home_zoom,theme)
    made=0
    for r in viewport_requests(center,zoom,theme) if center is not None else ():
        if not tile_path(radar_dir,*r).is_file():
            if cache_tile(radar_dir,*r,pinned=state.pinned) is None:return made
            made+=1
            if made>=limit:return made
    while state.home:
        r=state.home[0]
        if not tile_path(radar_dir,*r).is_file():
            if cache_tile(radar_dir,*r,pinned=state.pinned) is None:return made
            made+=1
        state.home.popleft()
        if made>=limit:break
    return made
