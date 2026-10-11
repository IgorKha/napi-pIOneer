"""Скрипты (фаза 0): хранение, запуск в worker-процессе, RPC к HW, лог и статус.

data/scripts/<id>/ — файлы проекта, pioneer.json — метаданные.
Скрипт исполняется в отдельном процессе (pioneer.worker): падение/зависание не роняет
core; при выходе worker'а все его ресурсы освобождаются, выходы — в safe state.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import re
import secrets
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from pioneer.codetree import CodeTree, migrate
from pioneer.config import REPO_ROOT
from pioneer.hw import HW
from pioneer.resources import ResourceConflict

log = logging.getLogger(__name__)
LOG_LINES = 1000
STOP_TIMEOUT_S = 2.0
FLUSH_MAX_LINES = 200  # не больше строк лога за одну рассылку на скрипт

TEMPLATE = '''# Кнопка на J10-2 переключает LED на J9-7.
# Без платы: на странице «Плата» добавьте на J10-2 кнопку, на J9-7 — LED (раздел «Стенд»).
from pioneer.sdk import Input, Output, log

led = Output("J9-7")
button = Input("J10-2", debounce_ms=30)


@button.on_press
def toggle(event):
    led.toggle()
    log("LED", "вкл" if led.value else "выкл")
'''


@dataclass
class ScriptMeta:
    id: str
    name: str
    autostart: bool = False
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    entry: str = "main.py"
    restart_on_save: bool = False


@dataclass
class Status:
    state: str = "stopped"  # stopped | running | exited | error
    pid: int | None = None
    started: float | None = None
    exit_code: int | None = None
    error: str | None = None


class ScriptError(Exception):
    pass


class _Runner:
    """Один запущенный worker."""

    def __init__(self, mgr: ScriptManager, meta: ScriptMeta, path: Path):
        self.mgr, self.meta = mgr, meta
        self.root = mgr.tree(meta.id).root
        self.owner = f"script:{meta.id}"
        self.owner_name = f"скрипт «{meta.name}»"
        self.handles: dict[int, object] = {}
        self._next_h = 1
        self._send_lock = threading.Lock()
        self._stopping = False
        self._last_err = ""

        parent, child = socket.socketpair()
        self.sock = parent
        env = {**os.environ, "PIONEER_WORKER_FD": str(child.fileno()), "PYTHONUNBUFFERED": "1",
               "PIONEER_LIB": str(mgr.dir.parent / "lib"),
               "PYTHONPATH": str(REPO_ROOT / "backend")}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "pioneer.worker", str(path)],
            pass_fds=[child.fileno()], env=env, cwd=mgr.tree(meta.id).root,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        child.close()
        self.status = Status("running", self.proc.pid, time.time())
        for target, name in ((self._rpc_loop, "rpc"), (lambda: self._pipe(self.proc.stdout, "out"), "out"),
                             (lambda: self._pipe(self.proc.stderr, "err"), "err"), (self._wait, "wait")):
            threading.Thread(target=target, name=f"script-{meta.id}-{name}", daemon=True).start()

    # ---- жизненный цикл
    def stop(self) -> None:
        self._stopping = True
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self.mgr._log(self.meta.id, "sys", f"не остановился за {STOP_TIMEOUT_S:g} с — SIGKILL")
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait()
            except (ProcessLookupError, PermissionError):
                pass  # уже вышел (на macOS killpg группы с зомби-лидером даёт EPERM)

    def _wait(self) -> None:
        code = self.proc.wait()
        # Выходы переходят в заданный безопасный уровень, порты закрываются.
        self.mgr.hw.release(self.owner)
        self.sock.close()
        if self._stopping or code in (0, -signal.SIGTERM):
            st = Status("stopped" if self._stopping else "exited", exit_code=code)
            self.mgr._log(self.meta.id, "sys", "остановлен" if self._stopping else "скрипт завершился")
        else:
            st = Status("error", exit_code=code, error=self._last_err or f"код выхода {code}")
            self.mgr._log(self.meta.id, "sys", f"упал (код {code})")
        self.mgr._finished(self.meta.id, self, st)

    def _pipe(self, stream, kind: str) -> None:
        for raw in iter(stream.readline, b""):
            text = raw.decode("utf-8", "replace").rstrip("\n")
            text = text.replace(f'File "{self.root}/', 'File "')
            if kind == "err" and text.strip():
                self._last_err = text.strip()  # последняя строка трейсбека = суть ошибки
            self.mgr._log(self.meta.id, kind, text)

    # ---- RPC
    def _send(self, msg: dict) -> None:
        data = (json.dumps(msg) + "\n").encode()
        with self._send_lock:
            try:
                self.sock.sendall(data)
            except OSError:
                pass  # worker уже ушёл

    def _rpc_loop(self) -> None:
        f = self.sock.makefile("r", encoding="utf-8")
        try:
            for line in f:
                req = json.loads(line)
                rid = req.pop("id")
                try:
                    result = self._handle(req.pop("op"), **req)
                    self._send({"id": rid, "ok": True, "result": result})
                except Exception as e:
                    log.exception("script %s RPC failed", self.meta.id)
                    self._send({"id": rid, "ok": False, "error": _err_text(e)})
        except (OSError, ValueError):
            pass

    def _edge(self, pin: str, ev) -> None:
        self._send({"ev": "edge", "pin": pin, "rising": ev.rising, "ts": ev.timestamp_ns})

    def _handle(self, op: str, **a):
        hw, o, n = self.mgr.hw, self.owner, self.owner_name
        if op == "dht22_read":
            if hw.drivers is None:
                raise ValueError("драйвер DHT22 не настроен в core")
            return hw.drivers.read()
        if op == "acquire":
            hw.acquire(o, n, a["pin"], a["mode"], initial=a.get("initial", False), safe=a.get("safe", False),
                       debounce_ms=a.get("debounce_ms", 0),
                       on_edge=self._edge if a["mode"] == "gpio_in" else None)
            self.mgr.on_pins(a["pin"])
            return None
        if op == "write":
            self._owns(a["pin"])
            hw.write(a["pin"], a["value"])
            self.mgr.on_pins(a["pin"])
            return None
        if op == "read":
            self._owns(a["pin"])
            return hw.value(a["pin"])
        if op == "pwm":
            self._owns(a["pin"])
            hw.set_pwm(a["pin"], freq=a.get("freq"), duty=a.get("duty"))
            return None
        if op == "i2c_open":
            return self._handle_id(hw.i2c(o, n, a["iface"], a["addr"]))
        if op == "i2c":
            dev = self.handles[a["h"]]
            data = bytes.fromhex(a.get("data", ""))
            if a["kind"] == "write":
                dev.write(data)
                return None
            if a["kind"] == "read":
                return dev.read(a["n"]).hex()
            return dev.write_read(data, a["n"]).hex()
        if op == "spi_open":
            return self._handle_id(hw.spi(o, n, a["iface"], a["cs"], mode=a["mode"], speed_hz=a["speed_hz"]))
        if op == "spi_xfer":
            return self.handles[a["h"]].xfer(bytes.fromhex(a["data"])).hex()
        if op == "hardware_pwm_open":
            return self._handle_id(hw.hardware_pwm(o, n, a["iface"]))
        if op == "hardware_pwm_set":
            self.handles[a["h"]].set(a.get("freq"), a.get("duty"))
            return None
        if op == "uart_open":
            return self._handle_id(hw.uart(o, n, a["iface"], baud=a["baud"], direction=a.get("direction", "both")))
        if op == "uart_write":
            self.handles[a["h"]].write(bytes.fromhex(a["data"]))
            return None
        if op == "uart_read":
            return self.handles[a["h"]].read(a["max"], a["timeout"]).hex()
        if op == "led_open":
            h = self._handle_id(hw.led(o, n, a["led"]))
            self.mgr.on_leds()
            return h
        if op == "led":
            led = self.handles[a["h"]]
            led.set_trigger(a["trigger"]) if "trigger" in a else led.set(a["on"])
            return None
        raise ValueError(f"неизвестная операция {op!r}")

    def _owns(self, pin: str) -> None:
        claim = self.mgr.hw.res.claims.get(pin)
        if claim is None or self.owner not in claim.owners:
            raise RuntimeError(f"{pin} не захвачен этим скриптом")

    def _handle_id(self, obj) -> int:
        h, self._next_h = self._next_h, self._next_h + 1
        self.handles[h] = obj
        return h


def _err_text(e: Exception) -> str:
    if isinstance(e, KeyError):
        return str(e.args[0]) if e.args else "нет такого ресурса"
    return str(e)


class ScriptManager:
    def __init__(self, data_dir: Path, hw: HW):
        self.dir = data_dir / "scripts"
        migrate(self.dir, "main.py")
        self.hw = hw
        self._lock = threading.RLock()
        self._runners: dict[str, _Runner] = {}
        self._status: dict[str, Status] = {}
        self._logs: dict[str, collections.deque] = {}
        self._pending_logs: dict[str, list[dict]] = {}
        self._dropped: dict[str, int] = {}
        self._status_dirty: set[str] = set()
        self.on_pins = lambda *pins: None  # LiveHub.mark — обновить уровни на странице платы
        self.on_leds = lambda: None  # LiveHub.mark_leds

    # ---- хранилище
    def _paths(self, sid: str) -> tuple[Path, Path]:
        if not re.fullmatch(r"[a-z0-9]{6,32}", sid):
            raise ScriptError(f"плохой id скрипта {sid!r}")
        tree = CodeTree(self.dir / sid)
        meta_file = tree.path("pioneer.json", metadata=True)
        entry = tree.meta().get("entry", "main.py") if meta_file.exists() else "main.py"
        return tree.path(entry), meta_file

    def tree(self, sid: str) -> CodeTree:
        self._paths(sid)
        return CodeTree(self.dir / sid)

    def list(self) -> list[dict]:
        out = []
        for meta_file in sorted(self.dir.glob("*/pioneer.json")):
            meta = self.meta(meta_file.parent.name)
            out.append({**asdict(meta), "status": asdict(self.status(meta.id))})
        return sorted(out, key=lambda m: m["name"].lower())

    def meta(self, sid: str) -> ScriptMeta:
        _, meta_file = self._paths(sid)
        try:
            return ScriptMeta(**json.loads(meta_file.read_text()))
        except FileNotFoundError:
            raise ScriptError(f"нет скрипта {sid}") from None

    def code(self, sid: str) -> str:
        self.meta(sid)
        return self._paths(sid)[0].read_text()

    def create(self, name: str, code: str | None = None) -> ScriptMeta:
        meta = ScriptMeta(id=secrets.token_hex(4), name=name.strip() or "Без имени")
        code_file, meta_file = self._paths(meta.id)
        self.tree(meta.id).write(meta.entry, (TEMPLATE if code is None else code).encode())
        self.tree(meta.id).set_meta(asdict(meta))
        return meta

    def update(self, sid: str, *, name: str | None = None, code: str | None = None,
               autostart: bool | None = None, entry: str | None = None,
               restart_on_save: bool | None = None) -> ScriptMeta:
        meta = self.meta(sid)
        tree = self.tree(sid)
        if entry is not None:
            if not entry.endswith(".py") or not tree.path(entry).is_file():
                raise ScriptError("точка входа должна быть существующим Python-файлом")
            meta.entry = entry
        if restart_on_save is not None:
            meta.restart_on_save = restart_on_save
        if code is not None:
            tree.write(meta.entry, code.encode())
        if name is not None:
            meta.name = name.strip() or meta.name
        if autostart is not None:
            meta.autostart = autostart
        meta.updated = time.time()
        tree.set_meta(asdict(meta))
        if code is not None:
            self.saved(sid)
        return meta

    def delete(self, sid: str) -> None:
        self.stop(sid)
        self.meta(sid)
        tree = self.tree(sid)
        from pioneer.dependencies import remove
        remove(tree.root)
        shutil.rmtree(tree.root)
        with self._lock:
            self._logs.pop(sid, None)
            self._status.pop(sid, None)

    # ---- запуск
    def saved(self, sid: str) -> None:
        if self.meta(sid).restart_on_save and self.status(sid).state == "running":
            self.restart(sid)

    def start(self, sid: str, file: str | None = None) -> Status:
        with self._lock:
            if sid in self._runners:
                raise ScriptError("скрипт уже запущен")
            meta = self.meta(sid)
            self._logs.setdefault(sid, collections.deque(maxlen=LOG_LINES))
            self._log(sid, "sys", f"запуск «{meta.name}»")
            path = self.tree(sid).path(file) if file is not None else self._paths(sid)[0]
            if not path.is_file() or path.suffix != ".py":
                raise ScriptError("нет Python-файла для запуска")
            runner = _Runner(self, meta, path)
            self._runners[sid] = runner
            self._set_status(sid, runner.status)
            return runner.status

    def stop(self, sid: str) -> None:
        with self._lock:
            runner = self._runners.get(sid)
        if runner:
            runner.stop()
            for _ in range(100):  # дождаться освобождения ресурсов (_wait в своём потоке)
                if sid not in self._runners:
                    break
                time.sleep(0.01)

    def restart(self, sid: str) -> Status:
        self.stop(sid)
        return self.start(sid)

    def start_autostart(self) -> None:
        for m in self.list():
            if m["autostart"]:
                try:
                    self.start(m["id"])
                except Exception as e:
                    log.error("автозапуск %s: %s", m["id"], e)

    def stop_all(self) -> None:
        for sid in list(self._runners):
            self.stop(sid)

    def status(self, sid: str) -> Status:
        return self._status.get(sid, Status())

    def log_tail(self, sid: str, n: int = LOG_LINES) -> list[dict]:
        return list(self._logs.get(sid, ()))[-n:]

    # ---- для LiveHub
    def drain(self) -> list[dict]:
        """Сообщения для WS: накопленные строки лога (с ограничением) и смены статуса."""
        msgs = []
        with self._lock:
            for sid, lines in self._pending_logs.items():
                if lines:
                    msgs.append({"type": "script_log", "id": sid, "lines": lines})
            for sid, n in self._dropped.items():
                msgs.append({"type": "script_log", "id": sid,
                             "lines": [{"t": time.time(), "stream": "sys", "text": f"… пропущено {n} строк"}]})
            for sid in self._status_dirty:
                msgs.append({"type": "script_status", "id": sid, "status": asdict(self.status(sid))})
            self._pending_logs, self._dropped, self._status_dirty = {}, {}, set()
        return msgs

    # ---- внутреннее
    def _log(self, sid: str, stream: str, text: str) -> None:
        line = {"t": time.time(), "stream": stream, "text": text}
        with self._lock:
            self._logs.setdefault(sid, collections.deque(maxlen=LOG_LINES)).append(line)
            pending = self._pending_logs.setdefault(sid, [])
            if len(pending) < FLUSH_MAX_LINES:
                pending.append(line)
            else:
                self._dropped[sid] = self._dropped.get(sid, 0) + 1

    def _set_status(self, sid: str, st: Status) -> None:
        with self._lock:
            self._status[sid] = st
            self._status_dirty.add(sid)

    def _finished(self, sid: str, runner: _Runner, st: Status) -> None:
        with self._lock:
            if self._runners.get(sid) is runner:
                del self._runners[sid]
            self._set_status(sid, st)
        self.on_pins(*[p for p in self.hw.board.pins])  # захваты сняты — обновить плату
        self.on_leds()
