"""A booting engine has no radar result yet. That is a 'starting' state the page
can show, not 'no radar here' (2026-09-16: for a minute after every engine
restart the Radar tab vanished and a user on the radar screen was bounced)."""
from lib import almanac_emit as ae
from lib import radar_engine


def test_starting_while_the_cache_scan_runs_then_while_acquiring(make_emitter):
    e = make_emitter()
    e.radar._cache_ready.clear()
    s = e._build_payload()['radar']['starting']
    assert s['phase'] == 'cache' and s['cacheFiles'] == 0 and s['sinceSec'] >= 0
    e.radar._cache_ready.set()
    assert e._build_payload()['radar']['starting']['phase'] == 'acquire'


def test_starting_ends_with_a_result_a_conclusive_failure_or_time(make_emitter):
    e = make_emitter()
    e.radar._result = radar_engine._RADAR_NONE._replace(reason='no radar tiles')
    assert e._build_payload()['radar']['starting'] is None
    e.radar._result = radar_engine._RADAR_NONE._replace(available=True, reason=None)
    assert e._build_payload()['radar']['starting'] is None
    e.radar._result = radar_engine._RADAR_NONE
    e.radar._boot_mono -= radar_engine.RADAR_STARTING_MAX_SEC + 1
    assert e._build_payload()['radar']['starting'] is None
