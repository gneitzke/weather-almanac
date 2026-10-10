import io
import json
from http.client import IncompleteRead

import pytest
from lib import almanac_emit as ae
from lib import radar_engine
from tests.test_radar_hybrid import hybrid  # noqa: F401


@pytest.mark.parametrize('fail', [False, True])
def test_body_bytes_keep_request_start_tier_including_partial_failure(make_emitter, hybrid, monkeypatch, fail):
    e = make_emitter(); e.radar._attention.tier = 'rest'
    class Response(io.BytesIO):
        status = 200
        headers = {}
        count = 0
        def read(self, size):
            self.count += 1
            e.radar._attention.tier = 'live'
            if self.count == 1: return b'x'*10
            if fail: raise IncompleteRead(b'123', 8)
            return b''
    e.radar._session = radar_engine.RadarSession(); e.radar._session.begin_pass(100)
    monkeypatch.setattr(e.radar._session, 'open', lambda *a, **k: Response())
    try:
        e.radar._request('iem-mrms-lcref', radar_engine.RADAR_IEM_METADATA_URL, 100, metadata=True)
    except IncompleteRead:
        assert fail
    assert e.radar._bytes_by_tier == {'rest': 13 if fail else 10}
    metric = e.radar._request_metrics[-1]
    assert metric['bytes'] == (13 if fail else 10) and metric['tier'] == 'rest'
    health = e.radar._health_payload()
    json.dumps(health, allow_nan=False)
    assert 'response bodies' in health['attention']['byteAccounting']
