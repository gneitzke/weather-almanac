"""Missing radar tiles never add pigment; used by the two-theme v4.4 runner."""


def acquisition_pixels(page, theme, output):
    page.route('**/wx.json*', lambda route: route.abort())
    result = page.evaluate('''async()=>{
      radarView.active=false;radarRelease();radarGestureCancel();
      const r=radarView.data,c=new OffscreenCanvas(256,256),cx=c.getContext('2d'),x=document.getElementById('rad-echo').getContext('2d');
      const meta={opaquePixels:0,unmatchedPixels:0,ambiguousPixels:0},cases=[];
      const pixels=()=>x.getImageData(0,0,956,490).data;
      const marked=()=>{const a=pixels();let n=0;for(let i=3;i<a.length;i+=4)if(a[i])n++;return n;};
      for(const source of ['iem-nexrad-n0b','iem-mrms-lcref','rainviewer']){
        r.sourceId=source;r.sites=[];radarCamera={lat:48,lon:source==='iem-mrms-lcref'?-130:-122.33,zoom:7};
        const f={stamp:'200001010000',ts:946684800,at:'17:12',siteScans:source==='iem-nexrad-n0b'?[{id:'KATX',ts:946684800}]:[]};
        radarView.current=radarView.good=f;radarView.loaded=[f];
        const tiles=radarTileSet(radarCamera,7),xs=tiles.map(t=>t.x),ys=tiles.map(t=>t.y),grid={x0:Math.min(...xs),y0:Math.min(...ys),w:Math.max(...xs)-Math.min(...xs)+1,h:Math.max(...ys)-Math.min(...ys)+1};
        r.tiles={...r.tiles,z:7,grid,newest:{stamp:f.stamp,expectedMask:((1n<<BigInt(grid.w*grid.h))-1n).toString(16)}};
        // Satisfy the former geography gate even at the MRMS domain edge.
        for(const t of tiles){const key=radarGeoKey(7,t.x,t.y);if(!radarGeoTiles.has(key))radarGeoTiles.set(key,{bitmap:await createImageBitmap(c)});}
        for(const fraction of [0,.2,.4,.6,1]){
          radarTiles.forEach(t=>t.bitmap.close());radarTiles.clear();
          for(const t of tiles.slice(Math.ceil(tiles.length*fraction))){const key=radarTileKey(f,7,t.x,t.y);radarTiles.set(key,{key,z:7,x:t.x,y:t.y,bitmap:await createImageBitmap(c),meta,hasEcho:false,sites:f.siteScans,partial:fraction===.4});}
          radarEchoPaint(f);const early=marked();await new Promise(resolve=>setTimeout(resolve,450));radarEchoPaint(f);
          cases.push({source,fraction,early,late:marked()});
        }
        // Unknown and unrequested masks must also leave every pixel untouched.
        for(const mask of [null,'0']){r.tiles.newest.expectedMask=mask;radarEchoPaint(f);cases.push({source,mask,late:marked()});}
        const plate=new OffscreenCanvas(956,490);plate.getContext('2d');f.bitmap=plate.transferToImageBitmap();f.camera={...radarCamera};f.ready=true;radarEchoPaint(f);cases.push({source,cached:true,late:marked()});f.bitmap.close();delete f.bitmap;plate.width=plate.height=0;
      }
      // Reviewable partial acquisition: real basemap, fixture echoes in acquired
      // cells, one visible empty cell, and an honest incomplete loop inventory.
      radarRelease();r.sourceId='iem-mrms-lcref';r.sourceMode='mosaic';r.siteId=null;r.sites=[];
      radarCamera={...r.center,zoom:7};radarBasePaint();
      const f={stamp:'200001010000',ts:946684800,at:'17:12',siteScans:[]};
      radarView.current=radarView.good=f;radarView.loaded=[f];r.observedTs=f.ts;r.observedAt=f.at;
      const tiles=radarTileSet(radarCamera,7),hole=tiles[Math.floor(tiles.length/2)];
      cx.fillStyle='#8AA3C6';cx.beginPath();cx.ellipse(120,110,70,45,-.5,0,Math.PI*2);cx.fill();
      for(const t of tiles.filter(t=>t!==hole)){const key=radarTileKey(f,7,t.x,t.y);radarTiles.set(key,{key,z:7,x:t.x,y:t.y,bitmap:await createImageBitmap(c),meta,hasEcho:true});}
      const pigment=cx.getImageData(0,0,256,256).data,allowed=new Set();for(let i=0;i<pigment.length;i+=4)allowed.add(Array.from(pigment.slice(i,i+4)).join(','));
      radarEchoPaint(f);const before=pixels();await new Promise(resolve=>setTimeout(resolve,650));radarEchoPaint(f);const after=pixels();
      const p=radarWorldPoint(radarCamera.lat,radarCamera.lon,7);let holePixels=0,unexpected=0;
      for(let y=0;y<490;y++)for(let px=0;px<956;px++){
        const i=(y*956+px)*4,a=after[i+3];
        if(a&&!allowed.has(Array.from(after.slice(i,i+4)).join(',')))unexpected++;
        if(a&&px>=Math.round(478+hole.x*256-p[0])&&px<Math.round(478+(hole.x+1)*256-p[0])&&y>=Math.round(245+hole.y*256-p[1])&&y<Math.round(245+(hole.y+1)*256-p[1]))holePixels++;
      }
      radarView.paused=false;radarView.refresh={state:'newest'};radarIntent.postedAt=Date.now()-1000;
      radarUpdateReady();radarView.active=true;radarLoopSync();radarView.active=false;radarState();radarSourceRender();radarNoteRender();radarOverlayBuild();radarZoomRender();
      const still={unchanged:before.every((v,i)=>v===after[i]),marked:marked(),holePixels,unexpected,read:document.getElementById('rad-frame-time').textContent,note:document.getElementById('rad-note').textContent,aria:document.getElementById('rad-plate').getAttribute('aria-label')};
      c.width=c.height=0;return {cases,still};
    }''')
    assert all(c.get('early', 0) == 0 and c['late'] == 0 for c in result['cases']), result
    still = result['still']
    assert still['unchanged'] and still['marked'] > 0 and still['holePixels'] == 0 and still['unexpected'] == 0, still
    assert still['read'] == 'Buffering · 0 of 8' and still['note'] == 'Refreshing · newest frame' and still['aria'] == 'Reflectivity radar', still
    assert page.locator('#rad-plate [id*="hatch"], #rad-plate [class*="hatch"], #rad-plate pattern').count() == 0
    page.wait_for_function("getComputedStyle(document.getElementById('rad-note')).opacity==='1'")
    page.screenshot(path=str(output/f'mid-acquisition-{theme}.png'))
    return result
