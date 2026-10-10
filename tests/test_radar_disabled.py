"""WFP_RADAR=0: a kiosk that cannot show radar (tabs off) runs none of it."""
from lib import almanac_emit as ae
from lib import radar_engine


def test_disabled_radar_schedules_nothing_and_says_so(make_emitter, monkeypatch):
    monkeypatch.setattr(radar_engine, 'RADAR_ENABLED', False)
    e = make_emitter()
    scheduled = []
    monkeypatch.setattr(e._runtime, 'schedule', lambda cb, delay, interval=False: scheduled.append(getattr(cb, '__name__', str(cb))) or object())
    e.start()
    assert not any(name in ('_check', '_check_zoom', '_check_geo', '_check_discovery') for name in scheduled), scheduled
    assert e.radar._cache_thread is None                     # no inventory scan thread
    p = e._build_payload()
    assert p['radar'] == dict(available=False, reason='radar off', enabled=False, starting=None, attention=None)
    assert e.radar._write_health(ae.time.time())                  # /health reads radar-health.json
    import json
    from pathlib import Path
    health = json.loads((Path(e.output_path).parent / 'radar-health.json').read_text())
    assert health['enabled'] is False and health['summary']['state'] == 'off'
    assert e.radar._health_payload()['enabled'] is False
    e.stop()


def test_enabled_radar_is_the_default(make_emitter, monkeypatch):
    assert radar_engine.RADAR_ENABLED is True
    e = make_emitter()
    scheduled = []
    monkeypatch.setattr(e._runtime, 'schedule', lambda cb, delay, interval=False: scheduled.append(getattr(cb, '__name__', str(cb))) or object())
    monkeypatch.setattr(e.radar, '_start_inventory', lambda: scheduled.append('inventory'))
    e.start()
    assert 'inventory' in scheduled and any(name in ('_check', '_check_zoom', '_check_geo', '_check_discovery') for name in scheduled)
    e.stop()
