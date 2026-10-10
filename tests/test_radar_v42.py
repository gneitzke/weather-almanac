"""Geography scheduling must be independent of viewing and radar transport."""
import io
import json
from pathlib import Path
import threading

from PIL import Image
import pytest

from lib import almanac_emit as ae, radar_basemap as bm
from lib import radar_engine
from tests.test_emitter_lifecycle import FakeClock
from tests.test_freshness_health import _load_serve


@pytest.fixture
def fast_tiles(monkeypatch):
    out=io.BytesIO()
    Image.new('P',(256,256)).save(out,'PNG')
    calls=[]
    def render(*args):
        calls.append(args)
        return out.getvalue()
    monkeypatch.setattr(bm,'tile',render)
    monkeypatch.setattr(radar_engine,'RADAR_GEO_UNVIEWED_SLEEP_SEC',0)
    return calls


def activity(e,**changes):
    record=dict(at=ae.time.time(),theme='night',moving=False,
                center=dict(lat=35.68,lon=139.69),zoom=8)
    record.update(changes)
    Path(e.output_path).with_name('radar_activity').write_text(json.dumps(record))
    return record


def tick(e):
    """Execute the real spawn wrapper, joining so no worker leaks into teardown."""
    e.radar._check_geo()
    # Tests that need concurrency use explicit barriers instead of this helper.
    for thread in threading.enumerate():
        if thread is not threading.current_thread() and thread.name.startswith('geo-test-'):
            thread.join(5)


@pytest.fixture
def geo_threads(monkeypatch):
    real=threading.Thread
    threads=[]
    def create(**kwargs):
        thread=real(name='geo-test-'+str(len(threads)),**kwargs)
        threads.append(thread)
        return thread
    monkeypatch.setattr(ae.threading,'Thread',create)
    yield threads
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()


@pytest.mark.parametrize('hint',[None,'stale','invalid'])
def test_engine_prewarm_before_any_radar_or_view(make_emitter,monkeypatch,geo_threads,hint):
    e=make_emitter();clock=FakeClock();monkeypatch.setattr(ae,'Clock',clock)
    if hint=='stale':activity(e,at=ae.time.time()-200)
    if hint=='invalid':Path(e.output_path).with_name('radar_activity').write_text('{')
    e.start()
    try:
        clock.advance(.24)
        assert not list(Path(radar_engine.RADAR_DIR).rglob('*.png'))
        clock.advance(.01)
        for thread in geo_threads:thread.join(5)
        files=list(Path(radar_engine.RADAR_DIR).glob('geo/*/*/*/*/*.png'))
        assert len(files)==1
        with Image.open(files[0]) as image:assert image.size==(256,256)
        assert e.radar._session is None and e.radar._result==radar_engine._RADAR_NONE
        assert not Path(e.output_path).with_name('radar_viewed').exists()
        assert not Path(e.output_path).with_name('radar_viewing').exists()
        assert (Path(radar_engine.RADAR_DIR)/'.geo-revision').read_text()==bm.version()
    finally:e.stop()


@pytest.mark.parametrize('theme',['paper','night'])
def test_current_activity_preempts_home_and_ignores_old_radar_context(make_emitter,fast_tiles,theme):
    e=make_emitter();e.radar._geo_work()
    e._radar_geo_context=((0,0),dict(lat=0,lon=0),4,4)  # obsolete transport geometry
    Path(e.output_path).with_name('radar_viewed').write_text(str(ae.time.time()))
    a=activity(e,theme=theme)
    expected=list(bm.viewport_requests(a['center'],8,theme))
    for _ in expected:e.radar._geo_work()
    assert fast_tiles[1:]==expected
    e.radar._geo_work()
    home=list(bm.home_requests((47.61,-122.33),radar_engine._radar_zoom_for(47.61)))
    assert fast_tiles[-1]==home[1]
    # No live viewing session is needed for the current activity report either.
    assert not Path(e.output_path).with_name('radar_viewing').exists()


@pytest.mark.parametrize('age,viewed',[(6,True),(-10,True),(0,False)])
def test_freshness_and_view_gate_only_viewport(make_emitter,fast_tiles,age,viewed):
    e=make_emitter();activity(e,at=ae.time.time()-age)
    if viewed:Path(e.output_path).with_name('radar_viewed').write_text(str(ae.time.time()))
    e.radar._geo_work()
    assert fast_tiles==[next(bm.home_requests((47.61,-122.33),radar_engine._radar_zoom_for(47.61),'night'))]


@pytest.mark.parametrize('age',[0,200])
def test_moving_suppresses_both_queues_until_settled(make_emitter,fast_tiles,age):
    e=make_emitter();activity(e,moving=True,at=ae.time.time()-age)
    e.radar._geo_work();assert len(fast_tiles)==(0 if age<5 else 1)
    before=len(fast_tiles)
    activity(e,moving=False);e.radar._geo_work();assert len(fast_tiles)==before+1


def test_background_sleep_and_viewed_priority(make_emitter,fast_tiles,monkeypatch):
    e=make_emitter();sleeps=[]
    monkeypatch.setattr(radar_engine,'RADAR_GEO_UNVIEWED_SLEEP_SEC',.05)
    monkeypatch.setattr(ae.time,'sleep',sleeps.append)
    e.radar._geo_work();assert sleeps==[.05]
    Path(e.output_path).with_name('radar_viewed').write_text(str(ae.time.time()))
    activity(e);e.radar._geo_work();assert sleeps==[.05]


