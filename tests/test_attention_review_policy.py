"""Regression cases from the attention adversarial review; no network."""
import json
import os
from datetime import datetime, timezone

import pytest
import pytz

from lib import almanac_emit as ae
from lib.radar_attention import Attention, AWAY_SEC, REST_TO_DORMANT_SEC
from tests.test_radar_attention import sig, T0
from tests.test_radar_hybrid import hybrid  # noqa: F401


def test_rest_dwell_begins_at_actual_entry_not_desired_entry():
    a = Attention(T0, 'watch')
    a.decide(sig(T0, local_hour=1))
    assert a.decide(sig(T0 + 600, local_hour=1)) == 'rest'
    assert a.decide(sig(T0 + REST_TO_DORMANT_SEC, local_hour=1)) == 'rest'
    assert a.decide(sig(T0 + 600 + REST_TO_DORMANT_SEC, local_hour=1)) == 'dormant'


def test_fresh_install_without_attention_markers_eventually_becomes_away():
    a = Attention(T0, 'rest')
    assert a.decide(sig(T0 + AWAY_SEC, viewed_age=None, touch_age=None)) == 'dormant'


def test_rain_hold_is_anchored_to_observation_not_repeated_tick():
    a = Attention(T0, 'rest')
    a.decide(sig(T0, rain_rate_mm=1, obs_age=0))
    a.decide(sig(T0 + 240, rain_rate_mm=1, obs_age=240))
    assert a.wet_until == T0 + 3600


@pytest.mark.parametrize('status,wet', [('-', False), ('Unknown', False),
    ('Currently Dry', False), ('Very Light Rain', True), ('Light Rain', True),
    ('Moderate Rain', True), ('Heavy Rain', True), ('Very Heavy Rain', True),
    ('Extreme Rain', True), ('Snow Likely', True)])
def test_recognized_rain_statuses_only(make_emitter, status, wet):
    e = make_emitter()
    s = e._radar_attention_signals(dict(rainStatus=status), T0, timezone.utc)
    assert s.rain_wet is wet


def test_carried_observation_uses_original_epoch(make_emitter, hybrid, tmp_path):
    now = ae.time.time()
    (tmp_path / 'wx.json').write_text(json.dumps(dict(obsTs=now-900,
        obsAgeSec=5, rainStatus='Heavy Rain', rainRateMm=5)))
    e = make_emitter()
    p = e._build_payload()
    assert p['carried'] and p['obsAgeSec'] == 900
    assert e._radar_attention.wet_until == 0
    assert p['radar']['attention']['reason'] == 'observations unknown'


@pytest.mark.parametrize('utc,hour', [('2026-11-01T08:30:00', 1.5),
    ('2026-11-01T09:30:00', 1.5), ('2026-03-08T10:30:00', 3.5)])
def test_station_timezone_across_dst(make_emitter, utc, hour):
    now = datetime.fromisoformat(utc).replace(tzinfo=timezone.utc).timestamp()
    s = make_emitter()._radar_attention_signals({}, now, pytz.timezone('America/Los_Angeles'))
    assert s.local_hour == hour


def test_lightning_is_seconds_and_each_strike_extends_hold(make_emitter):
    e = make_emitter()
    for now, age in ((T0, 1200), (T0+600, 10)):
        s = e._radar_attention_signals(dict(lightningSinceSec=age), now, timezone.utc)
        e._radar_attention.decide(s)
        assert e._radar_attention.lightning_until == now + 1800 - age


def test_rest_dwell_resets_on_weather_bounce_and_handles_zero_epoch():
    a = Attention(0, 'rest')
    assert a.decide(sig(REST_TO_DORMANT_SEC, local_hour=1)) == 'dormant'
    assert a.decide(sig(8000, rain_rate_mm=1, obs_age=0)) == 'watch'
    assert a.rest_since is None
    assert a.decide(sig(12000, local_hour=1)) == 'rest'
    assert a.rest_since == 12000
    assert a.decide(sig(15000, local_hour=1)) == 'rest'


def test_force_expiry_shadow_and_health_share_current_decision(make_emitter, hybrid, tmp_path, monkeypatch):
    monkeypatch.setattr(ae, 'RADAR_ATTENTION_MODE', 'shadow')
    e = make_emitter(); e._running = True
    marker = tmp_path / 'radar_attention_force'
    marker.write_text('dormant')
    os.utime(marker, (ae.time.time(), ae.time.time()))
    scheduled = []
    e._schedule = lambda *args, **kwargs: scheduled.append(args)
    p = e._build_payload()['radar']
    assert p['attention']['tier'] == e._radar_health_payload()['attention']['tier'] == 'dormant'  # health: radar-health.json
    assert not scheduled
    hybrid.mono += ae.RADAR_ATTENTION_FORCE_TTL
    p = e._build_payload()['radar']
    assert p['attention']['tier'] == 'watch'
    assert e._radar_health_payload()['attention']['forced'] is None
    assert not scheduled


@pytest.mark.parametrize('value', ['nan', 'inf', '-inf', 'garbage'])
def test_invalid_presence_epochs_do_not_prove_attention(make_emitter, hybrid, tmp_path, value):
    (tmp_path / 'presence').write_text(value)
    assert make_emitter()._radar_marker_age('presence', ae.time.time()) is None


def test_forecast_words_override_low_probability_as_documented():
    a = Attention(T0, 'rest')
    a.decide(sig(T0, conditions='Chance of rain 10%', precip_pct=10))
    assert not a.forecast_on            # a spoken probability is judged by its number, not its noun
    a.decide(sig(T0, conditions='Chance of rain 60%', precip_pct=10))
    assert a.forecast_on
    assert a.forecast_on
