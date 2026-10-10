"""Проекты (spec 3.2, 3.5, 3.7a, 4.2): хранение, пакеты операций, запуск в Engine, лог и live.

data/projects/<id>.json = {id, name, description, autostart, rev, created, updated, graph}.
Граф меняется только пакетами операций (apply_ops): rev растёт на каждый принятый пакет,
ответ содержит обратный пакет для undo. Запущенный проект исполняет граф той ревизии,
с которой стартовал; новая ревизия применяется кнопкой «Применить» (горячий перезапуск).
"""
from __future__ import annotations

import collections
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pioneer.engine.graph import Graph, Report, validate
from pioneer.engine.node import NodeError, NodeRegistry
from pioneer.engine.ops import OpError, apply_ops
from pioneer.engine.runtime import Engine, StartError
from pioneer.hw import HW

log = logging.getLogger(__name__)
LOG_LINES = 1000
FLUSH_MAX_LINES = 200


@dataclass
class ProjectMeta:
    id: str
    name: str
    description: str = ""
    autostart: bool = False
    rev: int = 0
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)


@dataclass
class Status:
    state: str = "stopped"  # stopped | running | error
    error: str | None = None
    started: float | None = None
    rev: int | None = None  # ревизия графа, с которой запущен


class ProjectError(Exception):
    def __init__(self, message: str, code: int = 409, detail: Any = None):
        super().__init__(message)
        self.code, self.detail = code, detail


class RevConflict(ProjectError):
    pass


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


