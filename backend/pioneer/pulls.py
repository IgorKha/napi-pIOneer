"""GPIO bias via verified next-boot DT overlay; no pretend libgpiod pull support."""
import difflib
import hashlib
import json
import os
import re
import time
from pioneer.board import load_board,parse_uenv_overlays
from pioneer.uenv import UEnvError
from pioneer.codetree import CodeTree
from pioneer.kernel_drivers import DriverError
NAME='pioneer-gpio-bias'

class Pulls:
    def __init__(self,drivers):
        self.drivers=drivers;self.file=drivers.root/'gpio-bias.json'
    def config(self):return json.loads(self.file.read_text()) if self.file.exists() else {}
    def source(self,values):
        groups={}
        for pin,bias in sorted(values.items()):
            p=self.drivers.board.pin(pin);bank,line=p.gpio.chip,p.gpio.line
            groups.setdefault(bank,[]).append(f'<{bank} {line} 0 &pcfg_pull_{bias}>')
        fragments=['/dts-v1/;','/plugin/;','/ {']
        configs=' '.join(f'pioneer_gpio_bias_bank{bank}: bank{bank}-pins {{ rockchip,pins = '+', '.join(pins)+'; };' for bank,pins in groups.items())
        fragments.append(f'fragment@0 {{ target = <&pinctrl>; __overlay__ {{ pioneer-gpio-bias {{ {configs} }}; }}; }};')
        for index,bank in enumerate(groups,1):
            fragments.append(f'fragment@{index} {{ target = <&gpio{bank}>; __overlay__ {{ pinctrl-names = "default"; pinctrl-0 = <&pioneer_gpio_bias_bank{bank}>; }}; }};')
        fragments.append('};');return '\n'.join(fragments)+'\n'
    def _plan(self,pin,bias):
        d=self.drivers;old=self.config();values=dict(old)
        if bias not in ('up','down','none','default'):raise DriverError('подтяжка: up/down/none/default')
        p=d.board.pins.get(pin)
        if not p or not p.gpio:raise DriverError('подтяжка только GPIO гребёнки')
        if bias=='default':values.pop(pin,None)
        else:values[pin]=bias
        if d.hw.hal.name=='linux' and d.board.dt:
            for name in values:
                bank=d.board.pin(name).gpio.chip
                node=d.board.dt.node_of('gpio'+str(bank))
                prop=d.cfg.dt_dir/node.lstrip('/')/'pinctrl-0'
                ours='pioneer_gpio_bias_bank'+str(bank) in d.board.dt.symbols
                if prop.exists() and not ours:raise DriverError('GPIO-банк уже имеет pinctrl-0; его перезапись запрещена',409)
        text=d.boot_text();pending=load_board(d.cfg.board_file,parse_uenv_overlays(text))
        blockers=[]
        for name in set(old)|set(values):
            if name in d.hw.res.claims:blockers.append(name+' занят проектом/скриптом')
            if values.get(name) is not None and (pending.function(name)!='gpio' or d.board.function(name) not in ('gpio',)):
                blockers.append(name+' занят интерфейсом/ядром')
        names=re.search(r'^user_overlays=(.*)$',text,re.M)
        overlays=names[1].split() if names else []
        overlays=[n for n in overlays if n!=NAME]
        if values:overlays.append(NAME)
        line='user_overlays='+' '.join(overlays)
        updated=re.sub(r'^user_overlays=.*$',line,text,flags=re.M) if names else text+('' if text.endswith('\n') else '\n')+line+'\n'
        source=self.source(values)
        digest=hashlib.sha256((text+json.dumps(old,sort_keys=True)+source).encode()).hexdigest()
        blob=None
        if not blockers and d.hw.hal.name=='linux':blob=d.compile(source,updated,NAME)
        return {'hash':digest,'uenv_hash':hashlib.sha256(text.encode()).hexdigest(),'pin':pin,'bias':bias,'values':values,'source':source,'diff':''.join(difflib.unified_diff(text.splitlines(True),updated.splitlines(True))), 'blockers':blockers,'compiled':blob is not None,'needs_reboot':True},updated,blob
    def preview(self,pin,bias):return self._plan(pin,bias)[0]
    def apply(self,pin,bias,expected,confirmed):
        d=self.drivers;d.real()
        if not confirmed:raise DriverError('подтвердите изменение загрузки и подтяжки')
        with d.overlays.lock,d.lock:
            plan,text,blob=self._plan(pin,bias)
            if not expected or plan['hash']!=expected:raise DriverError('настройки изменились; обновите план',409)
            if plan['blockers']:raise DriverError('; '.join(plan['blockers']),409)
            with d.hw._lock,d.hw.res._lock:
                if any(p in d.hw.res.claims for p in set(self.config())|set(plan['values'])):raise DriverError('пин занят',409)
                if self._plan_hash(plan)!=expected:raise DriverError('uEnv изменился',409)
                dest=d.cfg.user_overlays_dir/(NAME+'.dtbo')
                files={self.file:json.dumps(plan['values']).encode()}
                if plan['values']:files[dest]=blob
                try:
                    return d.overlays.uenv.commit(plan['uenv_hash'],text,files)
                except UEnvError as e:
                    raise DriverError(str(e),409) from e
    def _plan_hash(self,plan):
        return hashlib.sha256((self.drivers.boot_text()+json.dumps(self.config(),sort_keys=True)+plan['source']).encode()).hexdigest()
