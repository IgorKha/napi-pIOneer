"""One lock and rollback transaction for boot configuration and companion files."""
from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path

from pioneer.codetree import CodeTree


class UEnvError(ValueError):
    pass


class UEnv:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.RLock()

    def read(self):
        path = self.cfg.uenv_file
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            raise UEnvError('uEnv должен быть обычным файлом до 64 КБ')
        return path.read_text()

    def commit(self, expected_hash: str, text: str, files: dict[Path, bytes] | None = None):
        with self.lock:
            old = self.read()
            if hashlib.sha256(old.encode()).hexdigest() != expected_hash:
                raise UEnvError('uEnv изменился — обновите предпросмотр')
            targets = {**(files or {}), self.cfg.uenv_file: text.encode()}
            before = {}
            for target in targets:
                if target.is_symlink():
                    raise UEnvError('симлинки в boot-транзакции запрещены')
                before[target] = (target.read_bytes(), target.stat().st_mode & 0o777) if target.exists() else None
            backups = self.cfg.data_dir.parent / 'backups'
            backups.mkdir(parents=True, exist_ok=True)
            backup = backups / ('boot-' + str(time.time_ns()))
            backup.mkdir(mode=0o700)
            for index, (target, original) in enumerate(before.items()):
                if original:
                    # Names are useful to an operator; index avoids collisions.
                    saved = backup / target.name
                    if saved.exists():
                        saved = backup / (str(index) + '-' + target.name)
                    CodeTree._write(saved, original[0])
                    os.chmod(saved, 0o600)
            try:
                for target, data in targets.items():
                    CodeTree._write(target, data)
                    os.chmod(target, before[target][1] if before[target] else 0o600)
            except BaseException:
                for target, original in before.items():
                    if original:
                        CodeTree._write(target, original[0])
                        os.chmod(target, original[1])
                    else:
                        target.unlink(missing_ok=True)
                raise
            return {'changed': True, 'needs_reboot': True, 'backup': str(backup)}
