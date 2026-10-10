"""Small value/config helpers shared by the almanac emitter and radar engine."""

import math
import pytz

# NWS asks for an app/contact User-Agent. A real address stays in station config
# or ALMANAC_CONTACT; both alerts and radar polygons use this fallback.
ALERTS_UA_FALLBACK = 'WeatherAlmanac (+https://github.com/gneitzke/weather-almanac)'

# Unpopulated display values from properties.py / observation_format.py.
_PLACEHOLDERS = {'-', '--', '---', '----', '-----', '------'}


def _clock_style(config):
    """ '12 hr' or '24 hr': the upstream Display/TimeFormat setting, which the
    sunrise/moonrise, observation extremes, forecast and Sager modules already
    follow. Everything the emitter formats itself must follow the same one. """
    return '12 hr' if _cfg(config, 'Display', 'TimeFormat') == '12 hr' else '24 hr'


def _clock(dt, style, sparse=False):
    """ One station-local clock string. 12 hr: "5:13 PM", or "5 PM" on the hour
    when sparse (labels such as "until Wed 5 PM"). 24 hr: "17:13" ("17:00" when
    sparse). Portable: no %-I / %#I. """
    if style != '12 hr':
        return dt.strftime('%H:%M')
    hour12 = dt.hour % 12 or 12
    ampm = 'AM' if dt.hour < 12 else 'PM'
    # A no-break space: "1 AM" must never orphan its meridiem on the next line.
    if sparse and dt.minute == 0:
        return f'{hour12}\u00a0{ampm}'
    return f'{hour12}:{dt.minute:02d}\u00a0{ampm}'


def _cfg(config, section, option, default=None):
    """ Safely read a Kivy ConfigParser value. Kivy's ConfigParser needs BOTH
    section and option to .get() (subscripting a section internally calls the
    one-arg .get() and raises), so always pass both and guard. """
    try:
        return config.get(section, option)
    except Exception:
        return default


def _clean_str(value):
    """ Strip whitespace and return None for known placeholder strings. """
    if not isinstance(value, str):
        return value
    text = value.strip()
    return None if (not text or text in _PLACEHOLDERS) else text


def _num(value, default=None):
    """ Coerce a formatted display value ('64.0', '--', 'Trace', 4.6, ...)
    into a float. 'Trace' (a sub-measurable rain amount) is treated as 0.0
    since that is closer to the truth than null. Never raises. """
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except (OverflowError, ValueError):
            return default
        return number if math.isfinite(number) else default
    if isinstance(value, str):
        text = _clean_str(value)
        if text is None:
            return default
        if text.lower() == 'trace':
            return 0.0
        # Strike counts can render as e.g. "1.2 k" for >= 1000
        if text.lower().endswith('k'):
            try:
                number = float(text[:-1].strip()) * 1000
                return number if math.isfinite(number) else default
            except (OverflowError, ValueError):
                return default
        try:
            number = float(text)
            return number if math.isfinite(number) else default
        except (OverflowError, ValueError):
            return default
    return default


def _json_safe(value):
    """ Replace non-finite measurements before strict JSON serialization. """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


def _snapshot_field(snapshot_attr, field):
    """ Expose one field of a provider snapshot as a plain attribute, for the
    callers (and tests) that seed or read a single value. Reads see the current
    snapshot; a write replaces the whole snapshot, so even a one-field
    assignment is published atomically. """
    def _read(self):
        return getattr(getattr(self, snapshot_attr), field)

    def _write(self, value):
        setattr(self, snapshot_attr, getattr(self, snapshot_attr)._replace(**{field: value}))

    return property(_read, _write)


def _station_tz(config):
    try:
        tzname = _cfg(config, 'Station', 'Timezone')
        return pytz.timezone(tzname) if tzname else None
    except Exception:                                                 # noqa: BLE001
        return None
