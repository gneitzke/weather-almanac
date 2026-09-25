"""Native Level III policy and restart-safe UTC body-byte ledger."""
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

NATIVE_NEWEST_ONLY_BYTES = 150_000_000
NATIVE_PAUSE_BYTES = 250_000_000


def native_allowed(requested, tier, ceiling_state):
    return requested and tier in ('live', 'warm', 'watch') and ceiling_state != 'paused'


class NativeBudget:
    """Short in-memory accounting lock; one asynchronous durable writer.

    Thresholds and UTC rollovers wake the writer immediately. Other changes
    coalesce to at most one write per two seconds, including the trailing burst.
    With healthy storage, a hard stop loses at most two seconds of counts plus
    an in-progress filesystem flush; no exit handler or later request is needed.
    Failed writes retry with bounded backoff. Native remains memory-metered under
    the same byte ceilings; ledger health is independent of the daily limit.
    """
    def __init__(self, path, clock=time.time, monotonic=time.monotonic):
        self.path, self.clock, self.monotonic = Path(path), clock, monotonic
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.writer = None
        self.requested = self.processed = 0
        self.day, self.bytes = '', 0
        self.clock_day = None
        self.failed = False
        self.retry_at = 0.
        self.write_failures = 0
        self.saved = None
        self.last_write = None
        try:
            record = json.loads(self.path.read_text())
            if (isinstance(record['day'], str) and
                    datetime.strptime(record['day'], '%Y-%m-%d').strftime('%Y-%m-%d') == record['day'] and
                    type(record['bytes']) is int and record['bytes'] >= 0):
                self.day, self.bytes = record['day'], record['bytes']
                self.saved = (self.day, self.bytes, self._state())
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _state(self):
        return ('paused' if self.bytes > NATIVE_PAUSE_BYTES else
                'newest-only' if self.bytes > NATIVE_NEWEST_ONLY_BYTES else 'normal')

    def _rollover(self):
        now = self.clock()
        clock_day = int(now // 86400)
        if clock_day == self.clock_day:
            return
        today = datetime.fromtimestamp(now, timezone.utc).date()
        day, tomorrow = today.isoformat(), (today + timedelta(days=1)).isoformat()
        if day > self.day or self.day > tomorrow:
            self.day, self.bytes = day, 0
        self.clock_day = clock_day

    def snapshot(self):
        with self.lock:
            self._rollover()
            result = dict(day=self.day, bytesToday=self.bytes, ceilingState=self._state(),
                          ledgerState='retrying' if self.failed else 'ok')
        self.persist(wait=False)
        return result

    def add(self, count):
        if count <= 0:
            return
        with self.lock:
            self._rollover()
            self.bytes += count
        self.persist(wait=False)

    def persist(self, wait=True):
        """Wake the single writer; optionally await its admission/write verdict.

        Request workers and the watcher use wait=False. Waiting is useful for
        explicit durability checks, never required for trailing-burst flushes.
        A wait does not override the ordinary coalescing or failure backoff.
        """
        with self.condition:
            self._rollover()
            if (self.day, self.bytes, self._state()) == self.saved and not self.failed:
                return
            self.requested += 1
            ticket = self.requested
            if self.writer is None:
                self.writer = threading.Thread(target=self._writer, name='radar-ledger', daemon=True)
                self.writer.start()
            self.condition.notify_all()
            if wait:
                self.condition.wait_for(lambda: self.processed >= ticket)

    def flush(self, timeout=5):
        """Optional caller barrier; normal durability does not depend on it."""
        with self.condition:
            self._rollover()
            target_day, target_bytes = self.day, self.bytes
            self.persist(wait=False)
            return self.condition.wait_for(lambda: self.failed or self.saved is not None and
                (self.saved[0] > target_day or self.saved[0] == target_day and
                 self.saved[1] >= target_bytes), timeout) and not self.failed

    def _writer(self):
        while True:
            with self.condition:
                self._rollover()
                current = (self.day, self.bytes, self._state())
                ticket = self.requested
                urgent = self.saved is None or current[0] != self.saved[0] or current[2] != self.saved[2]
                delay = (max(0, self.retry_at-self.monotonic()) if self.failed else
                         0 if urgent or self.last_write is None else
                         max(0, self.last_write+2-self.monotonic()))
                if current == self.saved and not self.failed:
                    self.processed = ticket
                    self.writer = None
                    self.condition.notify_all()
                    return
                if delay:
                    self.processed = ticket
                    self.condition.notify_all()
                    self.condition.wait(timeout=delay)
                    continue
            # Only this thread touches storage, outside every accounting/renderer
            # lock. A request that arrives during I/O wakes the next iteration.
            temporary = None
            started = self.monotonic()
            try:
                target = self.path.resolve()
                temporary = target.with_name(target.name+'.tmp')
                with temporary.open('w') as stream:
                    json.dump(dict(day=current[0], bytes=current[1]), stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
                directory = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                with self.condition:
                    self.failed = False
                    self.write_failures = 0
                    self.saved = current
                    self.last_write = started
            except Exception as error:
                with self.condition:
                    first_failure = not self.failed
                    self.failed = True
                    self.write_failures += 1
                    self.retry_at = self.monotonic() + min(300, 5 * 2**min(self.write_failures-1, 6))
                if first_failure:
                    logging.getLogger(__name__).warning('Native radar ledger unavailable; retrying, bytes counted in memory: %s', error)
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
                with self.condition:
                    self.processed = ticket
                    self.condition.notify_all()
