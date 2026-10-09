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
from lib import almanac_emit


def simulate(ae, mode, passes=1800):
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
        raise ae.LocalTransportError('radar connection/TLS setup timed out')

    def forbidden(*a, **kw):
        raise AssertionError('simulation must not use network or sleep')

    with tempfile.TemporaryDirectory(prefix='radar-log-') as root:
        app = SimpleNamespace(config=make_config(), obsParser=SimpleNamespace(api_data={}))
        e = ae.AlmanacEmitter(SimpleNamespace(app=app, Obs={}, Met={}, Astro={}, Sager={}),
                              output_path=str(Path(root)/'wx.json'))
        e._radar_cache_ready.set()
        e._running = True
        with patch.object(ae.time, 'time', lambda: clock.now), \
             patch.object(ae.time, 'monotonic', lambda: clock.mono), \
             patch.object(ae.time, 'process_time', lambda: 0.), \
             patch.object(ae.time, 'thread_time', lambda: 0.), \
             patch.object(ae.time, 'sleep', forbidden), \
             patch.object(socket, 'getaddrinfo', forbidden), \
             patch.object(socket.socket, 'connect', forbidden), \
             patch.object(ae.RadarSession, 'open', unreachable), \
             patch.object(e, '_radar_start_inventory', lambda: None), \
             patch.object(e, '_radar_auto_source',  # Auto's verdict, pinned per mode
                          lambda ctx, site_ok: 'site' if mode == 'Site' else 'mosaic'), \
             patch.object(ae.Logger, 'info', log), \
             patch.object(ae.Logger, 'warning', log):
            for _ in range(passes):
                e._do_radar(intent_triggered=False)
                clock.now += 2
                clock.mono += 2
        if e._radar_session is not None:
            e._radar_session.close()
        stats['requestHistory'] = len(e._radar_request_metrics)
        stats['phaseHistory'] = len(e._radar_phase_metrics)
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
