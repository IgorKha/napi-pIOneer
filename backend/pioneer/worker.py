"""Worker-процесс скрипта: python -m pioneer.worker <script.py>.

Запускается core (pioneer.scripts), исполняет скрипт пользователя вне процесса core;
к железу — только через RPC. stdout/stderr уходят в лог скрипта.
"""
from __future__ import annotations

import os
from pathlib import Path
import runpy
import signal
import sys


def _terminate(signum, frame):  # SIGTERM от core -> штатный выход
    raise SystemExit(0)


def main() -> None:
    signal.signal(signal.SIGTERM, _terminate)
    from pioneer import sdk
    from pioneer.sdk import _rpc
    _rpc.connect_from_env()

    path = str(Path(sys.argv[1]).resolve())
    root = os.getcwd()
    from pioneer.dependencies import site_for
    sys.path = [root, str(site_for(Path(root))), os.environ["PIONEER_LIB"], *[p for p in sys.path if p and p != root]]
    from pioneer.compat.circuitpython import paths, check_lock
    check_lock(Path(root))
    sys.path[:0] = paths(Path(root))
    sys.argv = [path]
    runpy.run_path(os.path.relpath(path, root), run_name="__main__")
    if sdk._loop.has_work and not sdk._loop.running:
        sdk.run()


if __name__ == "__main__":
    main()
