"""Deterministic browser intent/deadline scenarios on production page, both themes."""
import argparse
import copy
import json
import time
from pathlib import Path
from playwright.sync_api import sync_playwright
from tests.verify_radar_headless import radar_server, AUDIT, ae


def site_payload(original):
    """The fixture's Region payload re-described as the KATX site view."""
    data=copy.deepcopy(original);r=data['radar'];r.update(sourceId='iem-nexrad-n0b',sourceMode='site',siteId='KATX',
        legend=dict(ae._RADAR_DISPLAY_RAMP,remapped=True),cadenceSec=300,sites=[dict(id='KATX',lat=48.1947,lon=-122.4957,contributing=True,reporting=True)],zoomMin=7,zoomMax=10)
    r['tiles'].update(source=r['sourceId'],site='KATX')
    for f in r['tiles']['frames']:f['siteScans']=[dict(id='KATX',ts=f['ts'])]
    return data


def verify(browser, server, theme, output):
    context=browser.new_context(viewport=dict(width=1024,height=600),has_touch=True)
    context.add_init_script(AUDIT)
    context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(server.url+'/') else route.abort())
    page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto(server.url+'/?tabs=1&theme='+theme);page.locator('.tab[data-screen="s-radar"]').click()
    page.wait_for_function('radarReady().length===8 && radarIntent.owned')
    evidence={}
    # Two immediate presses accumulate from target; a true no-op retains bitmaps.
    evidence['noop']=page.evaluate('''()=>{var old=radarView.loaded.map(f=>f.bitmap);radarSettle();return old.every((b,i)=>radarView.loaded[i].bitmap===b);}''')
    assert evidence['noop']
    page.evaluate('radarZoomChange(-1);radarZoomChange(-1)')
    assert page.evaluate('radarGesture.animation.target.zoom')==6
    page.wait_for_function('radarGesture.state==="idle" && radarCamera.zoom===6')
    page.evaluate('radarZoomChange(2)');page.wait_for_function('radarGesture.state==="idle" && radarCamera.zoom===8')
    page.wait_for_function('radarReady().length===8')
    # Auto is policy even when its resolved camera is identical. Neither a
    # policy-only settle nor durable persistence should discard decoded frames.
    evidence['autoNoop']=page.evaluate('''async()=>{radarView.data.zoomAutoLevel=8;var old=radarView.loaded.map(f=>f.bitmap);radarZoomChange(0);await new Promise(r=>setTimeout(r,250));return radarZoom.auto&&old.every((b,i)=>radarView.loaded[i].bitmap===b);}''')
    assert evidence['autoNoop']
    page.wait_for_function('!radarIntent.ready')
    server.module._camera_persist_timer.join(2)
    assert (server.root/'radar_zoom').read_text().strip()=='auto'
    # Four stuck geography headers occupy real shared admission slots. Polls
    # still report motion and consume payloads while those jobs are draining.
    page.evaluate('radarView.active=false;radarCancelJobs()')
    page.wait_for_function('radarGeoBusy===0&&radarTileBusy===0')
    page.evaluate('''()=>{radarView.active=true;window.v57Fetch=fetch;window.fetch=(u,...a)=>String(u).includes('radar/geo/')?new Promise(()=>{}):v57Fetch(u,...a);radarGeoClear();radarGeoRequest();window.v57OldJobs=[...radarJobs];}''')
    page.wait_for_function('radarGeoBusy===4')
    page.evaluate('radarBegin();poll(true)')
    page.wait_for_function('!polling')
    assert json.loads((server.root/'radar_activity').read_text())['moving']
    page.evaluate('radarGestureCancel()')
    # An automatic handoff (Auto chose the site) stages the target: its caption is
    # in the first RAF and the stuck jobs of the old view are cancelled.
    evidence['caption']=page.evaluate('''async d=>{renderRadar(d);return await new Promise(resolve=>requestAnimationFrame(()=>resolve(document.getElementById('rad-src-cap').textContent)));}''',site_payload(server.data))
    assert evidence['caption'].startswith('Switching to Camano Island radar'),evidence['caption']
    evidence['cancelledOldJobs']=page.evaluate('''()=>{window.fetch=v57Fetch;return v57OldJobs.length===4&&v57OldJobs.every(j=>j.controller.signal.aborted);}''')
    assert evidence['cancelledOldJobs'],page.evaluate('v57OldJobs.map(j=>({aborted:j.controller.signal.aborted,key:j.key}))')
    # Header, body and image decode each exhaust an absolute job deadline. The
    # promise barriers are deterministic; move only each job's clock endpoint.
    evidence['deadlines']=page.evaluate('''async()=>{
      var outcomes=[];
      for(var phase of ['headers','body','decode']){
        var job={};radarJobStart(job);job.deadline=performance.now();
        try{if(phase==='headers')await radarAwait(new Promise(()=>{}),job);
          else if(phase==='body')await radarReadPNG(new Response(new ReadableStream({start(){}})),job);
          else {var original=Image.prototype.decode;Image.prototype.decode=()=>new Promise(()=>{});try{await radarDecode(new Uint8Array([1]),job);}finally{Image.prototype.decode=original;}}
        }catch(e){outcomes.push({phase,aborted:job.controller.signal.aborted});}finally{radarJobEnd(job);}
      }return outcomes;
    }''')
    assert all(row['aborted'] for row in evidence['deadlines']) and len(evidence['deadlines'])==3
    # Stuck old jobs are cancelled at the source boundary; target PNGs are real
    # immutable loopback files. Publish only the matching intent generation.
    page.wait_for_function('!radarIntent.ready')
    old_session=page.evaluate('radarIntent.session')
    page.evaluate('sessionStorage.setItem("radarCamera",JSON.stringify({lat:48,lon:-123,zoom:5,auto:false}))')
    page.reload();page.locator('.tab[data-screen="s-radar"]').click()
    page.wait_for_function('radarIntent.owned&&!radarIntent.ready')
    assert page.evaluate('radarIntent.session')!=old_session
    assert page.evaluate('radarCamera.zoom===8&&radarZoom.auto')
    evidence['reloadPendingAuto']=True
    target=site_payload(server.data)
    target['radar']['intent']=page.evaluate('({session:radarIntent.session,generation:radarIntent.generation})')
    target['radar']['refresh']=dict(state='idle',intent=target['radar']['intent'])
    target['radar']['tiles']['intent']=target['radar']['intent']
    (server.root/'wx.json').write_text(json.dumps(target))
    page.evaluate('d=>renderRadar(d)',target)
    page.wait_for_function('radarView.data.sourceMode==="site" && radarReady().length>=4 && !radarView.pendingSource',timeout=20000)
    first=page.evaluate('radarView.current.stamp')
    page.wait_for_function('s=>radarView.current.stamp!==s',arg=first,timeout=3000)
    evidence['switch']=dict(fourDecoded=True,advancing=True)
    # One irreparable composite cannot monopolize the scratch canvas.
    evidence['compositeEscape']=page.evaluate('''()=>{radarView.active=false;radarCompositeJob={f:radarView.loaded[0],progressAt:performance.now()-4000,done:new Set(),tiles:[],revision:radarCameraRevision};var failed=radarCompositeJob.f;radarHistoryWork();radarView.active=true;return failed.compositeRetry>performance.now()&&radarCompositeJob?.f!==failed;}''')
    assert evidence['compositeEscape']
    # Contract expiry is independent of successful server polls. Invoke the
    # deadline callback with a fake timer instead of sleeping twenty seconds.
    evidence['retry']=page.evaluate('''()=>{var callback,real=setTimeout;window.setTimeout=(fn,ms)=>{if(ms===RAD_SWITCH_MS){callback=fn;return 0;}return real(fn,ms);};try{radarSwitchStart();callback();}finally{window.setTimeout=real;}return {overdue:radarSwitch.overdue,caption:document.getElementById('rad-src-cap').textContent,bitmap:!!radarView.current.bitmap};}''')
    assert evidence['retry']['overdue'] and evidence['retry']['bitmap'] and evidence['retry']['caption'].startswith('Updating view')
    # Basemap stays at the camera's level even when echo source caps lower.
    assert page.evaluate('''()=>{radarView.data.zoomMax=7;radarCamera.zoom=10;return radarLevel()===7&&radarBasemapLevel()===10;}''')
    assert not errors,errors
    evidence['path']=page.evaluate('radarMetrics.path');(output/f'{theme}.json').write_text(json.dumps(evidence,indent=2))
    context.close()
    print('DETERMINISTIC BROWSER PASS',theme,flush=True)


def main():
    p=argparse.ArgumentParser();p.add_argument('--output-dir',type=Path,default=Path('/private/tmp/radar-v57-browser'));args=p.parse_args();args.output_dir.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-gpu'])
        for theme in ('paper','night'):
            with radar_server() as server:verify(browser,server,theme,args.output_dir)
        browser.close()

if __name__=='__main__':main()
