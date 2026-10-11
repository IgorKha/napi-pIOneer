"""Изоляция локальных импортов блоков, живущих в одном worker-процессе."""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path

from pioneer.config import REPO_ROOT


class ImportScope:
    def __init__(self, root: Path):
        self.root = root.resolve()
        # blocks/<id>/block.py и script_cache/<hash>/block.py
        self.lib = self.root.parent.parent / "lib"
        from pioneer.dependencies import site_for

        self.site = site_for(self.root)
        self.modules: dict = {}
        # Import paths stay fixed for this worker; resolving every module on each
        # callback is expensive on the board filesystem. A restart resets the cache.
        self._local_paths: dict[str, bool] = {}

    def local(self, module) -> bool:
        paths = [getattr(module, "__file__", None), *list(getattr(module, "__path__", ()))]
        for p in paths:
            if not p:
                continue
            if p not in self._local_paths:
                resolved = Path(p).resolve()
                self._local_paths[p] = any(
                    resolved.is_relative_to(root) for root in (self.root, self.lib, self.site)
                )
            if self._local_paths[p]:
                return True
        return False

    @contextmanager
    def activate(self):
        old_path, old_cwd = sys.path[:], os.getcwd()
        shadowed = {name: sys.modules[name] for name in self.modules if name in sys.modules}
        sys.modules.update(self.modules)
        sys.path = [
            str(self.root),
            str(self.site),
            str(self.lib),
            str(REPO_ROOT / "backend"),
            *[p for p in old_path if p and p != old_cwd and p != str(self.root)],
        ]
        os.chdir(self.root)
        try:
            yield
        finally:
            self.modules.update({n: m for n, m in list(sys.modules.items()) if self.local(m)})
            for name in self.modules:
                sys.modules.pop(name, None)
            sys.modules.update(shadowed)
            sys.path = old_path
            os.chdir(old_cwd)
