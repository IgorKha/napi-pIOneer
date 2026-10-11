"""Remaining phase contracts: worker I/O, portable modules, bounded analyses and hardware policy."""
import asyncio
import base64
import io
import json
import queue
import threading
import time
from pathlib import Path

import pytest
from PIL import Image
from conftest import wait_for
from test_api import anon, client
from pioneer.api.app import API
from pioneer.codetree import CodeTree,TreeError
from pioneer.config import REPO_ROOT,Config
from pioneer.nodes import registry
from pioneer.projects import ProjectManager
from pioneer.modules import Modules
from pioneer.userblocks import BlockManager
from pioneer.ai.attachments import validate,message
from pioneer.dependencies import parse,install,site_for


def test_peripheral_module_worker_and_simulator(tmp_path,hw,bench):
    reg=registry().copy();modules=Modules(tmp_path,reg,bench)
    data=CodeTree(REPO_ROOT/'modules/examples/register').export()
    with pytest.raises(TreeError):modules.install(data,False)
    item=modules.install(data,True)
    assert item['types']==['module.register_demo.read@1']
    assert not modules.install(data,True)['changed']
    model=bench.add(item['model_kind'],'register',bus=1,addr=72,value=1234)
    pm=ProjectManager(tmp_path,hw,reg)
    try:
        p=pm.create('Module test')
        pm.apply_ops(p.id,[{'op':'add_node','id':'read','type':item['types'][0]}, {'op':'add_node','id':'button','type':'builtin.ui_button'}, {'op':'connect','from':['button','click'],'to':['read','when']}])
        pm.start(p.id)
        sample=lambda:pm.engine.call(lambda:pm.engine.runtimes[p.id].sample('read','value').value)
        assert sample()==1234
        model.action('set',{'value':4567})
        pm.ui(p.id,'button','click')
        assert sample()==4567
        assert 'i2c1@0x48' in hw.res.claims
        pm.stop(p.id)
        assert not hw.res.claims
    finally:
        pm.close();bench.remove('register');modules.remove('register_demo',1)


def test_project_bundle_stopped_roundtrip_and_collision(client,tmp_path):
    b=client.post(f'{API}/blocks',json={'name':'Bundle block'}).json()
    pm=client.app.state.projects
    p=pm.create('Portable');pm.apply_ops(p.id,[{'op':'add_node','id':'h','type':'user.hysteresis'}]);pm.update_meta(p.id,autostart=True)
    package=client.get(f'{API}/projects/{p.id}/bundle.zip')
    assert package.status_code==200
    assert client.post(f'{API}/projects/import.zip',content=package.content).status_code==422
    response=client.post(f'{API}/projects/import.zip?confirmed=true',content=package.content)
    assert response.status_code==200,response.text
    other=response.json();assert other['id']!=p.id and not other['autostart']
    assert len(client.app.state.blocks.list())==1
    assert client.get(f'{API}/projects/{other["id"]}').json()['status']['state']=='stopped'
    original=client.app.state.blocks.get(b['id'])['code']
    client.app.state.blocks.save(b['id'],original+'\n# changed\n')
    before=len(pm.list())
    assert client.post(f'{API}/projects/import.zip?confirmed=true',content=package.content).status_code==422
    assert len(pm.list())==before


def test_uart_fanout_permissions_and_partial_release(hw,monkeypatch):
    class Port:
        baud=9600
        def __init__(self):self.q=queue.Queue();self.written=[];self.closed=False
        def read(self,n,timeout):
            try:return self.q.get(timeout=timeout)
            except queue.Empty:return b''
        def write(self,data):self.written.append(data)
        def close(self):self.closed=True
    port=Port();monkeypatch.setattr(hw.hal,'open_uart',lambda *a,**k:port)
    a=hw.uart('a','A','uart1',direction='rx');b=hw.uart('b','B','uart1',direction='rx')
    tx=hw.uart('a','A','uart1',direction='tx')
    port.q.put(b'hello');assert a.read(5,1)==b'hello' and b.read(5,1)==b'hello'
    with pytest.raises(ValueError):a.write(b'bad')
    with pytest.raises(ValueError):hw.uart('a','A','uart1',baud=19200,direction='rx')
    assert 'a' in hw.res.claims['uart1:rx'].owners
    hw.release('a','uart1:rx');tx.write(b'ok');assert port.written==[b'ok']
    assert tx in tx.hub.subscriptions and a not in tx.hub.subscriptions
    hw.release('b');hw.release('a');assert port.closed


