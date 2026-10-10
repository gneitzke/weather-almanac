import os
# The attention tiers change off-tab acquisition by tier and hour of day. The
# existing suite pins the pre-tier behaviour, so it runs the policy in shadow
# (decided and published, never applied); tests of the tiers opt in by
# monkeypatching radar_engine.RADAR_ATTENTION_MODE = 'active'.
os.environ.setdefault('WFP_RADAR_ATTENTION', 'shadow')
""" Test bootstrap for the almanac data pipeline.

The console is a Kivy app, but the two seams we test — the wx.json emitter
(lib/almanac_emit.py) and the observation parser (lib/observation_parser.py) —
touch only a thin slice of Kivy. We stub that slice into sys.modules BEFORE any
lib.* import so the suite runs hermetically on a bare Python (no Kivy install,
no display, no GL, no SDL) and deterministically.

Two stubs are load-bearing, not cosmetic:
  * kivy.clock.mainthread -> identity. Kivy's real mainthread ALWAYS defers via
    Clock.schedule_once, so obsParser.update_display would never run inline and
    the merge-critical button_list branch would never execute in a test. Making
    it identity runs update_display synchronously — that is what lets us assert
    the headless evt_strike invariant. It trades away real-loop scheduling
    fidelity (the *when*), not the code under test (the *what*).
  * kivy.app.App.get_running_app -> None. obsParser.__init__ calls it; parser
    tests bypass __init__ entirely (see make_parser) so this is just insurance.
"""

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


import importlib.abc          # noqa: E402
import importlib.machinery    # noqa: E402


class _AnyMeta(type):
    """ Attribute access on the class yields another universal stand-in, so
    `kivy.uix.modalview.ModalView` etc. resolve to a usable (subclassable,
    callable) type without us enumerating Kivy's widget tree. """
    def __getattr__(cls, name):
        return _Any


class _Any(metaclass=_AnyMeta):
    """ Universal Kivy stand-in: works as a base class, is callable, and every
    attribute is itself. Enough to satisfy import-time class definitions and
    decorators in the non-UI code we actually exercise. """
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, name):
        return _Any

    def __call__(self, *a, **k):
        return _Any


class _Logger:
    def _noop(self, *a, **k):
        pass
    warning = info = debug = error = critical = trace = _noop


class _Handle:
    def cancel(self):
        pass


class _Clock:
    def schedule_interval(self, *a, **k):
        return _Handle()

    def schedule_once(self, *a, **k):
        return _Handle()

    def tick(self, *a, **k):
        pass


class _App:
    @staticmethod
    def get_running_app():
        return None


# Real behavior only where a test depends on it; everything else -> _Any.
_SEED = {
    'kivy.logger':     {'Logger': _Logger()},
    'kivy.clock':      {'Clock': _Clock(), 'mainthread': (lambda fn: fn)},  # identity -> inline
    'kivy.app':        {'App': _App},
    'kivy.properties': {'DictProperty': dict, 'NumericProperty': _Any,
                        'StringProperty': _Any, 'ListProperty': _Any,
                        'BooleanProperty': _Any, 'ObjectProperty': _Any},
}


class _KivyModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith('__') and name.endswith('__'):
            raise AttributeError(name)
        return _Any


class _KivyStubFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """ Fabricates any `kivy` / `kivy.*` module on import. Each is a package
    (so arbitrarily deep submodule imports resolve) and seeded per _SEED. """
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'kivy' or fullname.startswith('kivy.'):
            return importlib.machinery.ModuleSpec(fullname, self, is_package=True)
        return None

    def create_module(self, spec):
        module = _KivyModule(spec.name)
        module.__path__ = []                      # mark as package
        for key, value in _SEED.get(spec.name, {}).items():
            setattr(module, key, value)
        return module

    def exec_module(self, module):
        pass


def _install_kivy_stubs():
    if any(isinstance(f, _KivyStubFinder) for f in sys.meta_path):
        return
    # Only stub if real Kivy is not importable/desired. Tests always want the stub.
    sys.meta_path.insert(0, _KivyStubFinder())


_install_kivy_stubs()


# ---------------------------------------------------------------------------
# Factories (imported after the stubs are live, so lib.* import cleanly)
# ---------------------------------------------------------------------------
import pytest                                             # noqa: E402
from types import SimpleNamespace                         # noqa: E402


@pytest.fixture
def make_emitter(tmp_path, monkeypatch):
    """ Build an AlmanacEmitter over a fake holder. `scenario` is a
    dict(Obs, Met, Astro, Sager) from fixtures.obs_scenarios; `config` defaults
    to fixtures.config.make_config(); extra kwargs set instance attrs (e.g.
    _aqi=42) to stand in for the off-thread network results. """
    from lib.almanac_emit import AlmanacEmitter
    from lib import radar_engine
    monkeypatch.setattr(radar_engine, 'RADAR_DIR', str(tmp_path / 'radar'))
    from tests.fixtures.config import make_config

    def _make(scenario=None, config=None, api_data=None, output_path=None, running=False, **attrs):
        scenario = scenario or {}
        app = SimpleNamespace(
            config=make_config() if config is None else config,
            obsParser=SimpleNamespace(api_data=api_data or {}),
        )
        screen = SimpleNamespace(
            Obs=scenario.get('Obs', {}), Met=scenario.get('Met', {}),
            Astro=scenario.get('Astro', {}), Sager=scenario.get('Sager', {}),
            app=app,
        )
        emitter = AlmanacEmitter(screen, output_path=output_path or str(tmp_path / 'wx.json'), interval=2.0)
        emitter._runtime.running = running
        for key, value in attrs.items():
            setattr(emitter, key, value)
        return emitter

    return _make


@pytest.fixture
def make_parser():
    """ Build an obs_parser WITHOUT running __init__ (which calls
    App.get_running_app()). Seed exactly the instance state __init__ would, from
    the module-level device_obs/derive_obs templates. `app` is a fake providing
    .config and .CurrentConditions. """
    import copy
    from lib import observation_parser as op
    from lib import properties

    def _make(app):
        parser = op.obs_parser.__new__(op.obs_parser)
        parser.display_obs = properties.Obs()
        parser.device_obs = copy.deepcopy(op.device_obs)
        parser.derive_obs = copy.deepcopy(op.derive_obs)
        parser.api_data = {}
        parser.flag_api = [1, 1, 1, 1]
        parser.transmit = 1
        parser.app = app
        return parser

    return _make


@pytest.fixture(autouse=True)
def loopback_only_when_offline(monkeypatch):
    """Offline suites must not accidentally escape through a new fallback path."""
    import os
    import socket
    import ipaddress
    if os.environ.get('RADAR_NET_TEST') != '0':
        return
    resolve, connect = socket.getaddrinfo, socket.socket.connect
    def local(host):
        if host == 'localhost':
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False
    def guarded_resolve(host, *args, **kwargs):
        if not local(host):
            raise socket.gaierror('offline test blocked non-loopback DNS')
        return resolve(host, *args, **kwargs)
    def guarded_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not local(address[0]):
            raise OSError('offline test blocked non-loopback connection')
        return connect(sock, address)
    monkeypatch.setattr(socket, 'getaddrinfo', guarded_resolve)
    monkeypatch.setattr(socket.socket, 'connect', guarded_connect)
