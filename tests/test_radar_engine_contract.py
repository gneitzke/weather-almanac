"""The emitter sees the radar boundary, never acquisition/cache internals."""

import json
from pathlib import Path

from lib import almanac_emit as ae, radar_engine
from tests.fixtures.config import make_config
from tests.test_emitter_lifecycle import FakeClock


def test_emitter_uses_only_public_boundary_and_live_input_readers(make_emitter, monkeypatch):
    calls, inputs = [], {}

    class BoundaryOnly:
        # No dynamic attributes or fallback delegation: any private reach-through
        # by AlmanacEmitter fails this contract.
        __slots__ = ()

        def __init__(self, output_path, *, runtime, config, forecast_updated,
                     refresh_alerts, logger):
            inputs.update(output_path=output_path, runtime=runtime, config=config,
                          forecast_updated=forecast_updated, refresh_alerts=refresh_alerts,
                          logger=logger)

        def start(self):
            calls.append('start')

        def stop(self):
            calls.append('stop')

        def before_emit(self):
            calls.append('before_emit')

        def payload_snapshot(self, now, tz, style):
            calls.append('snapshot')
            assert style == '24 hr' and now > 0 and tz is not None
            return dict(available=True, contract='radar snapshot')

        def tick(self, payload, now, tz):
            calls.append('tick')
            assert {'obsAgeSec', 'rainRateMm', 'conditions', 'fcPrecipPct'} <= payload.keys()
            payload['radar']['attention'] = dict(tier='watch')

        def write_health(self, now):
            calls.append('health')

    monkeypatch.setattr(ae, 'RadarEngine', BoundaryOnly)
    monkeypatch.setattr(ae, 'Clock', FakeClock())
    emitter = make_emitter()
    assert inputs['runtime'] is emitter._runtime
    assert inputs['output_path'] == emitter.output_path
    assert inputs['logger'] is ae.Logger
    assert inputs['config']() is emitter.app.config
    emitter.app.config = make_config(Station={'Latitude': '0'})
    assert inputs['config']() is emitter.app.config
    emitter.screen.Met = {'UpdatedTs': 1234}
    assert inputs['forecast_updated']() == 1234
    monkeypatch.setattr(emitter, '_check_alerts', lambda: calls.append('alerts'))
    inputs['refresh_alerts']()
    assert calls.pop() == 'alerts'

    emitter.start()
    emitter._emit(0)
    emitter.stop()
    assert calls == ['stop', 'start', 'before_emit', 'snapshot', 'tick', 'health', 'stop']
    payload = json.loads(Path(emitter.output_path).read_text())
    assert payload['radar'] == dict(available=True, contract='radar snapshot', attention=dict(tier='watch'))
    assert not emitter._runtime.events and not emitter._runtime.running
    assert not any(name.startswith(('_radar_', '_warnings')) for name in vars(emitter))


def test_engine_owns_original_cadence_and_health_file(make_emitter, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(ae, 'Clock', clock)
    emitter = make_emitter()
    engine = emitter.radar
    assert not hasattr(engine, 'app') and not hasattr(engine, 'screen')
    assert engine._runtime is emitter._runtime
    scheduled = []
    schedule = emitter._runtime.schedule

    def record(callback, delay, interval=False):
        if getattr(callback, '__self__', None) is engine:
            scheduled.append((callback.__name__, delay, interval))
        return schedule(callback, delay, interval)

    monkeypatch.setattr(emitter._runtime, 'schedule', record)
    monkeypatch.setattr(engine, '_start_inventory', lambda: None)
    emitter.start()
    assert scheduled == [('_check', 60, False),
                         ('_check_zoom', radar_engine.RADAR_INTENT_CHECK_SEC, True),
                         ('_check_geo', radar_engine.RADAR_GEO_QUANTUM_SEC, True),
                         ('_check_warnings', 45, False),
                         ('_check_warnings', radar_engine.nws_warnings.TICK_SEC, True)]
    assert len(clock.events) == 14
    fenced = [event.callback for event in clock.events]
    emitter._emit(0)
    health = json.loads(Path(emitter.output_path).with_name('radar-health.json').read_text())
    assert health['summary']['state'] == 'starting'
    assert health['enabled'] == engine.health_snapshot()['enabled']
    assert 'health' not in json.loads(Path(emitter.output_path).read_text())['radar']
    emitter.stop()
    assert clock.events == []
    assert all(callback(0) is False for callback in fenced)
    assert engine._input_pool._shutdown and engine._hca_pool._shutdown
