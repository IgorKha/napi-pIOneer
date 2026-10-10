"""Worker-процесс скрипта: python -m pioneer.worker <script.py>.

Запускается core (pioneer.scripts), исполняет скрипт пользователя вне процесса core;
к железу — только через RPC. stdout/stderr уходят в лог скрипта.
"""
from __future__ import annotations

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

    path = sys.argv[1]
    sys.argv = [path]
    runpy.run_path(path, run_name="__main__")
    if sdk._loop.has_work and not sdk._loop.running:
        sdk.run()


if __name__ == "__main__":
    main()
