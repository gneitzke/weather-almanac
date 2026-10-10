"""Offline worker profile; output directory then optional baseline checkout path."""
import sys, tempfile, cProfile, pstats, time, json, threading
from pathlib import Path
sys.path.insert(0, sys.argv[2] if len(sys.argv)>2 else str(Path(__file__).resolve().parents[1]))
from tests import conftest
import pytest
from tests.test_radar_hybrid import hybrid
from tests.test_radar_v3 import multisite
from lib import almanac_emit as ae
from types import SimpleNamespace
from tests.fixtures.config import make_config
out=Path(sys.argv[1]);out.mkdir(exist_ok=True,parents=True)
with tempfile.TemporaryDirectory() as d, pytest.MonkeyPatch.context() as m:
 p=Path(d);h=hybrid.__wrapped__(p,m);s=multisite.__wrapped__(h,m,p)
 s.colors['KFAR']=(12,145,16,255)
 s.scans={k:[h.latest-300*i for i in reversed(range(8))] for k in s.scans}
 app=SimpleNamespace(config=make_config(),obsParser=SimpleNamespace(api_data={}))
 e=ae.AlmanacEmitter(SimpleNamespace(app=app,Obs={},Met={},Astro={},Sager={}),output_path=str(p/'wx.json'))
 h.view()
 for mode in ('site','mosaic','site'):
  h.pin(mode);e.radar._request_times.clear();e.radar._acquire()
 for _ in range(4):
  e.radar._request_times.clear();e.radar._acquire(intent_triggered=True)
 assert e.radar._result.source_id=='iem-nexrad-n0b'
 assert sum(f['complete'] for f in e.radar._frames[-8:])==8
 def work():
  pr=cProfile.Profile();cpu=time.thread_time();wall=time.perf_counter();pr.enable()
  for i in range(10):
   e.radar._request_times.clear();e.radar._acquire(intent_triggered=True)
  pr.disable();result=dict(cpu=time.thread_time()-cpu,wall=time.perf_counter()-wall,files=len(list((p/'radar'/'t').rglob('*.png'))))
  pr.dump_stats(str(out/'worker.prof'))
  with (out/'worker.txt').open('w') as f:pstats.Stats(pr,stream=f).sort_stats('cumulative').print_stats(40)
  # Profile the actual settled watcher/discovery path at the production 100ms
  # watch cadence for ten seconds. No provider pass is due during this interval.
  from tests.test_emitter_lifecycle import FakeClock
  clock=FakeClock();m.setattr(ae,'Clock',clock)
  e._runtime.running=True;e.radar._was_viewed=True;e.radar._warm_pending=False
  e.radar._acquisition_pending=False;e.radar._view_pending=False
  initial_calls=len(h.calls)+len(s.calls)
  idle=cProfile.Profile();idle_cpu=time.thread_time();idle_wall=time.perf_counter();idle.enable()
  for _ in range(100):
   e.radar._check_zoom();clock.advance(.1);time.sleep(.1)
  idle.disable();idle_elapsed=time.perf_counter()-idle_wall
  result.update(idleWallSec=idle_elapsed,idleCpuSec=time.thread_time()-idle_cpu,
      idleProviderRequests=len(h.calls)+len(s.calls)-initial_calls)
  idle.dump_stats(str(out/'idle.prof'))
  with (out/'idle.txt').open('w') as f:pstats.Stats(idle,stream=f).sort_stats('cumulative').print_stats(25)
  # CPU utilization is measured without profiler hooks in a separate window;
  # keep the profiled observation too, so instrumentation cost is visible.
  plain_cpu=time.thread_time();plain_wall=time.perf_counter()
  for _ in range(100):
   e.radar._check_zoom();clock.advance(.1);time.sleep(.1)
  result.update(idleUnprofiledWallSec=time.perf_counter()-plain_wall,
      idleUnprofiledCpuSec=time.thread_time()-plain_cpu)
  e.stop()
  (out/'summary.json').write_text(json.dumps(result,indent=2));print(result)
 t=threading.Thread(target=work);t.start();t.join()
 if e.radar._session:e.radar._session.close()
