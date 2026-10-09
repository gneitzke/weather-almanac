"""v5.7 production page/server/emitter contract, real loopback TLS, both themes.

No external sockets: the autouse network fence is also installed for this CLI.
Every step must either advance four decoded frames by 20s or expose a retry at
20s, with the exact rate resume time when capacity prevents acquisition.
"""
import argparse
import hashlib
import json
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import sync_playwright

from tests.verify_radar_headless import radar_server, AUDIT, ae, make_config
from tests.test_radar_keepalive import origin as origin_fixture
from tests.test_emitter_lifecycle import FakeClock
from tests.test_radar_hybrid import png
from tests.conftest import loopback_only_when_offline


def verify(browser, server, origin, patch, theme, output, flaky, cold=False, site_count=2):
    clock=FakeClock();patch.setattr(ae,'Clock',clock)
    patch.setattr(ae,'RADAR_DIR',str(server.root/'radar'))
    if cold:
        shutil.rmtree(server.root/'radar'/'t'/ae._radar_render_revision()/'iem-nexrad-n0b')
    else:
        shutil.rmtree(server.root/'radar'/'t')
    patch.setattr(ae,'RADAR_IEM_METADATA_URL',origin.url+'/metadata')
    patch.setattr(ae,'RADAR_IEM_ARCHIVE_TEMPLATE',origin.url+'/archive/%Y%m%d%H%M')
    patch.setattr(ae,'RADAR_IEM_TILE_TEMPLATE',origin.url+'/tile/{stamp}/{z}/{x}/{y}')
    patch.setattr(ae,'RADAR_SITE_LIST_URL',origin.url+'/listing')
    patch.setattr(ae,'RADAR_SITE_TILE_TEMPLATE',origin.url+'/site/{site}/{stamp}/{z}/{x}/{y}')
    patch.setattr(ae,'_NEXRAD_SITES',{k:v for k,v in ae._NEXRAD_SITES.items() if k in ('KATX','KLGX')})
    if site_count==4:
        patch.setattr(ae,'_NEXRAD_SITES',{k:(47.61,-122.33,k) for k in ('KATX','KLGX','KRTX','KOTX')})
    origin.newest_ts=server.data['radar']['observedTs'] if cold else int(time.time())//120*120
    def response(path, raw):
        if path.startswith('/listing'):
            return json.dumps(dict(scans=[dict(ts=datetime.fromtimestamp(origin.newest_ts-300*i,timezone.utc).isoformat()) for i in range(8)])).encode()
        return png((12,145,16,255)) if path.startswith('/site/') else raw
    origin.response=response;origin.delay=.015
    origin.behavior=lambda path,n: 'hang' if flaky and ('/tile/' in path or '/site/' in path) and n==1 and int(hashlib.sha256(path.encode()).hexdigest()[:8],16)%10<3 else 'normal'
    (server.root/'radar_zoom').write_text('8');(server.root/'radar_viewed').write_text(str(time.time()))
    # Auto is the only source policy and a source changes only with a settled zoom.
    # Pin its verdict to zoom (site at 9+, no coverage evidence needed) so the
    # Region zoom steps stay Region and zoom 9 <-> 8 is the source switch.
    def verdict(self,ctx,site_ok):
        zoom=ctx['desired'] if ctx['desired'] is not None else ctx['auto_zoom']
        return 'site' if site_ok and zoom>=9 else 'mosaic'
    patch.setattr(ae.AlmanacEmitter,'_radar_auto_source',verdict)
    app=SimpleNamespace(config=make_config(Station={'Latitude':'47.61','Longitude':'-122.33'}),obsParser=SimpleNamespace(api_data={}))
    e=ae.AlmanacEmitter(SimpleNamespace(app=app,Obs={},Met={},Astro={},Sager={}),output_path=str(server.root/'wx.json'))
    publications=[];logs=[]
    def publish(*_):
        data=dict(server.data,radar=e._build_payload()['radar']);temp=server.root/'wx.new';temp.write_text(json.dumps(data));temp.replace(server.root/'wx.json')
        publications.append(dict(at=time.monotonic(),source=data['radar']['sourceId'],intent=data['radar']['intent'],ready=data['radar']['completeFrameCount']))
    patch.setattr(e,'_radar_emit_now',publish);patch.setattr(e,'_emit',publish);patch.setattr(ae.Logger,'info',logs.append)
    if cold:
        (server.root/'radar_viewed').unlink()
        e._radar_request_times=[time.monotonic()]*30
    e._do_radar();publish();e._running=True
    context=browser.new_context(viewport=dict(width=1024,height=600),has_touch=True);context.add_init_script(AUDIT)
    context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(server.url+'/') else route.abort())
    page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    rows=[];last_tick=time.monotonic()
    def tick():
        nonlocal last_tick
        e._check_radar_zoom();now=time.monotonic();clock.advance(now-last_tick);last_tick=now;page.wait_for_timeout(50)
    def state():
        return page.evaluate('''({elapsed:performance.now()-(window.v57Tap??performance.now()),camera:radarCamera,source:radarView.data?.sourceId,ready:radarReady().length,stamp:radarView.current?.stamp,bitmap:!!radarView.current?.bitmap,pending:!!radarView.pendingSource,caption:document.getElementById('rad-src-cap').textContent,read:document.getElementById('rad-frame-time').textContent,note:document.getElementById('rad-note').textContent,switch:radarSwitch,owned:radarIntent.owned})''')
    try:
        page.goto(server.url+'/?tabs=1&theme='+theme);page.locator('.tab[data-screen="s-radar"]').click()
        end=time.monotonic()+80
        while time.monotonic()<end:
            if cold:page.wait_for_timeout(50)
            else:tick()
            r=state()
            if r['ready']>=4 and r['owned']:break
        assert r['ready']>=4, r
        steps=[('zoom',7),('zoom',6),('zoom',7),('zoom',8),('zoom',9),('zoom',8),('zoom',9),('zoom',8)]
        if cold:steps=[('zoom',9),('zoom',8)]
        for kind,value in steps:
            start=time.monotonic();before=state();seen=[];first=None;retry=None
            expected_mode='iem-nexrad-n0b' if value>=9 else 'iem-mrms-lcref'
            accepted_source=False
            page.evaluate('(z)=>{window.v57Tap=performance.now();radarZoomChange(z-Math.round(radarCamera.zoom));}',value)
            while time.monotonic()-start<90:
                tick();r=state();r['elapsed']/=1000;seen.append(r)
                assert r['source']!='rainviewer', r
                assert r['source'] in {before['source'],expected_mode},r
                if accepted_source:assert r['source']==expected_mode,r
                accepted_source=accepted_source or r['source']==expected_mode
                assert r['bitmap'] or before['bitmap'] is False, r
                target=r['camera']['zoom']==value and r['source']==expected_mode and not r['pending']
                if target and r['ready']>=4:
                    if first is None:first=(r['stamp'],r['elapsed'])
                    elif r['stamp']!=first[0]:break
                # One 100ms monitor turn is allowed for real event-loop scheduling;
                # the deterministic harness invokes the exact 20s timer callback.
                if r['elapsed']>=20 and retry is None and (r['switch'] and r['switch'].get('overdue') or r['elapsed']>=20.1):
                    retry=r
                    assert r['switch'] and r['switch'].get('overdue'), r
                    assert 'Retry' in r['caption']+r['note']+r['read'] or r['caption'].startswith('Updating view'), r
            assert first and r['stamp']!=first[0], dict(step=(kind,value),last=r,health=e._radar_health_payload())
            assert r['elapsed']<20 or retry is not None
            if cold:assert r['elapsed']<20, dict(step=(kind,value),last=r)
            if not flaky and len(e._radar_request_times)<100:assert r['elapsed']<20, r
            rows.append(dict(step=[kind,value],secondsToFour=first[1],secondsToAdvance=r['elapsed'],deadlineRetry=retry,rows=seen))
            (output/f'{theme}-{flaky}-trace.json').write_text(json.dumps(dict(steps=rows,publications=publications,logs=logs,requests=origin.requests,errors=errors),indent=2))
        assert not errors,errors
        page.screenshot(path=str(output/f'{theme}-{flaky}.png'))
        print(json.dumps(dict(theme=theme,flaky=flaky,steps=[{k:v for k,v in row.items() if k!='rows'} for row in rows])),flush=True)
    finally:
        (output/f'{theme}-{flaky}-final.json').write_text(json.dumps(dict(steps=rows,publications=publications,logs=logs,requests=origin.requests,errors=errors,state=state(),path=page.evaluate('radarMetrics.path')),indent=2))
        e.stop()
        while 'radar' in e._inflight:page.wait_for_timeout(50)
        if e._radar_session:e._radar_session.close()
        context.close()


def run(args):
    args.output_dir.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as patch:
        patch.setenv('RADAR_NET_TEST','0');loopback_only_when_offline.__wrapped__(patch)
        fixture=origin_fixture.__wrapped__(Path(tmp),patch);origin=next(fixture)
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True,args=['--disable-gpu'])
                for theme in ('paper','night'):
                    origin.path_counts.clear()
                    with radar_server() as server,pytest.MonkeyPatch.context() as scenario:verify(browser,server,origin,scenario,theme,args.output_dir,args.flaky,args.cold_switch,args.sites)
                browser.close()
        finally:
            try:next(fixture)
            except StopIteration:pass
    print('RADAR V5.7 SWITCH CONTRACT PASS: paper + night',flush=True)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output-dir',type=Path,default=Path('/private/tmp/radar-v57-harness'));parser.add_argument('--flaky',action='store_true');parser.add_argument('--cold-switch',action='store_true');parser.add_argument('--sites',type=int,choices=[2,4],default=2);args=parser.parse_args();args.output_dir.mkdir(parents=True,exist_ok=True)
    run(args)

if __name__=='__main__':main()
