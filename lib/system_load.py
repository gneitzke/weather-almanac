"""Whole-machine load, for the page's motion budget.

The page cannot see the machine it runs on; the engine can. Every emit it
samples the CPU busy share across all cores, the SoC temperature and the
Raspberry Pi's under-voltage alarm, and publishes them as `perf`. The panel's
motion governor (console_live.html) steps the rain's frame rate down when the
machine has no headroom and back up when it does. A LAN browser ignores these
numbers: they describe the Pi, not the screen that browser draws on.

Measured on the Pi 4 panel (2026-10-26): every frame the page draws costs
about 37 ms of CPU however little changed, so the rain's 60 fps keyframes take
Chromium from 25 % to about 200 % of one core. Frame rate is the lever; the
engine only reports, the page decides.

  cpu              busy share of ALL cores since the previous sample, 0..1
                   (user+nice+system+irq+softirq+steal over the same plus idle
                   and iowait; guest time is already inside user and nice;
                   each field's change floored at zero). None on the first
                   sample and off Linux.
  cores            os.cpu_count()
  tempC            SoC temperature, thermal_zone0, 0.1 °C; None when absent
  undervolt        the Pi firmware's under-voltage flag right now (the
                   rpi_volt hwmon alarm); None off a Pi
  undervoltAgeSec  whole seconds since the alarm was last seen raised during
                   this run (0 while raised); None if never seen

Nothing here raises: a missing or malformed file reads as None.
"""
import os
import time

PROC_STAT = '/proc/stat'
THERMAL   = '/sys/class/thermal/thermal_zone0/temp'
HWMON     = '/sys/class/hwmon'
VOLT_HWMON_NAME = 'rpi_volt'          # drivers/hwmon/raspberrypi-hwmon.c
VOLT_ALARM      = 'in0_lcrit_alarm'   # 1 while the firmware reports under-voltage


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def _cpu_fields(text):
    """user nice system idle iowait irq softirq steal from the aggregate
    `cpu` line of /proc/stat (guest and guest_nice are already inside user
    and nice), or None."""
    if not text:
        return None
    line = text.split('\n', 1)[0].split()
    if len(line) < 9 or line[0] != 'cpu':
        return None
    try:
        return [int(v) for v in line[1:9]]
    except ValueError:
        return None


def _find_volt_alarm(hwmon):
    """Path of the Pi's under-voltage alarm file, or None off a Pi."""
    try:
        names = sorted(os.listdir(hwmon))
    except OSError:
        return None
    for name in names:
        base = os.path.join(hwmon, name)
        if (_read(os.path.join(base, 'name')) or '').strip() == VOLT_HWMON_NAME:
            alarm = os.path.join(base, VOLT_ALARM)
            if os.path.exists(alarm):
                return alarm
    return None


class SystemLoad:
    """Samples the machine once per emit. Not thread-safe: one caller, the
    emitter's clock, owns it."""

    def __init__(self, proc_stat=PROC_STAT, thermal=THERMAL, hwmon=HWMON, clock=time.monotonic):
        self._proc_stat = proc_stat
        self._thermal = thermal
        self._clock = clock
        self._alarm = _find_volt_alarm(hwmon)
        self._prev = None            # (busy, total) jiffies at the previous sample
        self._undervolt_at = None    # clock() when the alarm was last seen raised

    def sample(self):
        now = self._clock()
        undervolt = self._undervolt(now)
        return {
            'cpu': self._cpu(),
            'cores': os.cpu_count(),
            'tempC': self._temp(),
            'undervolt': undervolt,
            'undervoltAgeSec': (None if self._undervolt_at is None
                                else int(max(0.0, now - self._undervolt_at))),
        }

    def _cpu(self):
        cur = _cpu_fields(_read(self._proc_stat))
        prev, self._prev = self._prev, cur
        if cur is None or prev is None:
            return None
        # Per field, never below zero: Linux's iowait can step backwards, and a
        # negative idle share would shrink the total and fake a busy machine.
        delta = [max(0, c - p) for c, p in zip(cur, prev)]
        total = sum(delta)
        if total <= 0:               # no tick elapsed, or the counters reset
            return None
        return round((total - delta[3] - delta[4]) / total, 3)

    def _temp(self):
        text = _read(self._thermal)
        try:
            return round(int(text.strip()) / 1000.0, 1)
        except (AttributeError, ValueError):
            return None

    def _undervolt(self, now):
        if self._alarm is None:
            return None
        text = (_read(self._alarm) or '').strip()
        if text not in ('0', '1'):
            return None
        if text == '1':
            self._undervolt_at = now
        return text == '1'