def test_pwm_sysfs_change_order_and_bounds(tmp_path):
    from pioneer.hal.hardware_pwm import HardwarePwm,settings
    assert settings(25000,.25)==(40000,10000)
    for f,d in [(0,.5),(100,-1),(float('nan'),.5),(2_000_000,.5)]:
        with pytest.raises(ValueError):settings(f,d)
    chip=tmp_path;path=chip/'pwm0';path.mkdir()
    for name in ('period','duty_cycle','enable','polarity'):(path/name).write_text('0')
    (chip/'unexport').touch()
    pwm=HardwarePwm.__new__(HardwarePwm)
    pwm.path,pwm.chip,pwm.freq,pwm.duty,pwm.closed,pwm.lock=path,chip,100.,0.,False,threading.RLock()
    writes=[];real=pwm._write
    pwm._write=lambda k,v:(writes.append((k,v)),real(k,v))
    pwm.set(25000,.5)
    assert writes[:3]==[('enable',0),('duty_cycle',0),('period',40000)]
    assert (path/'duty_cycle').read_text()=='20000'
    pwm.close();assert pwm.closed and (chip/'unexport').read_text()=='0'


def test_pwm_fresh_export_needs_period_before_other_settings(tmp_path, monkeypatch):
    from pioneer.hal.hardware_pwm import HardwarePwm
    (tmp_path/'export').touch();(tmp_path/'unexport').touch()
    original=Path.write_text;writes=[]
    def write(path, value, *args, **kwargs):
        if path==tmp_path/'export':
            channel=tmp_path/'pwm0';channel.mkdir()
            for name in ('period','duty_cycle','enable','polarity'):
                original(channel/name,'0')
        elif path.parent==tmp_path/'pwm0':
            if path.name!='period' and not int((path.parent/'period').read_text()):
                raise OSError(22,'period must be nonzero')
            writes.append((path.name,str(value)))
        return original(path,value,*args,**kwargs)
    monkeypatch.setattr(Path,'write_text',write)
    pwm=HardwarePwm(tmp_path)
    try:
        assert writes[0]==('period','10000000')
        pwm.set(25000,.5)
        assert (pwm.path/'duty_cycle').read_text()=='20000'
    finally:pwm.close()


