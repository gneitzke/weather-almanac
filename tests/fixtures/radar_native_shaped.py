"""Deterministic storm-shaped native fixture, NOT an observed weather scan.

720 half-degree rays x 1840 quarter-km gates: a curved frontal band,
embedded convective cells, below-floor rain fringe, clutter and missing gates.
The native renderer sees the same polar geometry as decoded Level III.
"""
import numpy as np
from lib.radar_level3 import Scan


def scans():
    bearing = np.radians((np.arange(720)+.5)*.5)[:, None]
    radius = (np.arange(1840)+.5)[None, :]*.25
    east, north = radius*np.sin(bearing), radius*np.cos(bearing)
    spine = 45 + 12*np.sin(north/24)
    dbz = -5 + 34*np.exp(-((east-spine)/9)**2) * np.exp(-((north+15)/95)**4)
    for e, n, strength, width in ((39,-8,25,3), (48,-19,28,4), (55,-34,22,3), (24,30,38,6)):
        dbz += strength*np.exp(-((east-e)**2+(north-n)**2)/(2*width**2))
    dbz += 1.5*np.sin(east*2+np.cos(north)) * np.exp(-((east-spine)/13)**2)
    codes = np.clip(np.rint((dbz+32)*2+2), 2, 255).astype(np.uint8)
    codes[dbz < -3] = 0
    codes[205:211, 190:230] = 1
    table = np.arange(3600, dtype=np.int16)//5
    table[1000:1004] = -1
    scan = Scan(47.61, -122.33, 196, .5, 215, 1789257624, codes, table)
    hca = np.full((360, 1200), 60, np.uint8)
    hca[110:115, 175:200] = 20
    hca[80:85, 150:170] = 10
    classes = Scan(scan.lat, scan.lon, scan.height_m, .5, 215, scan.volume_ts,
                   hca, np.arange(3600, dtype=np.int16)//10)
    return scan, classes
