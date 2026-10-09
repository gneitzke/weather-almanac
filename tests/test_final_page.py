"""Production page state sequences, with no browser or network dependency."""
from tests.test_radar_buffer_page import run_page


def test_late_camera_ack_does_not_discard_already_sharpened_loop():
    run_page(r'''
radarView.paused=true;radarView.nextAt=0;radarView.cycle=[];
radarCamera={...radarCamera,lon:radarCamera.lon+.001};
radarRetarget();radarView.loaded.forEach(decode);radarUpdateReady();
assert.equal(radarView.holdingWindow,false);
const held=radarView.loaded.slice(),rasters=held.map(f=>f.bitmap);
const r=manifest();r.tiles.camera={...radarCamera};
renderRadar({radar:r,ts:100901});
assert.ok(held.every(f=>radarView.loaded.includes(f)),'camera acknowledgement rebuilt an already settled window');
assert.ok(rasters.every(b=>b.closes===0));
assert.equal(radarView.holdingWindow,false);
''')


def test_pan_during_source_stage_preserves_target_manifest():
    run_page(r'''
renderRadar({radar:manifest('b'),ts:100900});
const pending=radarView.pendingSource,rasters=pending.frames.slice(-3).map(f=>decode(f).bitmap);
radarCamera={...radarCamera,lon:radarCamera.lon+.01};
radarRetarget();
assert.ok(radarView.pendingSource,'pan discarded the target source, leaving old-source acquisitions');
assert.equal(radarView.pendingSource.data.sourceId,'b');
assert.ok(rasters.every(b=>b.closes===1));
assert.ok(radarView.pendingSource.frames.every(f=>!f.bitmap));
''')


def test_sustained_slow_raf_finishes_blends_and_advances_the_scan():
    run_page(r'''
radarView.nextAt=clock;
const initial=radarView.current;
radarFrame(clock);
const target=radarView.blend.to;
// Continuous load on a Pi can deliver every RAF after the 350ms step deadline.
for(let i=0;i<4;i++){clock+=500;radarFrame(clock);}
assert.notEqual(radarView.current,initial,'each late RAF restarted the same blend at alpha zero');
assert.ok(radarView.current.ts>=target.ts);
assert.ok(!radarView.blend||radarView.blend.to!==target,'loop never advanced beyond the first target');
''')


def test_source_camera_ack_keeps_newly_decoded_target_plates():
    run_page(r'''
renderRadar({radar:manifest('b'),ts:100900});
radarCamera={...radarCamera,lon:radarCamera.lon+.01};radarRetarget();
const held=radarView.pendingSource.frames.slice(-3).map(decode),rasters=held.map(f=>f.bitmap);
renderRadar({radar:manifest('b'),ts:100901});
assert.ok(held.every(f=>radarView.pendingSource.frames.includes(f)),'coverage ack discarded target plates for the settled camera');
assert.ok(rasters.every(b=>b.closes===0));
radarRelease();assert.equal(live.size,0);
''')
