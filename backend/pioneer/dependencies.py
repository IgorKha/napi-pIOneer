"""Per-project wheel-only dependencies; no system pip, shell or source builds."""
import os
import re
import shutil
import uuid
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


def remove(root):
    site = site_for(root)
    with CodeTree(root).lock:
        if site.is_symlink():
            target = site.resolve()
            if target.parent != site.parent or not target.name.startswith('.pioneer-deps-' + site.name + '-'):
                raise TreeError('неизвестная цель каталога зависимостей')
            site.unlink()
            shutil.rmtree(target, ignore_errors=True)
        elif site.exists():
            shutil.rmtree(site)
    return {'removed': True}


def install(root, confirmed):
    if not confirmed:
        raise TreeError('подтвердите установку доверенных Python-зависимостей')
    tree = CodeTree(root)
    with tree.lock:
        file = tree.path('requirements.txt')
        if file.stat().st_size > 16384:
            raise TreeError('requirements.txt больше 16 КиБ')
        text = file.read_text()
        requirements = parse(text)
        site = site_for(root)
        site.parent.mkdir(parents=True, exist_ok=True)
        lock = site / '.pioneer-requirements.txt'
        if lock.is_file() and lock.read_text() == text:
            return {'changed': False, 'requirements': requirements}
        old_target = site.resolve() if site.is_symlink() else None
        if old_target and (old_target.parent != site.parent or not old_target.name.startswith('.pioneer-deps-' + site.name + '-')):
            raise TreeError('неизвестная цель каталога зависимостей')
        stage = Path(tempfile.mkdtemp(prefix='.pioneer-deps-' + site.name + '-', dir=site.parent))
        pointer = site.with_name('.pioneer-next-' + uuid.uuid4().hex)
        backup = site.with_name('.pioneer-old-' + uuid.uuid4().hex)
        try:
            if requirements:
                args = [sys.executable, '-m', 'pip', 'install', '--isolated', '--disable-pip-version-check',
                        '--no-input', '--only-binary=:all:', '--target', str(stage),
                        '--cache-dir', str(site.parent / 'wheel-cache'), *requirements]
                try:
                    result = subprocess.run(args, timeout=180, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            env={k:v for k,v in os.environ.items() if not k.startswith('PIP_')})
                except subprocess.TimeoutExpired:
                    raise TreeError('установка зависимостей превысила 180 с') from None
                if result.returncode:
                    raise TreeError('не удалось установить совместимые колёса')
            (stage / '.pioneer-requirements.txt').write_text(text)
            pointer.symlink_to(stage.name, target_is_directory=True)
            # Existing installs use an atomic pointer swap; migrate legacy directories once.
            if site.exists() and not site.is_symlink():
                os.replace(site, backup)
            try:
                os.replace(pointer, site)
            except BaseException:
                if backup.exists():
                    os.replace(backup, site)
                raise
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        finally:
            pointer.unlink(missing_ok=True)
        if old_target:
            shutil.rmtree(old_target, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)
        return {'changed': True, 'requirements': requirements}