class ProjectManager:
    def __init__(self, data_dir: Path, hw: HW, registry: NodeRegistry, engine: Engine | None = None):
        self.dir = data_dir / "projects"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.hw, self.registry = hw, registry
        self.engine = engine or Engine(hw, registry)
        self.engine.on_log = self._log
        self._lock = threading.RLock()
        self._status: dict[str, Status] = {}
        self._logs: dict[str, collections.deque] = {}
        self._pending_logs: dict[str, list[dict]] = {}
        self._dropped: dict[str, int] = {}
        self._status_dirty: set[str] = set()
        self.on_pins = lambda *pins: None  # LiveHub.mark
        self.on_leds = lambda: None

    # ---- хранилище
    def _path(self, pid: str) -> Path:
        if not isinstance(pid, str) or not re.fullmatch(r"[a-z0-9]{6,32}", pid):
            raise ProjectError(f"плохой id проекта {pid!r}", 404)
        return self.dir / f"{pid}.json"

    def _read(self, pid: str) -> tuple[ProjectMeta, Graph]:
        try:
            raw = json.loads(self._path(pid).read_text())
        except FileNotFoundError:
            raise ProjectError(f"нет проекта {pid}", 404) from None
        graph = Graph.from_json(raw.pop("graph", None))
        return ProjectMeta(**{k: v for k, v in raw.items() if k in ProjectMeta.__dataclass_fields__}), graph

    def _write(self, meta: ProjectMeta, graph: Graph) -> None:
        _atomic_write(self._path(meta.id), {**asdict(meta), "graph": graph.to_json()})

    def list(self) -> list[dict]:
        out = []
        for f in self.dir.glob("*.json"):
            try:
                meta, graph = self._read(f.stem)
            except (ProjectError, ValueError, KeyError, TypeError) as e:
                log.error("проект %s не читается: %s", f.name, e)
                continue
            out.append({**asdict(meta), "nodes": len(graph.nodes), "status": self.status(meta.id)})
        return sorted(out, key=lambda m: m["name"].lower())

    def get(self, pid: str) -> dict:
        meta, graph = self._read(pid)
        return {**asdict(meta), "graph": graph.to_json(), "status": self.status(pid),
                "validation": self._validate(pid, graph).to_json(), "manifest": self.manifest(pid)}

    def create(self, name: str, description: str = "", graph: dict | None = None) -> ProjectMeta:
        meta = ProjectMeta(id=secrets.token_hex(4), name=name.strip() or "Без имени", description=description)
        g = Graph.from_json(graph)
        if graph:
            rep = validate(g, self.registry, self.hw.board)
            if not rep.ok:
                raise ProjectError(f"граф с ошибками: {rep.text()}", 422, rep.to_json())
        with self._lock:
            self._write(meta, g)
        return meta

    def update_meta(self, pid: str, *, name: str | None = None, description: str | None = None,
                    autostart: bool | None = None) -> ProjectMeta:
        with self._lock:
            meta, graph = self._read(pid)
            if name is not None:
                meta.name = name.strip() or meta.name
            if description is not None:
                meta.description = description
            if autostart is not None:
                meta.autostart = autostart
            meta.updated = time.time()
            self._write(meta, graph)
        return meta

    def delete(self, pid: str) -> None:
        self._read(pid)
        self.stop(pid)
        self._path(pid).unlink(missing_ok=True)
        with self._lock:
            self._logs.pop(pid, None)
            self._status.pop(pid, None)

    def duplicate(self, pid: str) -> ProjectMeta:
        meta, graph = self._read(pid)
        return self.create(f"{meta.name} (копия)", meta.description, graph.to_json())

    # ---- операции (spec 3.7a)
    def _foreign_claims(self, pid: str) -> dict[str, tuple[str, str]]:
        me = f"project:{pid}"
        return {rid: (c.mode, ", ".join(n for o, n in c.owners.items() if o != me))
                for rid, c in self.hw.res.claims.items() if any(o != me for o in c.owners)}

    def _validate(self, pid: str, graph: Graph) -> Report:
        return validate(graph, self.registry, self.hw.board, foreign_claims=self._foreign_claims(pid))

    def validate_ops(self, pid: str, ops: list[dict]) -> dict:
        """Прогнать пакет без сохранения (для UI и AI-помощника)."""
        _, graph = self._read(pid)
        try:
            new, _, _ = apply_ops(graph, ops, self.registry, self.hw.board)
        except OpError as e:
            return {"ok": False, "can_start": False, "errors": [e.to_json()], "incomplete": [], "warnings": []}
        return self._validate(pid, new).to_json()

    def apply_ops(self, pid: str, ops: list[dict], base_rev: int | None = None) -> dict:
        with self._lock:
            meta, graph = self._read(pid)
            if base_rev is not None and base_rev != meta.rev:
                raise RevConflict(f"граф изменён в другой вкладке (ревизия {meta.rev}, у вас {base_rev})", 409,
                                  {"rev": meta.rev})
            try:
                new, inverse, created = apply_ops(graph, ops, self.registry, self.hw.board)
            except OpError as e:
                raise ProjectError(str(e), 422, {"errors": [e.to_json()]}) from None
            rep = self._validate(pid, new)
            if not rep.ok:
                raise ProjectError(f"пакет отклонён: {rep.text()}", 422, rep.to_json())
            meta.rev += 1
            meta.updated = time.time()
            self._write(meta, new)
            self._status_dirty.add(pid)
        return {"rev": meta.rev, "inverse": inverse, "created": created, "validation": rep.to_json()}

    # ---- запуск
    def start(self, pid: str) -> dict:
        meta, graph = self._read(pid)
        rep = self._validate(pid, graph)
        if not rep.can_start:
            self._fail(pid, f"граф не готов: {rep.text()}")
            raise ProjectError(f"граф не готов: {rep.text()}", 422, rep.to_json())
        self._logs.setdefault(pid, collections.deque(maxlen=LOG_LINES))
        try:
            self.engine.start_project(pid, meta.name, graph, meta.rev)
        except StartError as e:
            self._fail(pid, str(e))
            raise ProjectError(str(e)) from None
        self._set_status(pid, Status("running", started=time.time(), rev=meta.rev))
        self._pins_changed()
        return self.status(pid)

    def stop(self, pid: str) -> dict:
        if self.engine.stop_project(pid):
            self._set_status(pid, Status("stopped"))
            self._pins_changed()
        return self.status(pid)

    def restart(self, pid: str) -> dict:
        """«Применить»: перезапуск с текущей ревизией графа (состояние нод сбрасывается)."""
        self.stop(pid)
        return self.start(pid)

    def stop_all(self) -> None:
        for pid in list(self.engine.runtimes):
            self.stop(pid)

    def start_autostart(self) -> None:
        for m in self.list():
            if m["autostart"]:
                try:
                    self.start(m["id"])
                except ProjectError as e:
                    log.error("автозапуск проекта %s: %s", m["id"], e)

    def close(self) -> None:
        self.engine.close()

    def _fail(self, pid: str, error: str) -> None:
        self._log(pid, {"t": time.time(), "level": "error", "node": None, "text": f"не запущен: {error}"})
        self._set_status(pid, Status("error", error=error))

    def _pins_changed(self) -> None:
        self.on_pins(*self.hw.board.pins)
        self.on_leds()

    def status(self, pid: str) -> dict:
        st = asdict(self._status.get(pid, Status()))
        rt = self.engine.runtimes.get(pid)
        if rt is not None:
            try:
                st["dirty"] = rt.rev != self._read(pid)[0].rev
            except ProjectError:
                st["dirty"] = False
        else:
            st["dirty"] = False
        return st

    def manifest(self, pid: str) -> list[dict]:
        rt = self.engine.runtimes.get(pid)
        return [asdict(d) for d in rt.manifest()] if rt else []

    def ui(self, pid: str, node_id: str, value: Any) -> None:
        try:
            self.engine.ui(pid, node_id, value)
        except NodeError as e:
            raise ProjectError(str(e)) from None

    # ---- лог и live
    def log_tail(self, pid: str, n: int = LOG_LINES) -> list[dict]:
        return list(self._logs.get(pid, ()))[-n:]

    def live_snapshot(self, pid: str) -> dict:
        rt = self.engine.runtimes.get(pid)
        return self.engine.call(rt.snapshot) if rt else {"values": {}, "events": {}, "views": {}, "nodes": {}}

    def _log(self, pid: str, line: dict) -> None:
        with self._lock:
            self._logs.setdefault(pid, collections.deque(maxlen=LOG_LINES)).append(line)
            pending = self._pending_logs.setdefault(pid, [])
            if len(pending) < FLUSH_MAX_LINES:
                pending.append(line)
            else:
                self._dropped[pid] = self._dropped.get(pid, 0) + 1

    def _set_status(self, pid: str, st: Status) -> None:
        with self._lock:
            self._status[pid] = st
            self._status_dirty.add(pid)

    def drain(self) -> list[dict]:
        """Сообщения для WS: лог, статусы, live-значения сокетов (≤ 20 Гц — частота LiveHub)."""
        msgs = []
        with self._lock:
            for pid, lines in self._pending_logs.items():
                if lines:
                    msgs.append({"type": "project_log", "id": pid, "lines": lines})
            for pid, n in self._dropped.items():
                msgs.append({"type": "project_log", "id": pid, "lines": [
                    {"t": time.time(), "level": "info", "node": None, "text": f"… пропущено {n} строк"}]})
            dirty = self._status_dirty
            self._pending_logs, self._dropped, self._status_dirty = {}, {}, set()
        for pid in dirty:
            msgs.append({"type": "project_status", "id": pid, "status": self.status(pid)})
        for pid, rt in list(self.engine.runtimes.items()):
            live = rt.drain_live()
            if live:
                msgs.append({"type": "project_live", "id": pid, **live})
        return msgs