def test_radar_inflight_and_held_result_lock_cannot_block_geo(make_emitter,fast_tiles,geo_threads):
    e=make_emitter(running=True)
    entered=threading.Event();release=threading.Event();written=threading.Event()
    real=bm.atomic_write
    def write(path,raw):
        real(path,raw)
        if path.suffix=='.png':written.set()
    # Hold the actual radar result lock through a simulated stalled network pass.
    def radar():
        with e.radar._lock:
            entered.set()
            assert release.wait(5)
    from unittest.mock import patch
    try:
        with patch.object(bm,'atomic_write',write):
            e.radar._spawn('radar',radar);assert entered.wait(2)
            e.radar._check_geo()
            assert written.wait(2), 'geo waited behind radar'
            assert 'radar' in e._runtime.inflight and not release.is_set()
            assert list(Path(radar_engine.RADAR_DIR).rglob('*.png'))
    finally:release.set();e.stop()


def test_geo_is_single_flight_and_stopped_callback_is_fenced(make_emitter,fast_tiles,geo_threads,monkeypatch):
    e=make_emitter(running=True);entered=threading.Event();release=threading.Event()
    real=bm.tile
    def blocked(*args):
        entered.set();assert release.wait(5);return real(*args)
    monkeypatch.setattr(bm,'tile',blocked)
    try:
        e.radar._check_geo();assert entered.wait(2)
        for _ in range(10):e.radar._check_geo()
        assert len(geo_threads)==1 and 'geo' in e._runtime.inflight
        e.stop();e.radar._check_geo();assert len(geo_threads)==1
    finally:release.set()


def test_home_completes_idles_and_rewarms_on_revision_station(make_emitter,fast_tiles,geo_threads,monkeypatch):
    e=make_emitter(running=True)
    for _ in range(491):tick(e)
    assert len(fast_tiles)==490 and not e.radar._geo_state.home
    count=len(geo_threads)
    for _ in range(4):tick(e)
    assert len(geo_threads)==count  # no worker or home directory scan once complete
    original=fast_tiles[:]
    monkeypatch.setattr(bm,'version',lambda:'123456abcdef')
    for _ in range(491):tick(e)
    assert fast_tiles[490:]==original
    e.app.config['Station'].update(Latitude='-33.87',Longitude='151.21')
    for _ in range(491):tick(e)
    expected=list(bm.home_requests((-33.87,151.21),radar_engine._radar_zoom_for(-33.87)))
    assert set(expected)<=set(fast_tiles[980:])|set(original)
    assert all(bm.tile_path(radar_engine.RADAR_DIR,*r).is_file() for r in expected)
    assert not e.radar._geo_state.home
    e.stop()


def test_png_visible_only_after_atomic_replace(tmp_path,fast_tiles,monkeypatch):
    target=bm.tile_path(tmp_path,'paper',8,40,89);replace=bm.os.replace;seen=[]
    def inspect(source,dest):
        assert not target.exists()
        assert Path(source).parent==target.parent
        with Image.open(source) as image:image.load();assert image.size==(256,256)
        seen.append(dest);replace(source,dest)
    monkeypatch.setattr(bm.os,'replace',inspect)
    bm.cache_tile(tmp_path,'paper',8,40,89)
    assert seen==[target] and target.exists() and not list(tmp_path.rglob('.tile-*'))


def test_activity_carries_displayed_camera_independent_of_desired_zoom(tmp_path,monkeypatch):
    server=_load_serve(monkeypatch,tmp_path,{})
    params=dict(radarTheme=['night'],radarMoving=['0'],radarGeoCenter=['35.68,139.69'],
                radarGeoZoom=['7'],radarZoom=['auto'])
    a=server._radar_activity(params)
    assert a['center']==dict(lat=35.68,lon=139.69) and a['zoom']==7 and a['theme']=='night'
    for bad in ['91,0','NaN,0','0,181','0,0&bad']:
        assert 'center' not in server._radar_activity(dict(params,radarGeoCenter=[bad]))


def test_benchmark_summary_first_with_actual_network_counts(tmp_path):
    from tools.benchmark_radar_kiosk import benchmark_output,server_requests
    def event(method,ident,**params):
        return dict(method='Network.'+method,params=dict(requestId=ident,**params))
    events=[event('requestWillBeSent',str(i),wallTime=1+i/10,request=dict(url='http://localhost/tile'))
            for i in range(4)]
    events.extend([event('requestServedFromCache','1'),
                   event('responseReceived','2',response=dict(fromDiskCache=True))])
    counts=server_requests(events,dict(pan=[1,1.4],pinch=[2,3]))
    assert counts==dict(pan=2,pinch=0)
    value=dict(cachedFirstPaintMs=9,memoryPeakMiB=30,fences=dict(memory=True),serverRequests=counts,
               frames=[dict(phase='pan',drawImages=4,ms=n) for n in range(1,21)],
               fetches=[dict(phase='pan')]*4,gpu={'large':'details'})
    (tmp_path/'geo'/'rev').mkdir(parents=True);(tmp_path/'geo'/'rev'/'tile.png').touch()
    (tmp_path/'t').mkdir();(tmp_path/'t'/'radar.png').touch()
    out=benchmark_output(value,tmp_path)
    assert next(iter(out))=='summary' and 'frames' not in out and 'gpu' not in out
    assert out['summary']['pan']==dict(frames=20,maxDrawMs=20,p95DrawMs=19,maxDrawImages=4,serverRequests=2)
    assert out['summary']['geoTilesOnDisk']==out['summary']['radarTilesOnDisk']==1
    assert benchmark_output(value,tmp_path,True)['frames']==value['frames']
