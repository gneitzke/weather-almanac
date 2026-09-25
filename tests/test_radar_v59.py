"""Inactivity hedges reset on every raw read, not merely the response headers."""
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from lib import radar_fetch as fetch


@pytest.mark.parametrize('stall', [False, True])
def test_progress_resets_hedge_timer(monkeypatch, stall):
    clock = [0.]
    primary = []
    claims, attempts = [], []
    # Header fragments at 1s/1.5s, body chunks at 2.5s/3s. Header activity
    # alone and ongoing bodies both defer admission; stall begins after 3s.
    progress = [1., 1.5, 2.5, 3.]
    if not stall:
        progress += [4., 5.]
    events = iter(progress + [5.5])
    event = [next(events)]

    class Pool:
        def __init__(self, **kw): pass
        def submit(self, fn, control, retry):
            f = Future()
            attempts.append(control)
            if retry:
                assert control.stall_hedge and control.hedged
                f.set_result(b'hedge')
            else:
                primary.append((f, control))
            return f
        def shutdown(self, wait): pass

    def wait(pending, timeout, return_when):
        done = {f for f in pending if f.done()}
        if done:
            return done, set(pending)-done
        end = clock[0]+timeout
        while event[0] <= end:
            clock[0] = event[0]
            f, control = primary[0]
            if event[0] == 5.5:
                f.set_result(b'primary')
                return {f}, set(pending)-{f}
            control.progress()
            event[0] = next(events)
        clock[0] = end
        return set(), set(pending)

    def claim():
        claims.append(clock[0])
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(fetch, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(fetch, 'ThreadPoolExecutor', Pool)
    monkeypatch.setattr(fetch, 'wait', wait)
    result = fetch.tile_race(lambda *a: None, 6, 2, claim, lambda n: None)
    assert result == (b'hedge' if stall else b'primary')
    assert claims == ([5.] if stall else [])
    assert len(attempts) == (2 if stall else 1)


@pytest.mark.parametrize('state,expected', [
    ('idle', 'Refreshing · frame 4 of 8'),
    ('history', 'Refreshing · frame 4 of 8'),
    ('failed', "Couldn't refresh · showing 12:00"),
])
def test_corner_note_keeps_page_acquisition_after_wrap(state, expected):
    import json
    import subprocess
    from tests.test_radar_v46 import function
    functions = '\n'.join(function(n) for n in ('isNum', 'radarPendingRetry', 'radarFallbackText', 'radarNoteRender'))
    script = '''
const node={dataset:{}},$=()=>node,radarIntent={postedAt:0},radarSwitch=null,radarSource={};
const radarFrameLabel=()=>'12:00';
const radarView={data:{frameCount:8},current:{bitmap:{}},holdingWindow:false,
 loaded:Array.from({length:8},(_,i)=>({bitmap:i<4?{}:null})),refresh:{state:STATE}};
''' .replace('STATE', json.dumps(state)) + functions + '\nradarNoteRender();console.log(node.textContent);'
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == expected


@pytest.mark.parametrize('gesture', ['idle', 'gesturing', 'inertia'])
def test_stationary_gesture_still_paints_retained_composite(gesture):
    # Holding a pointer still must not make a history scan fall back to native
    # tiles that have already left the LRU when its next playback tick arrives.
    import json
    import subprocess
    from pathlib import Path
    html = Path('design/almanac/console_live.html').read_text()
    function = html[html.index('  function radarEchoPaint('):html.index('  function radarHistoryWork(')]
    script = '''
const radarCamera={lat:47,lon:-122,zoom:8},radarGesture={state:STATE},radarView={};
const ctx={clearRect(){},drawImage(b){if(b!==f.bitmap)throw Error('wrong bitmap');this.drawn=true;}};
const $=()=>({getContext:()=>ctx,setAttribute(){}});
const f={bitmap:{},camera:{...radarCamera},ready:true,hasEcho:true};
''' .replace('STATE', json.dumps(gesture)) + function + '\nradarEchoPaint(f);console.log(ctx.drawn);'
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == 'true'
