"""Tile races and host health, independent of the display and immutable caches."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import socket
import threading
import time
from urllib.parse import urlsplit
from lib.radar_http import LocalTransportError, failure_class


class CircuitOpen(OSError):
    pass


class AttemptCancelled(OSError):
    pass


def hedge_now():
    """Separate progress clock; transport and batch keep their absolute deadline."""
    return time.monotonic()


class Attempt:
    def __init__(self, fresh=False, hedged=False, warm_lease=None):
        self.warm_lease = warm_lease
        self.fresh = fresh
        self.hedged = hedged
        self.issued = False
        self.waiting_response = False
        self.discarded = False
        self.first_byte = threading.Event()
        self.last_progress = hedge_now()
        self.stall_hedge = False
        self.cancelled = threading.Event()
        self.sock = None
        self.lock = threading.Lock()

    def progress(self):
        with self.lock:
            self.last_progress = hedge_now()
            self.first_byte.set()

    def check(self):
        if self.cancelled.is_set():
            raise AttemptCancelled('discarded radar attempt')

    def attach(self, sock):
        with self.lock:
            self.check()
            self.sock = sock

    def cancel(self, discarded=True):
        if self.warm_lease is not None:
            self.warm_lease.close()
        with self.lock:
            self.discarded = discarded
            self.cancelled.set()
            if self.sock is not None:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


class HostHealth:
    WINDOW = 60
    COOLDOWN = 30

    def __init__(self):
        self.lock = threading.RLock()
        self.hosts = {}
        self.sources = {}
        self.hedges = self.retries = self.discarded = 0
        self.stall_hedges = 0
        self.hedge_events = deque()
        self.hedge_until = 0
        self.local_failures = 0
        self.ambiguous_failures = 0
        self.last_success = None
        self.last_error = None

    def hedge_allowed(self):
        with self.lock:
            now = time.monotonic()
            while self.hedge_events and now-self.hedge_events[0][0] >= self.WINDOW:
                self.hedge_events.popleft()
            issued = sum(kind == 'issued' for _, kind in self.hedge_events)
            lost = sum(kind == 'discarded' for _, kind in self.hedge_events)
            if now >= self.hedge_until and issued and lost / issued > .5:
                self.hedge_until = now + 300
                self.hedge_events.clear()
            return now >= self.hedge_until

    def issue_hedge(self, stall=False):
        with self.lock:
            self.hedges += 1
            self.stall_hedges += int(stall)
            self.hedge_events.append((time.monotonic(), 'issued'))

    def discard_hedges(self, count):
        with self.lock:
            self.discarded += count
            self.hedge_events.extend((time.monotonic(), 'discarded') for _ in range(count))
            self.hedge_allowed()

    def _host(self, source, url):
        host = urlsplit(url).netloc
        self.sources.setdefault(source, set()).add(host)
        state = self.hosts.setdefault(host, dict(samples=deque(), until=0, probe=False,
                                                url=url, metadata=False, local=0, ambiguous=0))
        self._prune(state)
        return state

    def _prune(self, state):
        now = time.monotonic()
        while state['samples'] and now-state['samples'][0][0] >= self.WINDOW:
            state['samples'].popleft()

    def state(self, s):
        if s['probe']:
            return 'half'
        return 'open' if s['until'] else 'closed'

    def admit(self, source, url, metadata=False):
        with self.lock:
            s = self._host(source, url)
            if metadata:
                s.update(url=url, metadata=True)
            if s['until']:
                if time.monotonic() < s['until'] or s['probe'] or not metadata:
                    raise CircuitOpen('radar host circuit open: '+urlsplit(url).netloc)
                s['probe'] = True
                return True
            return False

    def record(self, source, url, success, error=None, probe=False):
        with self.lock:
            if isinstance(error, AttemptCancelled):
                return
            s = self._host(source, url)
            if error is not None:
                self.last_error = str(error) or type(error).__name__
            if error is not None and failure_class(error) != 'host':
                if failure_class(error) == 'local':
                    self.local_failures += 1
                    s['local'] += 1
                else:
                    self.ambiguous_failures += 1
                    s['ambiguous'] += 1
                if probe:
                    s['probe'] = False
                return
            if probe:
                s['probe'] = False
                s['until'] = 0 if success else time.monotonic()+self.COOLDOWN
                s['samples'].clear()
            s['samples'].append((time.monotonic(), bool(success)))
            if not s['until'] and len(s['samples']) >= 6:
                if sum(ok for _, ok in s['samples']) / len(s['samples']) < .5:
                    s['until'] = time.monotonic()+self.COOLDOWN

    def failure_counts(self, *sources):
        """Local and ambiguous failures on these sources' hosts, counted once."""
        with self.lock:
            hosts = {h for source in sources for h in self.sources.get(source, ())}
            states = [self.hosts[h] for h in hosts]
            return {kind: sum(s[kind] for s in states) for kind in ('local', 'ambiguous')}

    def probes(self, source):
        with self.lock:
            states = [self.hosts[h] for h in self.sources.get(source, ())]
            if any(s['until'] and (s['probe'] or time.monotonic() < s['until']) for s in states):
                raise CircuitOpen('radar source host circuit open: '+source)
            return [(s['url'], s['metadata']) for s in states if s['until']]

    def probe_delay(self, sources=None):
        with self.lock:
            hosts = self.hosts if sources is None else {
                h: self.hosts[h] for source in sources for h in self.sources.get(source, ())}
            delays = [max(0, s['until']-time.monotonic()) for s in hosts.values() if s['until']]
            return min(delays) if delays else None

    def snapshot(self):
        with self.lock:
            hosts = {}
            samples = []
            for host, s in self.hosts.items():
                self._prune(s)
                values = [ok for _, ok in s['samples']]
                samples.extend(values)
                hosts[host] = dict(breaker=self.state(s), samples60s=len(values),
                    successRate60s=sum(values)/len(values) if values else None)
            states = {h['breaker'] for h in hosts.values()}
            return dict(lastSuccessTs=self.last_success,
                successRate60s=sum(samples)/len(samples) if samples else None,
                hedges=self.hedges, stallHedges=self.stall_hedges, retries=self.retries, discardedHedges=self.discarded,
                breaker='open' if 'open' in states else 'half' if 'half' in states else 'closed',
                lastError=self.last_error, localFailures=self.local_failures, ambiguousFailures=self.ambiguous_failures,
                hedgeSuspendedSec=max(0, self.hedge_until-time.monotonic()), hosts=hosts)


