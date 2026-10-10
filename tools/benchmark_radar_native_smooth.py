"""Offline per-tile native Smooth timing, including PNG encoding and memory.

Run on the target machine for target timings; this never fetches radar data.
Defaults to the storm-shaped fixture. Optional --n0b/--n0h accept local products.
"""
import argparse
import io
import json
from pathlib import Path
import platform
import statistics
import sys
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import radar_mosaic as mosaic
from lib.radar_level3 import Scan, decode, decode_n0h
from lib.radar_geometry import world_point
from lib.radar_palette import source_palette
from tests.fixtures.radar_native_shaped import scans


def inputs(n0b=None, n0h=None):
    scan, hca = scans() if n0b is None else (decode(n0b.read_bytes(), speckle_dbz=15),
                                           decode_n0h(n0h.read_bytes()) if n0h else None)
    return mosaic.quality_control(scan, hca)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--n0b', type=Path)
    parser.add_argument('--n0h', type=Path)
    parser.add_argument('--iterations', type=int, default=24)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    scan = inputs(args.n0b, args.n0h)
    palette = source_palette('iem-nexrad-n0b')
    report = dict(platform=platform.platform(), python=platform.python_version(),
                  fixture='local N0B' if args.n0b else 'synthetic storm-shaped polar fixture', rows=[])
    for count in (1, 4):
        candidates = [Scan(scan.lat+i*.06, scan.lon-i*.06, scan.height_m+i*100, scan.elevation_deg,
                      scan.vcp, scan.volume_ts, scan.codes, scan.bearing_index) for i in range(count)]
        for z in (8, 9, 10):
            px, py = world_point(scan.lat-.15, scan.lon+.6, z)
            x, y = int(px//256), int(py//256)
            for cold in (False, True):
                for smooth in (False, True):
                    mosaic.clear_geometry_cache()
                    render, total = [], []
                    for i in range(args.iterations+1):
                        if cold:
                            mosaic.clear_geometry_cache()
                        start = time.perf_counter()
                        image, _ = mosaic.render_mosaic(candidates, z, x, y, palette, smooth=smooth)
                        drawn = time.perf_counter()
                        image.save(io.BytesIO(), format='PNG'); image.close()
                        if i:
                            render.append((drawn-start)*1000)
                            total.append((time.perf_counter()-start)*1000)
                    mosaic.clear_geometry_cache()
                    tracemalloc.start()
                    mosaic.render_mosaic(candidates, z, x, y, palette, smooth=smooth)[0].close()
                    peak = tracemalloc.get_traced_memory()[1]; tracemalloc.stop()
                    row = dict(sites=count, zoom=z, cold=cold, smooth=smooth,
                               median_ms=round(statistics.median(render), 3),
                               p95_ms=round(sorted(render)[int(.95*(len(render)-1))], 3),
                               png_median_ms=round(statistics.median(total), 3), peak_bytes=peak)
                    report['rows'].append(row)
                    print(row, flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(report['platform'])


if __name__ == '__main__':
    main()
