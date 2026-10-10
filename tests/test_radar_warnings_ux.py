""" Warning layer UX pass, engine side (lib/nws_warnings).

Two additive payload changes the page needs, and nothing else:
  - a failed refresh is reported (`refreshFailedAt`) instead of making the data
    stale at once; `stale`/`staleAt` stay tied to the last success, so the page
    keeps drawing last-good warnings (dimmed) until that deadline;
  - each item carries the warning's own action text (`instruction`), verbatim
    apart from folded whitespace, with no omitted instructions.
Hermetic: no network, no clock reads (times are passed in).
"""
import json
import time
import urllib.error

from lib import nws_warnings as nw
from tests.fixtures import nws_warnings as fx
from tests.test_radar_warnings import FakeResp, Net, emitter, parse


# ------------------------------------------------------------ refresh failure
def test_a_failed_refresh_is_reported_and_keeps_the_last_success_deadline():
    t = nw.Tracker()
    t.began(1000.0, fast=True)
    t.succeeded(1000.0, [], 'u')
    deadline = t.payload(1000.0, fast=True)['staleAt']
    assert deadline == 1000 + nw.FAST_SEC + nw.STALE_GRACE_SEC
    t.failed(1090.0, 'HTTP 503')
    p = t.payload(1090.0, fast=True)
    assert p['refreshFailedAt'] == 1090 and p['stale'] is False and p['staleAt'] == deadline
    # the deadline, not the failure, decides when the data stops being current
    assert t.payload(deadline - 1, fast=True)['stale'] is False
    assert t.payload(deadline, fast=True)['stale'] is True
    assert t.payload(deadline, fast=True)['refreshFailedAt'] == 1090


def test_a_later_failure_moves_the_mark_and_any_success_clears_it():
    t = nw.Tracker()
    t.succeeded(1000.0, [], 'u')
    t.failed(1100.0, 'x')
    t.failed(1340.0, 'y')
    assert t.payload(1340.0)['refreshFailedAt'] == 1340
    t.not_modified(1400.0)
    assert t.payload(1400.0)['refreshFailedAt'] is None
    t.failed(1500.0, 'z')
    t.succeeded(1600.0, [], 'u')
    assert t.payload(1600.0)['refreshFailedAt'] is None
    t.failed(1700.0, 'z')
    t.no_coverage(1800.0)
    assert t.payload(1800.0)['refreshFailedAt'] is None


def test_a_failure_before_any_success_is_stale_and_reported():
    t = nw.Tracker()
    t.failed(10.0, 'HTTP 503')
    p = t.payload(10.0)
    assert p['stale'] is True and p['staleAt'] is None and p['refreshFailedAt'] == 10 and p['items'] == []


def test_the_failure_mark_does_not_change_the_retry_schedule():
    t = nw.Tracker()
    t.succeeded(1000.0, [], 'u')
    t.failed(1090.0, 'x')
    assert t.next_due(1090.0, True, 'u') == 1090.0 + nw.RETRY_BASE_SEC
    assert t.health(1090.0)['failures'] == 1


def test_the_emitter_publishes_the_failure_with_last_good_items(make_emitter, monkeypatch):
    net = Net(monkeypatch)
    now = time.time()
    net.answers.append(FakeResp(json.dumps(fx.collection(fx.feature(now, ring=fx.OVER_STATION, ends_in=3600)))))
    e = emitter(make_emitter)
    reach, home, codes, url = e.radar._warnings_query()
    e.radar._do_warnings(url, reach, home)
    net.answers.append(urllib.error.HTTPError(url, 503, 'busy', {}, None))
    e.radar._do_warnings(url, reach, home)
    w = e._build_payload()['radar']['warnings']
    assert w['stale'] is False and len(w['items']) == 1
    assert isinstance(w['refreshFailedAt'], int) and w['refreshFailedAt'] >= w['fetchedTs']


# ---------------------------------------------------------------- instruction
def test_instruction_is_the_feeds_own_text():
    now = time.time()
    [item] = parse([fx.feature(now, ring=fx.OVER_STATION)], now)
    assert item['instruction'] == 'TAKE COVER NOW!'


def test_instruction_whitespace_is_folded_and_absence_stays_absent():
    assert nw._instruction({'instruction': 'Move to an interior room\non the lowest floor.\n\n Avoid windows.'}) \
        == 'Move to an interior room on the lowest floor. Avoid windows.'
    assert nw._instruction({'instruction': None}) is None
    assert nw._instruction({'instruction': '   '}) is None
    assert nw._instruction({}) is None
    assert nw._instruction({'instruction': 42}) is None


def test_long_instruction_is_preserved_in_full_including_the_last_sentence():
    sentence = 'Move to a basement or an interior room on the lowest floor of a sturdy building. '
    text = nw._instruction({'instruction': sentence * 8})
    assert text == (sentence * 8).strip()
    words = nw._instruction({'instruction': 'shelter ' * 80})
    assert words == ('shelter ' * 80).strip()


def test_instruction_is_published_without_private_fields():
    now = time.time()
    t = nw.Tracker()
    t.succeeded(now, parse([fx.feature(now, ring=fx.OVER_STATION)], now), 'u')
    [item] = t.payload(now)['items']
    assert item['instruction'] == 'TAKE COVER NOW!' and not any(k.startswith('_') for k in item)
