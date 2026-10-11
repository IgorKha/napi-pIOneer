"""Процесс Python-блоков проекта: python -m pioneer.blockhost.

- `--schema FILE` — импортировать файл блока и напечатать схемы объявленных нод (JSON) —
  проверка «без запуска проекта» (core сам код блоков не исполняет);
- без аргументов — worker проекта: JSON-строки через унаследованный сокет (PIONEER_WORKER_FD).

Запросы core: {"id", "op": "load"|"call"|"stop", ...} → {"id", "ok", "actions": [...]}
или {"id", "ok": false, "error"}. Действия: ["set", выход, значение], ["emit", выход, payload],
["log", текст]. Таймеры блоков шлют действия сами: {"async": нода, "actions": [...]}.
"""
from __future__ import annotations

import heapq
import queue
import itertools
import json
import os
import runpy
import signal
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

from pioneer.importscope import ImportScope

from pioneer import block


def load_file(path: str, scope: ImportScope | None = None) -> dict[str, type[block.Node]]:
    """Выполнить файл блока и вернуть объявленные в нём ноды {id: класс}."""
    block.REGISTRY.clear()
    path = str(Path(path).resolve())
    scope = scope or ImportScope(Path(path).parent)
    with scope.activate():
        runpy.run_path(os.path.relpath(path, scope.root), run_name="pioneer_block")
    found = dict(block.REGISTRY)
    block.REGISTRY.clear()
    if not found:
        raise ValueError("в файле нет ни одного @node")
    return found


def error_text(e: BaseException) -> str:
    # строка в коде пользователя: последний кадр вне пакета pioneer
    pkg = os.path.dirname(os.path.abspath(block.__file__))
    tb = traceback.extract_tb(e.__traceback__)
    where = next((f"строка {f.lineno}: " for f in reversed(tb)
                  if not os.path.abspath(f.filename).startswith(pkg + os.sep) and "runpy" not in f.filename), "")
    if isinstance(e, SyntaxError):
        return f"синтаксис, строка {e.lineno}: {e.msg}"
    return f"{where}{type(e).__name__}: {e}"


def schema_main(path: str) -> None:
    try:
        nodes = load_file(path)
        print(json.dumps({"ok": True, "nodes": [cls.BLOCK for cls in nodes.values()]}, ensure_ascii=False))
    except BaseException as e:  # noqa: BLE001 — любой сбой кода пользователя — это ответ, не падение
        print(json.dumps({"ok": False, "error": error_text(e)}, ensure_ascii=False))


class Host:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.requests = queue.SimpleQueue()
        self.hardware_pending = {}
        self.hardware_ids = itertools.count(1)
        threading.Thread(target=self._receive,daemon=True).start()
        self.nodes: dict[str, block.Node] = {}
        self.files: dict[tuple[str, float], dict] = {}
        self.scopes: dict[str, ImportScope] = {}
        self.lock = threading.RLock()
        self.send_lock = threading.Lock()
        self.timers: list = []
        self.seq = itertools.count()
        self.cv = threading.Condition()
        threading.Thread(target=self._timer_loop, name="block-timers", daemon=True).start()

    def send(self, msg: dict) -> None:
        data = (json.dumps(msg, ensure_ascii=False, default=str) + "\n").encode()
        with self.send_lock:
            self.sock.sendall(data)

    # ---- вызовы ноды
    def _run(self, node: block.Node, fn, *args) -> list:
        node._actions = []
        with node._scope.activate():
            fn(*args)
        out, node._actions = node._actions, []
        return out

    def handle(self, req: dict) -> list:
        op = req["op"]
        if op == "load":
            path = req["file"]
            key = (path, os.path.getmtime(path))
            if key not in self.files:
                scope = self.scopes.setdefault(str(Path(path).parent), ImportScope(Path(path).parent))
                self.files[key] = load_file(path, scope)
            cls = self.files[key].get(req["block"])
            if cls is None:
                raise ValueError(f"в {os.path.basename(path)} нет блока {req['block']!r}")
            node = cls(req["node"], req.get("params", {}))
            node._host = self
            node._scope = self.scopes[str(Path(path).parent)]
            self.nodes[req["node"]] = node
            return self._run(node, node.setup)
        node = self.nodes[req["node"]]
        if op == "stop":
            self.nodes.pop(req["node"], None)
            return self._run(node, node.stop)
        node._inputs = req.get("inputs", {})
        method = req["method"]
        if method == "on_change":
            return self._run(node, node.on_change, set(req.get("changed", ())))
        if method == "on_event":
            return self._run(node, node.on_event, req["name"], req.get("payload"))
        if method == "start":
            return self._run(node, node.start)
        raise ValueError(f"неизвестный метод {method!r}")

    # ---- таймеры
    def timer(self, node: block.Node, delay: float, fn, interval: float | None) -> None:
        with self.cv:
            heapq.heappush(self.timers, (time.monotonic() + delay, next(self.seq), node, fn, interval))
            self.cv.notify()

    def _timer_loop(self) -> None:
        while True:
            with self.cv:
                while not self.timers or self.timers[0][0] > time.monotonic():
                    self.cv.wait(None if not self.timers else self.timers[0][0] - time.monotonic())
                due, _, node, fn, interval = heapq.heappop(self.timers)
                if interval:
                    heapq.heappush(self.timers, (max(due + interval, time.monotonic()), next(self.seq), node, fn, interval))
            if node.id not in self.nodes:
                continue
            with self.lock:
                try:
                    actions = self._run(node, fn)
                except Exception as e:
                    actions = [["error", error_text(e)]]
            if actions:
                self.send({"async": node.id, "actions": actions})


    def _receive(self):
        try:
            for line in self.sock.makefile("r",encoding="utf-8"):
                msg=json.loads(line)
                if "hardware_reply" in msg:
                    slot=self.hardware_pending.get(msg["hardware_reply"])
                    if slot:slot[1]=msg;slot[0].set()
                else:self.requests.put(msg)
        finally:self.requests.put(None)

    def hardware(self,node,op,args):
        rid=next(self.hardware_ids);slot=[threading.Event(),None];self.hardware_pending[rid]=slot
        try:
            self.send({"hardware":rid,"node":node,"op":op,"args":args})
            if not slot[0].wait(.8):raise TimeoutError("hardware RPC не ответил за 0.8 с")
            msg=slot[1]
            if not msg.get("ok"):raise ValueError(msg.get("error","ошибка железа"))
            return msg.get("result")
        finally:self.hardware_pending.pop(rid,None)

    def serve(self) -> None:
        while (req:=self.requests.get()) is not None:
            with self.lock:
                try:
                    self.send({"id":req["id"],"ok":True,"actions":self.handle(req)})
                except Exception as e:
                    self.send({"id":req["id"],"ok":False,"error":error_text(e)})


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--schema":
        schema_main(sys.argv[2])
        return
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    Host(socket.socket(fileno=int(os.environ["PIONEER_WORKER_FD"]))).serve()


if __name__ == "__main__":
    main()
