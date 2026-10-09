"""The fail-safe reproductions through Tracker and production JavaScript/Node."""
import json

import pytest

from lib import nws_warnings as nw
from tests.test_radar_warnings_page import run
from tests.test_radar_warnings_remaining import NOW
from tests.test_radar_warnings_review import parse
from tests.test_radar_warnings_supersession import REPRODUCTIONS


@pytest.mark.parametrize('factory,order', REPRODUCTIONS)
def test_uncovered_station_stays_visible_through_tracker_and_page(factory, order):
    features = factory()
    tracker = nw.Tracker()
    tracker.succeeded(NOW - 60, parse(features[:-2], NOW - 60), 'fixture')
    before = tracker.payload(NOW - 60)
    tracker.succeeded(NOW, parse([features[k] for k in order], NOW), 'fixture')
    after = tracker.payload(NOW)
    run('const before=' + json.dumps(before) + ';const after=' + json.dumps(after)
        + ';const stationId=' + json.dumps(features[0]['properties']['id']) + ';\n' + r'''
wall=NOW-60;radarWarnUpdate(before,wall);
assert.equal($('rad-warn-tag').hidden,false);
wall=NOW;radarWarnUpdate(after,wall);
assert.equal($('rad-warn-tag').hidden,false);
assert.ok($('rad-warn-tag-when').textContent.includes('At this station'));
assert.equal(paths().length,2);
assert.equal(radarWarn.nodes.get(stationId).node.attrs['data-covers'],'true');
assert.equal($('rad-warnings-count').textContent,'Shown · 2');
assert.equal($('rad-warn-health').hidden,true);
assert.equal(radarWarnProblem(),'');
'''.replace('NOW', str(NOW)))
