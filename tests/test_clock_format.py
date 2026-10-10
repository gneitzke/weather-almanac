"""Every clock string the emitter formats itself follows Display/TimeFormat,
the same setting the upstream sunrise/observation/forecast/Sager modules
follow. 2026-09-16: the masthead clock, alert timestamps and radar check times
were hardcoded 24-hour while the AQI peak hour and the alert "until" label were
hardcoded 12-hour, so every console showed a mix whatever it was set to."""
import re
from datetime import datetime, timezone
import pytest
import pytz
from lib import almanac_emit as ae
from lib import radar_engine
from tests.fixtures.config import make_config

TZ = pytz.timezone('America/Los_Angeles')
NB = '\u00a0'  # the no-break space before every meridiem
TWELVE = re.compile(r'^(1[0-2]|[1-9]):[0-5]\d\u00a0[AP]M$')
TWENTY_FOUR = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')


def local(y, mo, d, h, mi):
    return TZ.localize(datetime(y, mo, d, h, mi))


@pytest.mark.parametrize('style,expect', [('12 hr', TWELVE), ('24 hr', TWENTY_FOUR)])
def test_clock_helper(style, expect):
    assert expect.match(ae._clock(local(2026, 9, 16, 17, 13), style))
    assert ae._clock(local(2026, 9, 16, 0, 5), style) == ('12:05'+NB+'AM' if style == '12 hr' else '00:05')
    assert ae._clock(local(2026, 9, 16, 12, 0), style) == ('12:00'+NB+'PM' if style == '12 hr' else '12:00')
    assert ae._clock(local(2026, 9, 16, 17, 0), style, sparse=True) == ('5'+NB+'PM' if style == '12 hr' else '17:00')
    assert ae._clock(local(2026, 9, 16, 17, 30), style, sparse=True) == ('5:30'+NB+'PM' if style == '12 hr' else '17:30')


def test_style_comes_from_the_display_setting():
    assert ae._clock_style(make_config(Display={'TimeFormat': '12 hr'})) == '12 hr'
    assert ae._clock_style(make_config(Display={'TimeFormat': '24 hr'})) == '24 hr'
    assert ae._clock_style({}) == '24 hr'  # upstream's own default


@pytest.mark.parametrize('style,expect', [('12 hr', TWELVE), ('24 hr', TWENTY_FOUR)])
def test_masthead_clock_and_alert_stamp_follow_the_setting(make_emitter, style, expect):
    e = make_emitter(config=make_config(Display={'TimeFormat': style}))
    payload = e._build_payload()
    assert expect.match(payload['time']), payload['time']


@pytest.mark.parametrize('style,expect', [('12 hr', 'Wed 5'+NB+'PM'), ('24 hr', 'Wed 17:00')])
def test_alert_until_text_follows_the_setting(style, expect):
    epoch = local(2026, 9, 16, 17, 0).timestamp()
    assert ae.AlmanacEmitter._until_text(epoch, TZ, style) == expect


@pytest.mark.parametrize('style,expect', [('12 hr', '5'+NB+'PM'), ('24 hr', '17:00')])
def test_aqi_peak_hour_follows_the_setting(style, expect):
    now = local(2026, 9, 16, 12, 0).timestamp()
    hourly = dict(time=[f'2026-09-16T{h:02d}:00' for h in range(12, 24)],
                  us_aqi=[20, 22, 25, 30, 40, 55, 30, 25, 20, 20, 20, 20])  # peak at 17:00
    series, peak, peak_time, *_ = ae.AlmanacEmitter._aqi_forecast_summary(hourly, now, TZ, 20, style)
    assert peak == 55 and peak_time == expect


@pytest.mark.parametrize('style,expect', [('12 hr', TWELVE), ('24 hr', TWENTY_FOUR)])
def test_radar_check_times_follow_the_setting(style, expect):
    ts = local(2026, 9, 16, 18, 25).timestamp()
    nexrad = dict(id='KATX', name='Camano Island', distanceMeters=1.0, bearing='NW',
                  reporting=True, newestTs=ts-300, ageSec=300, reason=None, checkedTs=ts, nextCheckTs=ts+60)
    snap = radar_engine._RADAR_NONE._replace(nexrad=nexrad)
    r = radar_engine.RadarEngine._payload(snap, ts, TZ, None, style)
    assert expect.match(r['nexrad']['checkedAt']) and expect.match(r['nexrad']['nextCheckAt'])


def test_upstream_sager_case_is_normalised():
    assert ae._clock_case('6:53 pm') == '6:53'+NB+'PM'
    assert ae._clock_case('06:53') == '06:53'
    assert ae._clock_case('-') == '-' and ae._clock_case(None) is None


def test_alerts_clock_style_survives_a_bare_emitter():
    # tests build the alert processor on an object without an app handle
    A = ae.AlmanacEmitter
    assert A._process_alerts.__get__(A.__new__(A))([], 0, TZ) == []


def test_upstream_clock_strings_get_a_no_break_meridiem():
    assert ae._clock_case('Clear until 1 AM on Saturday') == 'Clear until 1'+NB+'AM on Saturday'
    assert ae._clock_case('6:46 AM') == '6:46'+NB+'AM'
    assert ae._clock_case('19:17') == '19:17'
