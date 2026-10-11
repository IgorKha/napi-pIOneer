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

    def local(self, module) -> bool:
        paths = [getattr(module, "__file__", None), *list(getattr(module, "__path__", ()))]
        return any(p and (Path(p).resolve().is_relative_to(self.root)
                         or Path(p).resolve().is_relative_to(self.lib)
                         or Path(p).resolve().is_relative_to(self.site)) for p in paths)

    @contextmanager
    def activate(self):
        old_path, old_cwd = sys.path[:], os.getcwd()
        shadowed = {name: sys.modules[name] for name in self.modules if name in sys.modules}
        sys.modules.update(self.modules)
        sys.path = [str(self.root), str(self.site), str(self.lib), str(REPO_ROOT / "backend"),
                    *[p for p in old_path if p and p != old_cwd and p != str(self.root)]]
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
