"""Clock handles and daemon-provider admission shared by the two owners.

All lifecycle operations use ``lock`` (the outermost lock in RadarEngine's
documented order). Callers hold it across start/stop of their owned resources.
The runtime knows nothing about radar, weather payloads or provider policy.
"""

from threading import RLock


class ProviderRuntime:
    def __init__(self, clock, thread_factory):
        # Read the current Clock at scheduling time, as the original emitter
        # did. The factory likewise resolves the thread implementation at spawn.
        self.clock = clock
        self.thread_factory = thread_factory
        self.lock = RLock()
        self.events = []
        self.running = False
        self.inflight = set()
        self.retries = {}

    def cancel_all(self):
        """Cancel every timer; caller holds lock and has fenced running work."""
        for handle in self.events:
            try:
                handle.cancel()
            except Exception:  # noqa: BLE001
                pass
        self.events = []
        self.retries.clear()

    def schedule(self, callback, timeout, interval=False):
        """ Schedule through the registry, so stop() reaches every handle. The
        callback is fenced: a stopped instance's timer does nothing and (by
        returning False) unschedules itself, and a fired one-shot leaves the
        registry so a long run cannot accumulate dead handles. """
        handles = []

        def _fenced(dt):
            if not interval and handles:
                try:
                    self.events.remove(handles[0])
                except ValueError:
                    pass
            if not self.running:
                return False
            return callback(dt)

        with self.lock:
            if not self.running:
                return None                      # stopped between the caller's check and here
            clock = self.clock()
            handle = (clock.schedule_interval if interval else clock.schedule_once)(_fenced, timeout)
            handles.append(handle)
            self.events.append(handle)
            return handle

    def spawn(self, key, worker, on_complete=None):
        """ Run a provider fetch on a daemon thread, at most ONE per provider: a
        slow or hung request must not stack a second behind it, and the poll
        interval must not overtake a retry that is already running. """
        with self.lock:
            if not self.running or key in self.inflight:
                return
            self.inflight.add(key)

        def _run():
            try:
                worker()
            finally:
                with self.lock:
                    self.inflight.discard(key)
                    if on_complete is not None:
                        on_complete()

        try:
            self.thread_factory(target=_run, daemon=True).start()
        except Exception:                                                 # noqa: BLE001
            self.inflight.discard(key)

    def schedule_retry(self, key, callback, timeout):
        """ Arm the ONE pending retry a provider is allowed. Without this, every
        failure of a periodic poll starts its own retry chain and the chains
        multiply for as long as the network is down. """
        def _retry(dt):
            with self.lock:
                if self.retries.get(key) is not handle:
                    return  # a cancelled/replaced callback cannot consume its successor
                self.retries.pop(key, None)
            callback(dt)

        with self.lock:
            if not self.running or self.retries.get(key) is not None:
                return
            handle = self.schedule(_retry, timeout)
            if handle is not None:
                self.retries[key] = handle
