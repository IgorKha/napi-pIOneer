"""Per-project wheel-only dependencies; no system pip, shell or source builds."""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from pioneer.codetree import CodeTree, TreeError

PACKAGE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?==[A-Za-z0-9][A-Za-z0-9_.+!-]*$')


def parse(text):
    lines=[l.strip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith('#')]
    if len(lines)>32 or any(not PACKAGE.fullmatch(l) for l in lines):
        raise TreeError('requirements: до 32 зависимостей name==version; URL, editable и опции pip запрещены')
    return lines


def site_for(root):
    return root.parent.parent/'dependencies'/f'{root.parent.name}-{root.name}'


def install(root, confirmed):
    if not confirmed:raise TreeError('подтвердите установку доверенных Python-зависимостей')
    t=CodeTree(root);file=t.path('requirements.txt')
    if file.stat().st_size>16384:raise TreeError('requirements.txt больше 16 КиБ')
    text=file.read_text();requirements=parse(text)
    site=site_for(root);site.parent.mkdir(parents=True,exist_ok=True)
    if site.exists():
        lock=site/'.pioneer-requirements.txt'
        if lock.is_file() and lock.read_text()==text:return {'changed':False,'requirements':requirements}
        raise TreeError('зависимости уже установлены; используйте новую версию модуля/проект для смены lock')
    with tempfile.TemporaryDirectory(dir=site.parent) as tmp:
        stage=Path(tmp)/'site'
        args=[sys.executable,'-m','pip','install','--isolated','--disable-pip-version-check','--no-input','--only-binary=:all:','--target',str(stage),'--cache-dir',str(site.parent/'wheel-cache'),*requirements]
        if requirements:
            try:
                result=subprocess.run(args,timeout=180,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env={k:v for k,v in os.environ.items() if not k.startswith('PIP_')})
            except subprocess.TimeoutExpired:raise TreeError('установка зависимостей превысила 180 с') from None
            if result.returncode:raise TreeError('не удалось установить совместимые колёса; проверьте имя, версию, интернет и архитектуру')
        stage.mkdir(exist_ok=True)
        (stage/'.pioneer-requirements.txt').write_text(text)
        os.replace(stage,site)
    return {'changed':True,'requirements':requirements}
