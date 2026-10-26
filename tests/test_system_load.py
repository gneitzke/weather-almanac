"""lib/system_load.py: the machine's headroom, published as wx.json `perf`.

The panel's motion governor trusts these numbers to set the rain's frame
rate, so each one must mean exactly what it says, and anything missing or
malformed must read as None instead of raising inside the emit loop.
"""
from lib.system_load import SystemLoad

#               user nice system idle iowait irq softirq steal guest guest_nice
STAT_A = 'cpu  1000 0 500 8000 500 0 0 0 300 0\ncpu0 250 0 125 2000 125 0 0 0 75 0\n'
STAT_B = 'cpu  1600 0 700 8600 600 0 0 0 900 0\ncpu0 400 0 175 2150 150 0 0 0 225 0\n'


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def make(tmp_path, stat=STAT_A, temp='61234\n', volt=None, clock=None):
    (tmp_path / 'stat').write_text(stat)
    if temp is not None:
        (tmp_path / 'temp').write_text(temp)
    hwmon = tmp_path / 'hwmon'
    (hwmon / 'hwmon0').mkdir(parents=True)
    (hwmon / 'hwmon0' / 'name').write_text('cpu_thermal\n')
    if volt is not None:
        (hwmon / 'hwmon1').mkdir()
        (hwmon / 'hwmon1' / 'name').write_text('rpi_volt\n')
        (hwmon / 'hwmon1' / 'in0_lcrit_alarm').write_text(volt)
    return SystemLoad(proc_stat=str(tmp_path / 'stat'), thermal=str(tmp_path / 'temp'),
                      hwmon=str(hwmon), clock=clock or Clock())


def test_cpu_is_the_busy_share_of_all_cores_between_samples(tmp_path):
    load = make(tmp_path)
    assert load.sample()['cpu'] is None              # one sample is not a rate
    (tmp_path / 'stat').write_text(STAT_B)
    # busy +600 user +200 system = 800; idle +600 +100 iowait = 700; guest is
    # already inside user, so counting it again would overstate the load
    assert load.sample()['cpu'] == round(800 / 1500, 3)


def test_no_elapsed_tick_or_a_counter_reset_is_unknown_not_zero(tmp_path):
    load = make(tmp_path)
    load.sample()
    assert load.sample()['cpu'] is None              # identical counters: no time passed
    (tmp_path / 'stat').write_text('cpu  10 0 5 80 5 0 0 0 0 0\n')
    assert load.sample()['cpu'] is None              # counters went backwards


def test_iowait_stepping_backwards_cannot_fake_a_busy_machine(tmp_path):
    # Linux's iowait counter can decrease. busy +20, idle +20, iowait -30 used
    # to shrink the total to 10 and report the machine 100 % busy
    load = make(tmp_path, stat='cpu  1000 0 500 8000 500 0 0 0 0 0\n')
    load.sample()
    (tmp_path / 'stat').write_text('cpu  1020 0 500 8020 470 0 0 0 0 0\n')
    assert load.sample()['cpu'] == 0.5


def test_malformed_or_missing_proc_stat_reads_as_none(tmp_path):
    load = make(tmp_path, stat='intr 1 2 3\n')
    load.sample()
    assert load.sample()['cpu'] is None
    (tmp_path / 'stat').unlink()
    assert load.sample()['cpu'] is None


def test_temperature_in_tenths_of_a_degree(tmp_path):
    assert make(tmp_path).sample()['tempC'] == 61.2


def test_malformed_temperature_is_none(tmp_path):
    assert make(tmp_path, temp='garbage\n').sample()['tempC'] is None


def test_missing_thermal_zone_is_none(tmp_path):
    assert make(tmp_path, temp=None).sample()['tempC'] is None


def test_off_a_pi_undervolt_is_none(tmp_path):
    s = make(tmp_path).sample()
    assert s['undervolt'] is None and s['undervoltAgeSec'] is None


def test_undervolt_alarm_and_its_age(tmp_path):
    clock = Clock(100.0)
    load = make(tmp_path, volt='0\n', clock=clock)
    s = load.sample()
    assert s['undervolt'] is False and s['undervoltAgeSec'] is None   # never seen this run
    alarm = tmp_path / 'hwmon' / 'hwmon1' / 'in0_lcrit_alarm'
    alarm.write_text('1\n')
    clock.t = 130.0
    s = load.sample()
    assert s['undervolt'] is True and s['undervoltAgeSec'] == 0
    alarm.write_text('0\n')
    clock.t = 175.5
    s = load.sample()
    assert s['undervolt'] is False and s['undervoltAgeSec'] == 45     # the governor holds off on this


def test_unreadable_alarm_value_is_none_not_false(tmp_path):
    s = make(tmp_path, volt='x\n').sample()
    assert s['undervolt'] is None


def test_cores_is_reported(tmp_path):
    import os
    assert make(tmp_path).sample()['cores'] == os.cpu_count()


def test_payload_carries_perf_every_emit_and_never_from_the_last_run(make_emitter, tmp_path):
    from lib import almanac_emit
    e = make_emitter()
    e._system = make(tmp_path)
    p1 = e._build_payload()
    assert set(p1['perf']) == {'cpu', 'cores', 'tempC', 'undervolt', 'undervoltAgeSec'}
    assert p1['perf']['cpu'] is None                 # the first sample has no rate yet
    (tmp_path / 'stat').write_text(STAT_B)
    assert e._build_payload()['perf']['cpu'] == round(800 / 1500, 3)
    assert 'perf' in almanac_emit.CARRY_SKIP          # a previous run's load is never "current"
