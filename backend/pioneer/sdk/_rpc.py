"""Клиент RPC worker → core: JSON-строки через унаследованный сокет (fd в PIONEER_WORKER_FD).

Запросы: {"id": n, "op": ..., ...} → ответ {"id": n, "ok": true, "result": ...}
или {"id": n, "ok": false, "error": "..."}. События от core: {"ev": "edge"|"stop", ...}.
"""
from __future__ import annotations

import json
import os
import queue
import socket
import threading

# Задаётся core из pioneer.limits (script_rpc_s) при запуске скрипта.
RPC_TIMEOUT_S = float(os.environ.get("PIONEER_RPC_TIMEOUT", "15"))
_client: _Client | None = None


class PioneerError(Exception):
    """Ошибка со стороны core: пин занят, интерфейс выключен, нет устройства на шине…"""


class _Client:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.rfile = sock.makefile("r", encoding="utf-8")
        self._ids = iter(range(1, 1 << 62))
        self._pending: dict[int, list] = {}  # id -> [Event, ответ]
        self._send_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self.events: queue.Queue = queue.Queue()
        threading.Thread(target=self._reader, name="pioneer-rpc", daemon=True).start()

    def call(self, op: str, **args):
        rid = next(self._ids)
        slot = [threading.Event(), None]
        with self._pending_lock:
            self._pending[rid] = slot
        line = json.dumps({"id": rid, "op": op, **args}) + "\n"
        timeout = RPC_TIMEOUT_S
        if op == "uart_read" and isinstance(args.get("timeout"), (int, float)):
            timeout = max(timeout, args["timeout"] + 5)
        try:
            with self._send_lock:
                self.sock.sendall(line.encode())
            if not slot[0].wait(timeout):
                raise PioneerError(f"RPC {op}: core не ответил за {timeout:g} с — "
                                  "лимит: Настройки → Система → Таймауты")
        finally:
            with self._pending_lock:
                self._pending.pop(rid, None)
        resp = slot[1]
        if resp is None:
            raise PioneerError("связь с core потеряна")
        if not resp.get("ok"):
            raise PioneerError(resp.get("error", "ошибка"))
        return resp.get("result")

    def _reader(self) -> None:
        for line in self.rfile:
            msg = json.loads(line)
            if "id" in msg:
                with self._pending_lock:
                    slot = self._pending.pop(msg["id"], None)
                if slot:
                    slot[1] = msg
                    slot[0].set()
            else:
                self.events.put(msg)
        # core закрыл сокет — будим всех ждущих и останавливаем цикл событий
        with self._pending_lock:
            for slot in self._pending.values():
                slot[0].set()
        self.events.put({"ev": "stop"})


def connect_from_env() -> None:
    global _client
    fd = int(os.environ["PIONEER_WORKER_FD"])
    _client = _Client(socket.socket(fileno=fd))


def _require() -> _Client:
    if _client is None:
        raise RuntimeError("pioneer.sdk работает только внутри скрипта, запущенного pioneer")
    return _client


def call(op: str, **args):
    return _require().call(op, **args)


def events() -> queue.Queue:
    return _require().events
