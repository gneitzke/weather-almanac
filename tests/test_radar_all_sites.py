"""Every US radar: site mode beyond CONUS, and Level III identities for the whole site table."""
import pytest

from lib import almanac_emit as ae, radar_level3 as l3
from lib import radar_engine
from tests.test_radar_hybrid import hybrid, make_config  # noqa: F401
from tests.test_radar_level3 import product, SITE

# Swept 2026-09-25 from the Pi 4: 157 of 160 sites decoded with the production
# decoder; KGGW was off the air, LPLA and RODN are not in NOAA's bucket.
NOT_IN_BUCKET = {'LPLA', 'RODN'}


def test_every_site_has_a_level3_identity():
    for site, (lat, lon, name) in radar_engine._NEXRAD_SITES.items():
        assert len(site) == 4 and site.isalnum() and site.isupper(), site
        assert -90 <= lat <= 90 and -180 <= lon <= 180 and name
        key = '%s_N0B_2026_09_25_03_42_24' % site[1:]
        assert l3.s3_key_time(key) is not None


@pytest.mark.parametrize('lat,lon,site', [
    (61.22, -149.9, 'PAHG'),   # Anchorage
    (21.31, -157.86, 'PHMO'),  # Honolulu: Molokai is nearest
    (18.46, -66.1, 'TJUA'),    # San Juan
    (13.48, 144.75, 'PGUA'),   # Guam
    (49.05, -122.45, 'KATX'),  # just over the Canadian border, KATX in range
])
def test_site_mode_is_offered_wherever_a_radar_is_in_range(make_emitter, hybrid, lat, lon, site):
    emitter = make_emitter(config=make_config(Station={'Latitude': str(lat), 'Longitude': str(lon)}))
    emitter.radar._acquire()
    sources = emitter._build_payload()['radar']['sources']
    assert sources[1]['siteId'] == site and sources[1]['available'] is True, sources


def test_no_radar_in_range_offers_no_site(make_emitter, hybrid):
    emitter = make_emitter(config=make_config(Station={'Latitude': '52.52', 'Longitude': '13.4'}))
    emitter.radar._acquire()
    sources = emitter._build_payload()['radar']['sources']
    assert sources[1]['available'] is False and sources[1]['reason'] == 'no site in range'


@pytest.mark.parametrize('tenths,ok', [(-10, True), (-2, True), (5, True), (20, True), (-11, False), (21, False)])
def test_lowest_tilt_range(tenths, ok):
    raw = product(elevation=tenths)
    if ok:
        assert l3.decode(raw, expect_site=SITE).elevation_deg == tenths / 10
    else:
        with pytest.raises(ValueError, match='lowest tilt'):
            l3.decode(raw, expect_site=SITE)



def test_decoded_scan_cache_holds_a_whole_loop():
    # A multi-site loop needs every site's scan per frame plus the next per site;
    # an LRU smaller than that working set misses on every in-order walk (the
    # LRU mechanics themselves: test_radar_level3_review).
    assert radar_engine.RADAR_LEVEL3_SCAN_CACHE >= radar_engine.RADAR_SITE_MAX_COUNT * (radar_engine.RADAR_LOOP_FRAMES + 1)
