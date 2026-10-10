"""Offline outage log volume: 1,800 real orchestration passes, fake time/transport.

Run with PYTHONPATH=. ./venv-test/bin/python tools/benchmark_radar_logging.py
Use --revision HEAD to execute an unmodified git revision of the emitter locally.
Counts UTF-8 logger messages plus one newline; excludes logger-specific prefixes.
"""
import argparse
import importlib.util
import json
import socket
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests import conftest  # noqa: F401; headless Kivy stubs
from tests.fixtures.config import make_config
from lib import almanac_emit, radar_engine


def simulate(ae, mode, passes=1800):
    # --revision can load the pre-extraction emitter. Keep that compatibility
    # confined to this benchmark; current callers target the engine directly.
    radar_module = radar_engine if hasattr(ae, 'RadarEngine') else ae
    clock = SimpleNamespace(now=1800000000., mono=1000.)
    stats = dict(passes=passes, lines=0, bytes=0, passLines=0, maxPassBytes=0, requests=0)

    def log(message, *args):
        message = message % args if args else message
        size = len((message+'\n').encode('utf-8'))
        stats['lines'] += 1
        stats['bytes'] += size
        if message.startswith('almanac_emit: radar pass '):
            stats['passLines'] += 1
            stats['maxPassBytes'] = max(stats['maxPassBytes'], size)

    def unreachable(session, req, **kw):
        stats['requests'] += 1
        if mode == 'Site':
            raise socket.gaierror(-2, 'Name or service not known')
        raise radar_module.LocalTransportError('radar connection/TLS setup timed out')

    def forbidden(*a, **kw):
        raise AssertionError('simulation must not use network or sleep')

    with tempfile.TemporaryDirectory(prefix='radar-log-') as root:
        app = SimpleNamespace(config=make_config(), obsParser=SimpleNamespace(api_data={}))
        e = ae.AlmanacEmitter(SimpleNamespace(app=app, Obs={}, Met={}, Astro={}, Sager={}),
                              output_path=str(Path(root)/'wx.json'))
        radar = getattr(e, 'radar', e)
        def member(name):
            return '_radar' + name if radar is e else name
        getattr(radar, member('_cache_ready')).set()
        if radar is e:
            e._running = True
        else:
            e._runtime.running = True
        acquire = getattr(radar, '_do_radar' if radar is e else '_acquire')
        with patch.object(ae.time, 'time', lambda: clock.now), \
             patch.object(ae.time, 'monotonic', lambda: clock.mono), \
             patch.object(ae.time, 'process_time', lambda: 0.), \
             patch.object(ae.time, 'thread_time', lambda: 0.), \
             patch.object(ae.time, 'sleep', forbidden), \
             patch.object(socket, 'getaddrinfo', forbidden), \
             patch.object(socket.socket, 'connect', forbidden), \
             patch.object(radar_module.RadarSession, 'open', unreachable), \
             patch.object(radar, member('_start_inventory'), lambda: None), \
             patch.object(radar, member('_auto_source'),  # Auto's verdict, pinned per mode
                          lambda ctx, site_ok: 'site' if mode == 'Site' else 'mosaic'), \
             patch.object(ae.Logger, 'info', log), \
             patch.object(ae.Logger, 'warning', log):
            for _ in range(passes):
                acquire(intent_triggered=False)
                clock.now += 2
                clock.mono += 2
        session = getattr(radar, member('_session'))
        if session is not None:
            session.close()
        stats['requestHistory'] = len(getattr(radar, member('_request_metrics')))
        stats['phaseHistory'] = len(getattr(radar, member('_phase_metrics')))
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision')
    args = parser.parse_args()
    ae = almanac_emit
    if args.revision:
        code = subprocess.check_output(['git', 'show', args.revision+':lib/almanac_emit.py'], text=True)
        spec = importlib.util.spec_from_loader('lib.radar_logging_baseline', loader=None)
        ae = importlib.util.module_from_spec(spec)
        ae.__file__ = almanac_emit.__file__
        exec(compile(code, ae.__file__, 'exec'), ae.__dict__)
    print(json.dumps({mode: simulate(ae, mode) for mode in ('Region', 'Site')}, indent=2))


if __name__ == '__main__':
    main()
