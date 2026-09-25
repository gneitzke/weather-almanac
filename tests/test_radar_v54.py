"""Fable v5.2 N1–N6: computed colour guarantees and primary scan cadence."""
import copy
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from lib import almanac_emit as ae, radar_palette as rp
from tests.test_radar_v32 import contrast, luminance, indexed, TRANSLUCENT_PALETTE
from tests.test_radar_hybrid import hybrid, png  # noqa: F401


def test_n1_computed_contrast_all_26():
    minima = []
    assert len(rp._RADAR_LUT) == 26
    for ground, floor in [('EBE6DB',2), ('F2EDE2',2), ('0B0D11',3)]:
        ratios = [contrast(c[:3], bytes.fromhex(ground)) for _,c in rp._RADAR_LUT]
        assert all(r >= floor for r in ratios)
        minima.append(round(min(ratios),2))
    assert minima == [2.03,2.17,3.13]


def test_n2_n3_ramp_shape_and_greens():
    b = rp._RADAR_RAMP['bands']
    assert rp._RADAR_RAMP['id'] == 'almanac-reflectivity-v3'
    assert rp.REMAP_REVISION == 'native-v5.2-1'
    assert len(b)==9 and (b[0]['lo'],b[-1]['hi'])==(10,75)
    assert [v['hi']-v['lo'] for v in b] == [10,5,10,5,5,5,10,10,5]
    assert [(v['start'],v['end']) for v in b[:3]] == [('#89AB92','#43A05D'),('#43A05D','#209143'),('#088A34','#11672D')]
    assert b[3:] == [dict(lo=lo,hi=hi,start=a,end=z) for lo,hi,a,z in [
        (35,40,'#C79C14','#B0870D'),(40,45,'#E5871A','#D2700F'),
        (45,50,'#DE5C17','#C94C0C'),(50,60,'#DD4530','#BC2A1A'),
        (60,70,'#CE4E88','#A9389B'),(70,75,'#8A46C2','#8A46C2')]]
    assert [d for d,c in rp._RADAR_LUT] == [10+2.5*i for i in range(26)]
    for _,(r,g,b,a) in rp._RADAR_LUT[:10]:
        assert g>b and g>=r and a==255


def test_n0b_remap_every_lut_entry():
    mapped = rp.remap(indexed('iem-nexrad-n0b'),'iem-nexrad-n0b',TRANSLUCENT_PALETTE)
    for dbz,rgba in rp._RADAR_LUT:
        assert mapped.getpixel((int((dbz+33)*2),0)) == rgba


def stamps(gaps, end=1800000000):
    result=[end-sum(gaps)*60]
    for gap in gaps: result.append(result[-1]+gap*60)
    return result


CADENCES = [([4,4,4],'precipitation',4,False),([4,9,4],'precipitation',4,False),
    ([6,6,7],'precipitation',6,False), # M2 threshold wins over conflicting N5 typo.
    ([7,7,7],None,7,False),([10,10,10],'clear-air',10,False),
    ([10,10,22],'clear-air',10,False),([16,16,16],None,16,True),
    ([20,20,20],None,20,True),([4],None,4,False),([4,10],None,7,False),([],None,None,False)]


@pytest.mark.parametrize('gaps,mode,minutes,slow', CADENCES)
def test_n5_cadence(gaps,mode,minutes,slow):
    inferred = ae._radar_scan_cadence(stamps(gaps))
    assert inferred == dict(scan_cadence_sec=minutes*60 if minutes else None,
        scan_mode=mode,scan_mode_source='cadence' if mode else None,scanning_slowly=slow)
    snap=ae._RADAR_NONE._replace(**inferred)
    payload=ae.AlmanacEmitter._radar_payload(snap,1800000000,timezone.utc)
    assert payload['scanCadenceSec']==inferred['scan_cadence_sec']
    assert payload['scanMode']==mode and payload['scanningSlowly']==slow
    assert payload['scanModeSource']==('cadence' if mode else None)
    assert 'vcp' not in payload


@pytest.mark.parametrize('seconds,mode,slow',[(390,'precipitation',False),(391,None,False),
    (539,None,False),(540,'clear-air',False),(900,'clear-air',False),(901,None,True)])
def test_cadence_threshold_boundaries(seconds,mode,slow):
    got=ae._radar_scan_cadence([0,seconds,2*seconds,3*seconds])
    assert (got['scan_mode'],got['scanning_slowly'])==(mode,slow)
    assert ae._radar_scan_cadence([0,10000,10240,10480,10720])['scan_cadence_sec']==240