def test_linux_gpio_bank_identity_survives_reordered_chips(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from pioneer.hal.linux import _gpio_path,LinuxHAL
    from pioneer.board import GpioRef
    labels={'gpiochip0':'gpio0','gpiochip2':'gpio3','gpiochip4':'gpio2'}
    for name in labels:(tmp_path/name).touch()
    class Chip:
        def __init__(self,path):self.path=Path(path)
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get_info(self):return SimpleNamespace(label=labels[self.path.name])
    requests=[]
    monkeypatch.setitem(sys.modules,'gpiod',SimpleNamespace(Chip=Chip,request_lines=lambda path,**kw:requests.append(path)))
    assert _gpio_path(2,tmp_path)==str(tmp_path/'gpiochip4')
    with pytest.raises(OSError):_gpio_path(1,tmp_path)
    resolved=[]
    def resolve(bank):resolved.append(bank);return _gpio_path(bank,tmp_path)
    monkeypatch.setattr('pioneer.hal.linux._gpio_path',resolve)
    hal=LinuxHAL.__new__(LinuxHAL);hal._gpio_paths={}
    hal._request(GpioRef(2,4),{});hal._request(GpioRef(2,5),{})
    assert requests==[str(tmp_path/'gpiochip4')]*2 and resolved==[2]


def test_ai_attachments_validation_redaction_and_provider_shapes():
    stream=io.BytesIO();Image.new('RGB',(2,2),'red').save(stream,format='PNG')
    image={'name':'test.png','type':'image/png','content':base64.b64encode(stream.getvalue()).decode()}
    text={'name':'sample.txt','type':'text','content':'temporary-secret'}
    items=validate([image,text],lambda s:s.replace('temporary-secret','[redacted]'))
    assert items[1]['content']=='[redacted]'
    assert message('hello',items,'openai')[1]['type']=='input_image'
    assert message('hello',items,'custom')[1]['type']=='image_url'
    assert message('hello',items,'anthropic')[1]['source']['media_type']=='image/png'
    for bad in [{**image,'content':'invalid-base64'}, {**image,'name':'../test.png'}, {**image,'type':'image/jpeg'}, {**text,'content':'x'*65537}]:
        with pytest.raises(ValueError):validate([bad],lambda s:s)


def test_jedi_cross_file_and_no_execution(client):
    script=client.post(f'{API}/scripts',json={'name':'Language test'}).json();ident=script['id']
    root=client.app.state.cfg.data_dir/'scripts'/ident
    marker=root/'executed';(root/'driver.py').write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\ndef temperature(value):\n    '''Read temperature.'''\n    return value\n")
    code='from driver import temperature\ntemperature(10)\n'
    def query(action,line,column):return client.post(f'{API}/code/scripts/{ident}/language',json={'path':'main.py','code':code,'line':line,'column':column,'action':action})
    result=query('goto',2,5)
    assert result.status_code==200,result.text
    assert result.json()['items'][0]['path']=='driver.py'
    assert 'Read temperature' in query('hover',2,5).json()['items'][0]['documentation']
    assert query('signatures',2,12).json()['items']
    assert not marker.exists()


def test_wheel_requirements_locked_and_isolated(tmp_path):
    for value in ['-r other.txt','requests','https://example.org/a.whl','pillow>=10','-e .']:
        with pytest.raises(TreeError):parse(value)
    root=tmp_path/'scripts'/'abc';root.mkdir(parents=True);(root/'requirements.txt').write_text('# no dependencies\n')
    with pytest.raises(TreeError):install(root,False)
    assert install(root,True)['changed']
    assert site_for(root).exists() and not install(root,True)['changed']
    (root/'requirements.txt').write_text('# changed empty set\n')
    assert install(root,True)['changed']


def test_watchdog_requires_disarmable_before_open(tmp_path,monkeypatch):
    from pioneer.watchdog import Watchdog,GETSUPPORT,MAGICCLOSE,SETTIMEOUT
    w=Watchdog(Config(kernel_sys_dir=tmp_path));w.device=tmp_path/'watchdog';w.device.touch();w.sysfs.mkdir(parents=True)
    (w.sysfs/'nowayout').write_text('1');opened=[];writes=[]
    monkeypatch.setattr('pioneer.watchdog.os.open',lambda *a:opened.append(a) or 123)
    monkeypatch.setattr('pioneer.watchdog.os.write',lambda fd,data:writes.append(data))
    monkeypatch.setattr('pioneer.watchdog.os.close',lambda fd:None)
    def ioctl(fd,op,value,*args):
        if op==GETSUPPORT:value[:4]=MAGICCLOSE.to_bytes(4,'little')
    monkeypatch.setattr('pioneer.watchdog.fcntl.ioctl',ioctl)
    async def run():
        with pytest.raises(ValueError):await w.arm(60,True)
        assert not opened
        (w.sysfs/'nowayout').write_text('0')
        assert (await w.arm(60,True))['armed']
        assert not (await w.disarm())['armed'] and writes==[b'.',b'V']
    asyncio.run(run())


def test_gpio_bias_plan_preserves_boot_and_busy_pins(tmp_path,board,hw):
    from pioneer.kernel_drivers import KernelDrivers
    from pioneer.overlays import OverlayManager
    from pioneer.pulls import Pulls
    uenv=tmp_path/'uEnv.txt';original='overlays='+' '.join(Config().default_overlays)+'\nuser_overlays=pioneer-dht22\n';uenv.write_text(original)
    (tmp_path/'data').mkdir()
    cfg=Config(data_dir=tmp_path/'data',uenv_file=uenv,dt_dir=tmp_path/'absent')
    pulls=Pulls(KernelDrivers(cfg,board,hw,OverlayManager(cfg,board,hw)))
    p=pulls.preview('J9-7','up')
    assert 'pioneer_gpio_bias_bank2' in p['source'] and 'pcfg_pull_up' in p['source']
    assert 'pioneer-dht22 pioneer-gpio-bias' in p['diff']
    assert not p['blockers'] and uenv.read_text()==original
    hw.acquire('a','Busy','J9-7','gpio_in')
    assert pulls.preview('J9-7','down')['blockers']

def test_ai_model_override_and_attachment_not_stored(client):
    from test_ai import FakeProvider,setup_ai
    setup_ai(client);original=client.app.state.ai.settings().model
    seen={}
    class Capture(FakeProvider):
        def __init__(self,settings,key):
            seen['model']=settings.model;super().__init__(settings,key)
        async def stream(self,messages,system,tools,budget):
            seen['content']=messages[-1]['content']
            yield {'delta':'OK'}
            yield {'result':[{'role':'assistant','content':'OK'}],'calls':[],'usage':{'input':1,'output':1}}
    client.app.state.ai.provider_factory=Capture
    p=client.app.state.projects.create('Attachments')
    image=io.BytesIO();Image.new('RGB',(2,2),'red').save(image,format='PNG');content=base64.b64encode(image.getvalue()).decode()
    r=client.post(f'{API}/ai/chats/{p.id}/messages',json={'text':'Describe','model':'different-test-model','attachments':[{'name':'test.png','type':'image/png','content':content},{'name':'test.txt','type':'text','content':'live-secret-value'}]})
    assert r.status_code==200,r.text
    assert seen['model']=='different-test-model' and seen['content'][1]['type']=='input_image'
    assert 'live-secret-value' not in str(seen['content'])
    assert client.app.state.ai.settings().model==original
    history=client.get(f'{API}/ai/chats/{p.id}').text
    assert content not in history and 'test.png' in history and 'different-test-model' in history


def test_gpio_bias_does_not_claim_pins_but_driver_still_does(tmp_path):
    from test_dt import make_dt,u32,s
    from pioneer.dt import read_dt
    dt=make_dt(tmp_path/'dt',{'/aliases':{'gpio2':s('/pinctrl/gpio@ff240000')},'/pinctrl/gpio@ff240000':{'phandle':u32(1),'pinctrl-0':u32(2)},'/pinctrl/pioneer-gpio-bias/bank2-pins':{'phandle':u32(2),'rockchip,pins':u32(2,4,0,3)},'/dht22':{'gpios':u32(1,4,0)}})
    users=read_dt(dt).users(2,4)
    assert len(users)==1 and users[0].via=='gpios'


def test_module_malformed_partial_reply_is_bounded(tmp_path,bench):
    from pioneer.module_models import ModuleModel
    root=tmp_path/'modules'/'broken';root.mkdir(parents=True)
    # Worker Simulation itself never returns: subsequent calls expire and core stays usable.
    (root/'simulation.py').write_text('import time\nclass Simulation:\n    def __init__(self,config):pass\n    def state(self):time.sleep(10)\n')
    cls=type('Slow',(ModuleModel,),{'root':root,'simulation':'simulation.py','kind':'slow','title':'Slow'})
    m=cls(bench,'slow');start=time.monotonic()
    assert m.state()['available'] is False
    assert time.monotonic()-start<1
    m.detach()

def test_language_worker_reuse_and_idle_release(tmp_path):
    from pioneer.language_service import LanguageService
    (tmp_path/'helper.py').write_text('def temperature(value):\n    return value\n')
    body={'root':str(tmp_path),'path':'main.py','code':'from helper import temperature\ntemperature(1)','line':2,'column':5,'action':'goto'}
    async def run():
        service=LanguageService(asyncio.Lock());service.idle_seconds=.03
        try:
            first=await service.analyze(body);pid=service.process.pid
            second=await service.analyze(body)
            assert first==second and service.process.pid==pid
            await asyncio.sleep(.08)
            assert service.process is None
        finally:await service.close()
    asyncio.run(run())

def test_module_bundle_installs_pinned_version_without_start(client):
    manager=client.app.state.modules
    package=CodeTree(REPO_ROOT/'modules/examples/register').export()
    installed=manager.install(package,True)
    pm=client.app.state.projects;p=pm.create('Module bundle')
    pm.apply_ops(p.id,[{'op':'add_node','id':'r','type':installed['types'][0]}])
    exported=client.get(f'{API}/projects/{p.id}/bundle.zip');assert exported.status_code==200
    pm.delete(p.id);manager.remove('register_demo',1)
    response=client.post(f'{API}/projects/import.zip?confirmed=true',content=exported.content)
    assert response.status_code==200,response.text
    assert manager.list()[0]['types']==['module.register_demo.read@1']
    assert not pm.engine.runtimes and not client.app.state.hw.res.claims
    pm.delete(response.json()['id']);manager.remove('register_demo',1)


def test_invalid_module_model_config_is_an_api_error(client):
    manager=client.app.state.modules
    installed=manager.install(CodeTree(REPO_ROOT/'modules/examples/register').export(),True)
    try:
        r=client.post(f'{API}/bench/models',json={'kind':installed['model_kind'],'config':{'value':'not-an-integer'}})
        assert r.status_code==422
        assert not client.app.state.hal.models
    finally:manager.remove('register_demo',1)

def test_streamed_module_zip_upload_and_limit(client,monkeypatch):
    import pioneer.api.modules as routes
    package=CodeTree(REPO_ROOT/'modules/examples/register').export()
    response=client.post(f'{API}/modules?confirmed=true',content=package)
    assert response.status_code==200,response.text
    assert response.json()['types']==['module.register_demo.read@1']
    client.app.state.modules.remove('register_demo',1)
    monkeypatch.setattr(routes,'TREE_LIMIT',32)
    assert client.post(f'{API}/modules?confirmed=true',content=b'x'*33).status_code==413
    assert not client.app.state.modules.list()

def test_language_syntax_diagnostic_without_execution(tmp_path):
    from pioneer.language_worker import analyze
    result=analyze({'action':'diagnostics','code':'def temperature(:\n    return 1\n'})
    assert result['items'][0]['line']==1 and result['items'][0]['message']
    assert analyze({'action':'diagnostics','code':'raise RuntimeError("not executed")'})['items']==[]