def tile_race(request, deadline, hedge, claim_hedge, discarded):
    """At most two attempts; winning bytes alone leave this function."""
    controls = [Attempt()]
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='radar-attempt')
    pending = {pool.submit(request, controls[0], False): controls[0]}
    started = hedge_now()
    next_hedge = started + hedge if hedge is not None else None
    second = False
    error = None
    winner = None
    try:
        while pending:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('radar tile race exceeded deadline')
            due = next_hedge-hedge_now() if hedge is not None and not second else remaining
            done, _ = wait(pending, timeout=max(0, min(remaining, due)), return_when=FIRST_COMPLETED)
            for future in done:
                control = pending.pop(future)
                try:
                    result = future.result()
                    winner = control
                    return result
                except Exception as caught:
                    error = caught
            if not second:
                failed = not pending
                now = hedge_now()
                eligible = (hedge is not None and now >= next_hedge
                            and now-controls[0].last_progress >= hedge)
                lease = claim_hedge() if eligible and not failed else None
                if (failed and error is not None and isinstance(error, OSError) and not isinstance(error, (CircuitOpen, AttemptCancelled))) or lease:
                    second = True
                    control = Attempt(fresh=failed, hedged=not failed, warm_lease=lease)
                    control.stall_hedge = not failed and controls[0].first_byte.is_set()
                    controls.append(control)
                    pending[pool.submit(request, control, True)] = control
                elif hedge is not None:
                    # Every received chunk resets inactivity, including headers
                    # and partial bodies. Busy warm leases remain retryable.
                    next_hedge = max(now + .05, controls[0].last_progress + hedge)
        raise error
    finally:
        for control in controls:
            control.cancel(discarded=winner is not None)
        pool.shutdown(wait=True)
        # Admission can race the primary's completion. Count only after every
        # attempt drains, so a late admitted hedge cannot escape loss accounting.
        discarded(sum(c.hedged and c.issued and c is not winner for c in controls))
