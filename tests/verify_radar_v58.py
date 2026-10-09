"""Warm disk switch and cold 15-tile newest: real page/engine/TLS, loopback only."""
import argparse
import cProfile
import io
import json
import pstats
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import urllib.request

import pytest
from playwright.sync_api import sync_playwright
from tests.verify_radar_headless import radar_server, tile_png, AUDIT, ae, make_config
from tests.test_radar_keepalive import origin as origin_fixture
from tests.test_radar_hybrid import png
from tests.test_emitter_lifecycle import FakeClock
from tests.conftest import loopback_only_when_offline


def verify(browser, server, origin, patch, theme, output):
    patch.setattr(ae,'RADAR_DIR',str(server.root/'radar'))
    patch.setattr(ae,'_NEXRAD_SITES',{k:(47.61,-122.8,k) for k in ('KATX','KLGX','KRTX')})
    patch.setattr(ae,'RADAR_IEM_METADATA_URL',origin.url+'/metadata')
    patch.setattr(ae,'RADAR_IEM_ARCHIVE_TEMPLATE',origin.url+'/archive/%Y%m%d%H%M')
    patch.setattr(ae,'RADAR_IEM_TILE_TEMPLATE',origin.url+'/tile/{stamp}/{z}/{x}/{y}')
    patch.setattr(ae,'RADAR_SITE_LIST_URL',origin.url+'/listing')
    patch.setattr(ae,'RADAR_SITE_TILE_TEMPLATE',origin.url+'/site/{site}/{stamp}/{z}/{x}/{y}')
    clock=FakeClock();patch.setattr(ae,'Clock',clock)
    newest=int(time.time())//120*120-360
    origin.newest_ts=newest;origin.delay=0;origin.behavior=None
    def response(path,raw):
        if path.startswith('/listing'):
            return json.dumps(dict(scans=[dict(ts=datetime.fromtimestamp(newest-120*i,timezone.utc).isoformat()) for i in range(8)])).encode()
        return png((12,145,16,255)) if path.startswith('/site/') else raw
    origin.response=response
    shutil.rmtree(server.root/'radar'/'t')
    ctx=dict(center=dict(lat=47.61,lon=-122.8),zoom=8)
    # Seed only the bounded active cache, 3 sites x 3 levels x 8 scans + Region.
    for source,sites in (('iem-mrms-lcref',[None]),('iem-nexrad-n0b',['KATX','KLGX','KRTX'])):
        for i in range(8):
            for z in (7,8,9):
                for x,y,*_ in ae._radar_grid(ctx,z,margin=1):
                    path=ae._radar_tile_path(source,sites[0],newest-i*120,z,x,y)
                    raw=tile_png(ae._RADAR_LUT[i*3][1])
                    for site in sites:
                        path=ae._radar_tile_path(source,site,newest-i*120,z,x,y)
                        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
    for name,value in dict(radar_zoom='8',radar_viewed=str(time.time())).items():
        (server.root/name).write_text(value)
    # Auto is the only source policy; pin its verdict so the scenario drives the source.
    verdict=dict(mode='mosaic')
    patch.setattr(ae.AlmanacEmitter,'_radar_auto_source',lambda self,ctx,site_ok:verdict['mode'] if site_ok else 'mosaic')
    app=SimpleNamespace(config=make_config(Station={'Latitude':'47.61','Longitude':'-122.8'}),obsParser=SimpleNamespace(api_data={}))
    e=ae.AlmanacEmitter(SimpleNamespace(app=app,Obs={},Met={},Astro={},Sager={}),output_path=str(server.root/'wx.json'))
    publications=[];profiles=[];rows=[]
    def publish(*_):
        data=dict(server.data,radar=e._build_payload()['radar'])
        path=server.root/'wx.new';path.write_text(json.dumps(data));path.replace(server.root/'wx.json')
        publications.append(dict(at=time.monotonic(),source=data['radar']['sourceId'],ready=data['radar']['completeFrameCount']))
    patch.setattr(e,'_emit',publish)
    original=e._do_radar
    def profiled(*args,**kwargs):
        pr=cProfile.Profile();cpu=time.thread_time();started=time.monotonic()
        pr.runcall(original,*args,**kwargs)
        name=f'{theme}-worker-{len(profiles)}'
        pr.dump_stats(str(output/(name+'.prof')))
        with (output/(name+'.txt')).open('w') as f:pstats.Stats(pr,stream=f).sort_stats('cumulative').print_stats(30)
        profiles.append(dict(name=name,cpuSec=time.thread_time()-cpu,wallSec=time.monotonic()-started))
    patch.setattr(e,'_do_radar',profiled)
    e._do_radar();publish();e._running=True
    e._schedule(e._emit,2,interval=True)
    e._radar_arm_discovery()
    context=browser.new_context(viewport=dict(width=1024,height=600),has_touch=True)
    context.add_init_script(AUDIT)
    context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(server.url+'/') else route.abort())
    page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    last=time.monotonic()
    def tick():
        nonlocal last
        e._check_radar_zoom();now=time.monotonic();clock.advance(now-last);last=now;page.wait_for_timeout(40)
    def state():
        return page.evaluate('''({ready:radarReady().length,stamp:radarView.current?.stamp,source:radarView.data?.sourceId,pending:!!radarView.pendingSource,owned:radarIntent.owned,elapsed:(performance.now()-(window.v58Tap??performance.now()))/1000})''')
    def until(predicate,seconds=20):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            tick();r=state()
            if predicate(r):return r
        raise AssertionError(state())
    try:
        page.goto(server.url+'/?tabs=1&theme='+theme);page.locator('.tab[data-screen="s-radar"]').click()
        until(lambda r:r['ready']>=8 and r['owned'])
        # Control: precisely 15 independent tiles, six actual pool workers.
        def behavior(path,n):
            if path.startswith(('/tile/','/control/')):threading.Event().wait(.55)
            return 'normal'
        origin.behavior=behavior
        control=ae.RadarSession();deadline=time.monotonic()+10;control.begin_pass(deadline)
        def get(i):
            with control.open(urllib.request.Request(origin.url+f'/control/{i}'),3) as r:r.read()
        started=time.monotonic()
        with ThreadPoolExecutor(max_workers=6) as pool:list(pool.map(get,range(15)))
        network=time.monotonic()-started;control.close()
        for mode in ('site','mosaic'):
            if mode=='mosaic':
                origin.newest_ts=newest+120
                # Simulate metadata age after dwelling in site mode; no rate reset.
                when,known=e._radar_newest[('iem-mrms-lcref',None)]
                e._radar_newest[('iem-mrms-lcref',None)]=(time.monotonic()-121,known)
            start_request=len(origin.requests);first=advanced=None;eight=None;engine_newest=None
            tap=time.monotonic()
            page.evaluate("()=>{window.v58Tap=performance.now()}");verdict['mode']=mode;e._radar_restart=True
            target='iem-nexrad-n0b' if mode=='site' else 'iem-mrms-lcref'
            samples=[]
            end=time.monotonic()+15
            while time.monotonic()<end:
                tick();r=state();samples.append(r)
                snap=e._radar_result
                if snap.source_id==target and snap.frames and snap.frames[-1]['complete'] and engine_newest is None:
                    engine_newest=time.monotonic()-tap
                if r['source']==target and not r['pending']:
                    if r['ready']>=4 and first is None:first=dict(r)
                    if first and r['stamp']!=first['stamp'] and advanced is None:advanced=r['elapsed']
                    if r['ready']>=8 and eight is None:eight=r['elapsed']
                if advanced is not None and eight is not None:break
            assert first and advanced and eight, r
            assert eight<12 and advanced<12, r
            requests=origin.requests[start_request:]
            visible={(x,y) for x,y,*_ in ae._radar_grid(ctx)}
            newest_requests=[p for _,_,p in requests if p.startswith('/tile/'+ae._radar_stamp_text(newest+120)+'/8/') and tuple(map(int,p.split('/')[-2:])) in visible]
            if mode=='site':assert not any('/site/' in p for _,_,p in requests),requests
            else:
                assert len(set(newest_requests))==15,newest_requests
                assert engine_newest < network+2.5,(engine_newest,network)
            rows.append(dict(mode=mode,four=first['elapsed'],advance=advanced,eight=eight,engineNewest=engine_newest,requests=len(requests),newestTileRequests=len(newest_requests),uniqueNewestTiles=len(set(newest_requests)),samples=samples))
        assert not errors,errors
        page.screenshot(path=str(output/(theme+'.png')))
        result=dict(theme=theme,control15TilesSec=network,startup=e._radar_disk_inventory.startup,steps=rows,workerProfiles=profiles,publications=publications,errors=errors)
        (output/(theme+'.json')).write_text(json.dumps(result,indent=2))
        print(json.dumps(dict(theme=theme,control=network,startup=result['startup'],steps=[{k:v for k,v in r.items() if k!='samples'} for r in rows])),flush=True)
    finally:
        e.stop()
        while 'radar' in e._inflight:page.wait_for_timeout(40)
        if e._radar_session:e._radar_session.close()
        context.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output-dir',type=Path,default=Path('/private/tmp/radar-v58-switch'));args=parser.parse_args();args.output_dir.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp,pytest.MonkeyPatch.context() as patch:
        patch.setenv('RADAR_NET_TEST','0');loopback_only_when_offline.__wrapped__(patch)
        fixture=origin_fixture.__wrapped__(Path(tmp),patch);origin=next(fixture)
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True,args=['--disable-gpu'])
                for theme in ('paper','night'):
                    with radar_server() as server,pytest.MonkeyPatch.context() as scenario:verify(browser,server,origin,scenario,theme,args.output_dir)
                browser.close()
        finally:
            try:next(fixture)
            except StopIteration:pass
    print('RADAR V5.8 PASS: paper + night',flush=True)

if __name__=='__main__':main()
