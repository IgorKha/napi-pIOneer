import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from conftest import wait_for
from test_api import anon, client
from test_engine import engine, graph, out
from pioneer.api.app import API
from pioneer.api.live import LiveHub
from pioneer.drivers import sensors
from pioneer.engine import Origin, StartError, validate
from pioneer.nodes import registry


class Device:
    def __init__(self, response):
        self.response = response
        self.writes = []
    def write(self, data): self.writes.append(data)
    def read(self, length): return self.response


def frame(temp=0x6666, rh=0x8000):
    t, h = temp.to_bytes(2, 'big'), rh.to_bytes(2, 'big')
    return t + bytes([sensors.crc8(t)]) + h + bytes([sensors.crc8(h)])


def test_sensor_protocols(monkeypatch):
    delays=[]
    monkeypatch.setattr(sensors.time, 'sleep', delays.append)
    assert sensors.crc8(bytes.fromhex('beef')) == 0x92  # Sensirion datasheet vector
    device=Device(frame())
    assert sensors.sht30(device) == {'temperature':25.0,'humidity':50.0}
    assert device.writes == [bytes.fromhex('2400')]
    assert sensors.sht30(Device(frame(0,0))) == {'temperature':-45.0,'humidity':0.0}
    assert sensors.sht30(Device(frame(65535,65535))) == {'temperature':130.0,'humidity':100.0}
    light=Device(bytes.fromhex('0120'))
    assert sensors.bh1750(light) == {'lux':240.0}
    assert light.writes == [b'\x01',b'\x42',b'\x65',b'\x20']
    assert delays[-1] >= .18 and delays[0] >= .0155


@pytest.mark.parametrize('data',[b'',b'\x00'*5,bytes([frame()[0]^1])+frame()[1:],frame()[:5]+bytes([frame()[5]^1])])
def test_sensor_crc_and_short_reads(data,monkeypatch):
    monkeypatch.setattr(sensors.time,'sleep',lambda _:None)
    with pytest.raises(ValueError,match='CRC'): sensors.sht30(Device(data))
    with pytest.raises(ValueError,match='2 байта'): sensors.bh1750(Device(b''))


class Bus:
    fail=True
    def write(self,addr,data): pass
    def read(self,addr,n):
        if self.fail: raise OSError('NACK')
        return frame()
    def close(self): pass


def test_sensor_recovers_and_keeps_last_value(engine,bench,monkeypatch):
    bus=Bus()
    monkeypatch.setattr(bench,'open_i2c',lambda _:bus)
    g=graph(('sensor','builtin.sht30',{'interval_ms':500}),('tick','builtin.interval',{}, {'period_ms':20}), ('ticks','builtin.counter'),edges=[('tick.tick','ticks.up')])
    engine.start_project('s','sensor',g)
    assert wait_for(lambda: out(engine,'s','sensor.error')=='NACK')
    assert out(engine,'s','sensor.valid') is False
    bus.fail=False
    assert wait_for(lambda: out(engine,'s','sensor.valid') is True)
    assert out(engine,'s','sensor.temperature')==25.0
    assert out(engine,'s','ticks.count') >= 10
    bus.fail=True
    assert wait_for(lambda: out(engine,'s','sensor.valid') is False)
    assert out(engine,'s','sensor.temperature')==25.0
    engine.stop_project('s')
    assert not engine.hw.res.claims


def test_sensor_exclusive_default_address(board,engine,bench,monkeypatch):
    g=graph(('a','builtin.sht30'),('b','builtin.sht30'))
    assert not validate(g,registry(),board).can_start
    assert 'эксклюзивно' in validate(g,registry(),board).text()
    monkeypatch.setattr(bench,'open_i2c',lambda _:Bus())
    engine.start_project('one','one',graph(('a','builtin.sht30')))
    with pytest.raises(StartError): engine.start_project('two','two',graph(('b','builtin.sht30')))
    assert set(engine.hw.res.claims['i2c1@0x44'].owners)=={'project:one'}


def test_live_does_not_poll_without_clients(hw,monkeypatch):
    hub=LiveHub(hw.board,hw); calls=[]
    hub.add_source(lambda: calls.append('drain') or [])
    monkeypatch.setattr(hub,'pin_state',lambda p: calls.append('pin') or {})
    hub.mark('J9-7')
    asyncio.run(hub._flush())
    assert calls==['drain']
    calls.clear()
    class WS:
        async def send_json(self,msg): pass
    hub.clients.add(WS())
    hub.mark('J9-7'); asyncio.run(hub._flush())
    assert 'pin' in calls


def test_system_report_private(client):
    client.app.state.secrets.update({'test_api_key':'never-export-this'})
    r=client.get(f'{API}/system/report')
    assert r.status_code==200 and r.headers['cache-control']=='no-store'
    assert 'attachment' in r.headers['content-disposition']
    assert 'never-export-this' not in r.text and 'test_api_key' not in r.text
    assert r.json()['hal']=='bench' and r.json()['projects']['running']==0
    assert r.json()['uptime_seconds']>=0


