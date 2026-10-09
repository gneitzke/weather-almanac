"""Last-listing evidence reaches Region independently of retained tile snapshots."""
import pytest

from lib import almanac_emit as ae
from tests.test_radar_hybrid import hybrid  # noqa: F401
from tests.test_radar_v3 import multisite  # noqa: F401
from tests.test_radar_v50 import scheduled  # noqa: F401


@pytest.mark.parametrize('status', ['empty', 'old', 'fresh', 'failed'])
def test_region_publishes_last_listing_on_discovery(make_emitter, hybrid, multisite, tmp_path, monkeypatch, status):
    hybrid.pin('mosaic')
    e = make_emitter()
    e._do_radar()
    before = e._radar_result
    unknown = e._build_payload()['radar']['nexrad']
    assert unknown['reporting'] is unknown['checkedTs'] is unknown['ageSec'] is None
    multisite.scans['KNEA'] = [] if status == 'empty' else [hybrid.now-(1620 if status == 'old' else 360)]
    if status == 'failed':
        original = ae.RadarSession.open
        def failed(session, request, *a, **kw):
            if 'operation=list' in request.full_url:
                raise ConnectionResetError('listing unavailable')
            return original(session, request, *a, **kw)
        monkeypatch.setattr(ae.RadarSession, 'open', failed)
    e._radar_discovery.started(hybrid.now)
    e._do_radar(intent_triggered=False, discovery=True)
    r = e._build_payload()['radar']
    n = r['nexrad']
    assert r['sourceMode'] == 'mosaic' and e._radar_result.frames == before.frames
    assert n['id'] == 'KNEA' and n['checkedTs'] == hybrid.now
    assert n['nextCheckTs'] == e._radar_discovery.due
    assert n['checkedAt'] and n['nextCheckAt']
    assert n['reporting'] is (None if status == 'failed' else status == 'fresh')
    assert n['reason'] == ('scan unavailable' if status == 'failed' else None if status == 'fresh' else 'not reporting')
    assert n['ageSec'] == (1620 if status == 'old' else 360 if status == 'fresh' else None)
    if status != 'failed':
        assert multisite.calls == [('list', 'KNEA')]
    # Heartbeats age the observation, never repeat discovery or move checkedTs.
    hybrid.mono += 60
    aged = e._build_payload()['radar']['nexrad']
    assert aged['checkedTs'] == n['checkedTs']
    if n['newestTs'] is not None:
        assert aged['ageSec'] == n['ageSec']+60


def test_region_site_checks_obey_cadence_and_reserve(scheduled):
    e, clock, h = scheduled
    def listings():
        return [c for c in h.calls if c[2].startswith(ae.RADAR_SITE_LIST_URL)]
    assert not listings()
    clock.advance(120)
    assert len(listings()) == 1
    checked = e._build_payload()['radar']['nexrad']['checkedTs']
    for _ in range(5):
        e._build_payload()
    assert len(listings()) == 1
    clock.advance(20)
    assert len(listings()) == 2
    assert e._build_payload()['radar']['nexrad']['checkedTs'] == checked+20
    # Closest-site knowledge yields to the existing interaction reserve.
    e._radar_request_times = [clock.now] * (ae.RADAR_REQUESTS_PER_MIN-ae.RADAR_HISTORY_RESERVE)
    clock.advance(20)
    assert len(listings()) == 2
    assert e._build_payload()['radar']['nexrad']['checkedTs'] == checked+20
    e.stop()


def test_cached_history_expiry_cannot_invent_an_empty_listing(make_emitter, hybrid, multisite, tmp_path):
    hybrid.pin('mosaic')
    e = make_emitter()
    e._do_radar()
    multisite.scans['KNEA'] = [hybrid.now-4440]  # real scan within the 75-minute listing
    e._do_radar(intent_triggered=False, discovery=True)
    before = e._build_payload()['radar']['nexrad']
    assert before['newestTs'] is not None
    hybrid.mono += 120  # now outside acquisition's history horizon, within cache TTL
    multisite.calls.clear()
    _, scans, reused = e._radar_site_listing(dict(intent_triggered=True, deadline=hybrid.mono+10), dict(id='KNEA'))
    assert reused and not scans and not multisite.calls
    after = e._build_payload()['radar']['nexrad']
    assert after['checkedTs'] == before['checkedTs']
    assert after['newestTs'] == before['newestTs']  # never relabel this as an empty listing
    assert after['ageSec'] == before['ageSec']+120