@pytest.mark.parametrize('primary', ['KATX','KLGX'])
def test_n6_primary_listing_controls_mode(make_emitter,hybrid,tmp_path,monkeypatch,primary):
    # This local transport models IEM ridge layers, not Level III products.
    monkeypatch.setattr(ae.AlmanacEmitter, '_radar_level3_down', lambda self: True)
    # Exchange geographic positions; both listings/tiles remain available.
    sites={i:ae._NEXRAD_SITES[i] for i in ('KATX','KLGX')}
    if primary=='KLGX': sites['KATX'],sites['KLGX']=sites['KLGX'],sites['KATX']
    monkeypatch.setattr(ae,'_NEXRAD_SITES',sites)
    original=ae.RadarSession.open
    def fetch(self,req,timeout):
        if 'operation=list' in req.full_url:
            site=parse_qs(urlsplit(req.full_url).query)['radar'][0]
            times=stamps([4,4,4] if site=='ATX' else [10,10,10],hybrid.latest)
            return io.BytesIO(json.dumps(dict(scans=[dict(ts=datetime.fromtimestamp(t,timezone.utc).isoformat()) for t in times])).encode())
        if 'ridge::' in req.full_url: return io.BytesIO(png())
        return original(self,req,timeout)
    monkeypatch.setattr(ae.RadarSession,'open',fetch)
    (tmp_path/'radar_source').write_text('site');(tmp_path/'radar_zoom').write_text('7')
    e=make_emitter();e._do_radar();r=e._build_payload()['radar']
    assert r['siteId']==primary
    assert r['scanMode']==('precipitation' if primary=='KATX' else 'clear-air')
    assert r['scanCadenceSec']==(240 if primary=='KATX' else 600)
    assert r['frameCount']==4
    # A partially acquired history must not change listing-derived cadence.
    snap=e._radar_result._replace(frames=e._radar_result.frames[-1:])
    partial=e._radar_payload(snap,hybrid.now,timezone.utc)
    assert partial['scanCadenceSec']==r['scanCadenceSec'] and partial['scanMode']==r['scanMode']


def test_old_palette_tiles_cannot_enter_current_inventory(make_emitter,monkeypatch):
    current=ae._radar_render_revision()
    old=copy.deepcopy(rp._RADAR_RAMP)
    old['id']='almanac-reflectivity-v1'
    for band,(a,b) in zip(old['bands'],[('#8AA3C6','#4E79B4'),('#2E93A8','#227F92'),('#3FA65E','#2A8448')]):
        band.update(start=a,end=b)
    with monkeypatch.context() as m:
        m.setattr(ae,'_RADAR_RAMP',old)
        ae._radar_revision_digest.cache_clear()
        # Same remap revision: prove the ramp itself changes the tile identity.
        prior=ae._radar_render_revision()
        assert prior!=current
        path=ae._radar_tile_path('iem-mrms-lcref',None,1800000000,8,40,89)
        path.parent.mkdir(parents=True);path.write_bytes(png())
    ae._radar_revision_digest.cache_clear()
    assert ae._radar_render_revision()==current
    assert ae._radar_tile_path('iem-mrms-lcref',None,1800000000,8,40,89)!=path
    e=make_emitter();e._radar_start_inventory();assert e._radar_cache_ready.wait(5)
    assert not e._radar_disk_inventory


@pytest.mark.parametrize('gaps', [[],[4]])
def test_short_primary_listing_payload(make_emitter,hybrid,tmp_path,monkeypatch,gaps):
    # This local transport models IEM ridge layers, not Level III products.
    monkeypatch.setattr(ae.AlmanacEmitter, '_radar_level3_down', lambda self: True)
    monkeypatch.setattr(ae,'_NEXRAD_SITES',{'KATX':ae._NEXRAD_SITES['KATX']})
    original=ae.RadarSession.open
    def fetch(self,req,timeout):
        if 'operation=list' in req.full_url:
            return io.BytesIO(json.dumps(dict(scans=[dict(ts=datetime.fromtimestamp(t,timezone.utc).isoformat())
                for t in stamps(gaps,hybrid.latest)])).encode())
        if 'ridge::' in req.full_url: return io.BytesIO(png())
        return original(self,req,timeout)
    monkeypatch.setattr(ae.RadarSession,'open',fetch)
    (tmp_path/'radar_source').write_text('site')
    e=make_emitter();e._do_radar();r=e._build_payload()['radar']
    assert r['sourceMode']=='site'
    assert r['scanCadenceSec']==(240 if gaps else None)
    assert r['scanMode'] is r['scanModeSource'] is None
    assert r['latestOnly']==(not gaps) and not r['scanningSlowly']