def test_diagnostics_and_templates_require_login(anon):
    for path in ['/system','/system/report','/project-templates']:
        assert anon.get(API+path).status_code==401


def test_all_templates_start_and_remain_stopped_copies(client):
    templates=client.get(f'{API}/project-templates').json()['templates']
    assert len(templates)==3
    for t in templates:
        r=client.post(f'{API}/projects',json={'name':t['name'],'graph':t['graph']})
        assert r.status_code==200,r.text
        p=r.json();pid=p['id']
        assert not p['autostart']
        detail=client.get(f'{API}/projects/{pid}').json()
        assert detail['status']['state']=='stopped' and detail['validation']['can_start']
        result=client.post(f'{API}/projects/{pid}/start',json={})
        assert result.status_code==200,result.text
        assert not client.app.state.hw.res.claims
        if t['id']=='counter':
            client.app.state.projects.ui(pid,'button','click')
            assert wait_for(lambda: client.app.state.projects.engine.call(lambda: out(client.app.state.projects.engine,pid,'counter.count'))==1)
        client.post(f'{API}/projects/{pid}/stop',json={})


def test_sensor_sdk_in_actual_worker(tmp_path,hw,bench,monkeypatch):
    from pioneer.scripts import ScriptManager
    from test_scripts import log_text
    class SensorBus(Bus):
        fail=False
        def read(self,addr,n): return frame() if addr==0x44 else bytes.fromhex('0120')
    monkeypatch.setattr(bench,'open_i2c',lambda _:SensorBus())
    sm=ScriptManager(tmp_path,hw)
    try:
        sid=sm.create('Sensors','from pioneer.sdk import SHT30, BH1750\nprint(SHT30().read())\nprint(BH1750().read())\n').id
        sm.start(sid)
        assert wait_for(lambda: '240.0' in log_text(sm,sid),5),log_text(sm,sid)
        assert '25.0' in log_text(sm,sid) and '50.0' in log_text(sm,sid)
        assert wait_for(lambda: not hw.res.claims,5)
    finally: sm.stop_all()


def test_slow_conversion_does_not_block_graph(engine,bench,monkeypatch):
    import threading
    started,release=threading.Event(),threading.Event()
    class WaitingBus(Bus):
        fail=False
        def read(self,addr,n):
            started.set()
            assert release.wait(1)
            return frame()
    monkeypatch.setattr(bench,'open_i2c',lambda _:WaitingBus())
    g=graph(('sensor','builtin.sht30'),('tick','builtin.interval',{}, {'period_ms':10}),('counter','builtin.counter'),edges=[('tick.tick','counter.up')])
    try:
        engine.start_project('slow','slow',g)
        assert started.wait(1)
        assert wait_for(lambda: out(engine,'slow','counter.count') >= 5,.25)
        assert out(engine,'slow','sensor.valid') is False
    finally: release.set()
    assert wait_for(lambda: out(engine,'slow','sensor.valid') is True)


def test_low_resource_warnings(client,monkeypatch):
    from pioneer.api import system
    monkeypatch.setattr(system,'proc_values',lambda p: {'VmRSS':121*1024**2,'MemAvailable':63*1024**2,'MemTotal':414*1024**2})
    monkeypatch.setattr(system.os,'statvfs',lambda p: SimpleNamespace(f_bavail=1,f_frsize=4096,f_blocks=1000))
    assert len(client.get(API+'/system').json()['warnings'])==3


def test_linux_proc_empty_features_do_not_hide_rss(tmp_path):
    from pioneer.api.system import proc_values
    status=tmp_path/'status'
    status.write_text('Name:	python3\nVmRSS:	70176 kB\nx86_Thread_features:	\n')
    assert proc_values(status)['VmRSS']==70176*1024
    assert proc_values(tmp_path/'missing')=={}


def test_sensor_unassigned_address_is_incomplete_without_crash(board):
    g=graph(('sensor','builtin.sht30',{'addr':None}))
    report=validate(g,registry(),board)
    assert not report.can_start and not report.errors
    assert any(i.param=='addr' for i in report.incomplete)
    from pioneer.engine import Graph
    assert validate(Graph.from_json({'nodes':[{'id':'s','type':'builtin.sht30@1','params':{}}]}),registry(),board).can_start


def test_led_telemetry_is_four_hz_and_owner_change_immediate(hw,monkeypatch):
    from pioneer.api import live
    now=[10.0];reads=[]
    monkeypatch.setattr(live,'time',SimpleNamespace(monotonic=lambda:now[0]))
    hub=LiveHub(hw.board,hw)
    class WS:
        async def send_json(self,m): pass
    hub.clients.add(WS())
    monkeypatch.setattr(hub,'led_states',lambda: reads.append(now[0]) or {})
    for n in range(20):
        now[0]=10+n*.05
        asyncio.run(hub._flush())
    assert len(reads)==4
    hub.mark_leds();asyncio.run(hub._flush())
    assert len(reads)==5
