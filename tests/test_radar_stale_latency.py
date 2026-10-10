""" Site radar staleness = "a scan we should have received is missing".

Live regression (round 1, L9): the threshold was 2.5 x cadence (780 s at a
300 s cadence). Normal age just before the next scan lands is publication
latency + one interval; on the Pi it climbed to 839-869 s, radar.stale went
true for about 1.5 minutes, then the next frame arrived (obsTs stepped 540 s).
The threshold now adds the site's measured publication latency (scan time to
first seen in the IEM listing) to two intervals, bounded 8 to 20 minutes.
Hermetic: listings are fed straight to the latency recorder.
"""
from datetime import timezone

from lib import almanac_emit as ae
from lib import radar_engine
from tests.fixtures import obs_scenarios as scn

T = 1_791_576_900   # the live obsTs before the 540 s step


def site_snapshot(anchor, cadence, latency):
    frame = dict(ts=anchor, stamp='x', complete=True, levels={}, mosaicKey='Mabc',
                 siteScans=[dict(id='KATX', ts=anchor)], acquiredSites=[dict(id='KATX', ts=anchor)],
                 requestedPairs=[['KATX', anchor]])
    return radar_engine._RADAR_NONE._replace(available=True, reason=None, frames=(frame,), ts_frame=anchor,
        source_id='iem-nexrad-n0b', source_mode='site', stale_sec=900, tiles=dict(frames=[dict(frame)], variant='native'),
        scan_cadence_sec=cadence, scan_latency_sec=latency, units='mi', legend={}, sources=(), sites=())


def payload(snap, now):
    return radar_engine.RadarEngine._payload(snap, now, timezone.utc)


# ------------------------------------------------------------ the live numbers
def test_live_ages_between_scans_are_not_stale_with_measured_latency():
    # cadence 300 s, latency measured 300-540 s: p90 lands near the top.
    for age in (839, 845, 869):
        r = payload(site_snapshot(T, 300, 540), T + age)
        assert r['staleSec'] == 1140 and r['stale'] is False, age


def test_live_ages_are_not_stale_before_any_latency_is_measured():
    # Default latency 300 + 2 x 300 = 900: the 869 s peak stays current.
    r = payload(site_snapshot(T, 300, None), T + 869)
    assert r['staleSec'] == 900 and r['stale'] is False


def test_the_old_threshold_would_have_flashed_stale():
    # Documents the regression: L9's rule is now only the neighbour blend limit.
    assert radar_engine._radar_neighbour_limit_sec(300) == 780 < 869


# ------------------------------------------------------------ genuinely missed
def test_missed_scans_go_stale():
    # Two scans past due beyond the measured latency: stale.
    r = payload(site_snapshot(T, 300, 330), T + 330 + 2*300 + 60)
    assert r['staleSec'] == 960 and r['stale'] is True
    # Just before: still current.
    r = payload(site_snapshot(T, 300, 330), T + 950)
    assert r['stale'] is False


# ----------------------------------------------------------------------- bounds
def test_bounds_and_whole_minutes():
    assert radar_engine._radar_site_stale_sec(120, 0) == 480            # SAILS, instant: never under 8 min
    assert radar_engine._radar_site_stale_sec(600, 900) == 1200         # clear air, slow: never over 20 min
    assert radar_engine._radar_site_stale_sec(300, 301) == 960          # rounded up to the minute
    assert radar_engine._radar_site_stale_sec(None, None) == 900        # unknown cadence: nominal 300 s
    assert radar_engine._radar_site_stale_sec(300, -50) == 600          # a negative sample is not latency


def test_latency_estimate_is_a_robust_high_percentile():
    assert radar_engine._radar_scan_latency([]) is None
    assert radar_engine._radar_scan_latency([300, 320]) is None          # too few samples: the default applies
    samples = [300, 310, 320, 330, 340, 350, 360, 380, 400, 540]
    assert radar_engine._radar_scan_latency(samples) == 400              # p90 of ten: one outlier does not set it
    assert radar_engine._radar_scan_latency([300, 330, 540]) == 540
    assert radar_engine._radar_scan_latency([300, float('nan'), 330, 'x', 360]) == 360


# ------------------------------------------------------ measuring the latency
def simulate(e, site, latencies, every=60):
    """ List the site every `every` seconds; scan k (taken at T + 300k) first
    appears `latencies[k]` seconds after it was taken. """
    scans = [(T + 300*k, T + 300*k + lat) for k, lat in enumerate(latencies)]
    now, end = T, scans[-1][1] + every
    while now <= end:
        e.radar._note_latency(site, [T - 300] + [ts for ts, seen in scans if seen <= now], now)
        now += every
    return now


def test_listings_measure_first_seen_latency(make_emitter):
    e = make_emitter(scn.all_none())
    e.radar._note_latency('KATX', [T - 300], T - 60)          # the first listing only seeds
    assert e.radar._site_latency('KATX', T) is None
    end = simulate(e, 'KATX', [300, 320, 330])
    # First seen on the next listing (every 60 s): within a minute of the truth.
    assert 330 <= e.radar._site_latency('KATX', end) < 390


def test_a_long_listing_gap_is_not_a_latency_sample(make_emitter):
    e = make_emitter(scn.all_none())
    e.radar._note_latency('KATX', [T - 300], T)
    # A quiet tier listed again 15 minutes later: three new scans, but the gap
    # measures our polling, not IEM.
    e.radar._note_latency('KATX', [T - 300, T, T + 300, T + 600], T + 900)
    assert len(e.radar._latency['KATX']['samples']) == 0
    assert e.radar._site_latency('KATX', T + 900) is None


def test_latency_is_per_site(make_emitter):
    e = make_emitter(scn.all_none())
    a = simulate(e, 'KATX', [120, 130, 140])
    b = simulate(e, 'KRTX', [500, 520, 540])
    assert e.radar._site_latency('KATX', a) < 200 < 500 <= e.radar._site_latency('KRTX', b)


def test_payload_publishes_the_latency_used():
    r = payload(site_snapshot(T, 300, 330), T + 100)
    assert r['scanLatencySec'] == 330
