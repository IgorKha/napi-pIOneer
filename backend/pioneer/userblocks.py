"""Python-блоки пользователя (spec 3.6 b): хранение, проверка, регистрация, исполнение.

data/blocks/<slug>/ — код и ассеты, pioneer.json — метаданные и схемы.
Схема извлекается отдельным процессом (`python -m pioneer.blockhost --schema`): core код
пользователя не исполняет. Типы регистрируются как `user.<id>@<version>`; нода в графе —
BlockProxy: входы/выходы живут в движке, обработчики — в worker-процессе проекта
(один на проект), по сокету, с таймаутом 1 с на вызов.

Сохранение блока не трогает запущенные проекты: у них появляется «граф изменён» —
новая версия подхватывается кнопкой «Применить».
"""
from __future__ import annotations

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
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, ClassVar

from pioneer.codetree import CodeTree, migrate
from pioneer.config import REPO_ROOT
from pioneer.engine.node import In, Node, NodeError, NodeRegistry, Out, Param, node
from pioneer.engine.types import Origin, jsonable

log = logging.getLogger(__name__)
CALL_TIMEOUT_S = 1.0
WORKER_START_TIMEOUT_S = 15.0
SCHEMA_TIMEOUT_S = 15.0  # импорт на A35 небыстрый
PREFIX = "user."

TEMPLATE = '''from pioneer.block import node, Node, In, Out, Param, Number, Bool


@node(id="hysteresis", title="Гистерезис (мой)", category="Мои блоки")
class Hysteresis(Node):
    """Включает выход, когда значение ≥ «Верхний», выключает — когда ≤ «Нижний»."""

    value = In(Number, 0, title="Значение")
    low = In(Number, 30, title="Нижний")
    high = In(Number, 35, title="Верхний")
    invert = Param(bool, False, title="Инвертировать")
    out = Out(Bool, title="Выход")

    def setup(self):
        self.state = False

    def on_change(self, changed):
        if self.value >= self.high:
            self.state = True
        elif self.value <= self.low:
            self.state = False
        self.out.set(self.state != self.invert)
'''


def _env() -> dict:
    return {**os.environ, "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": str(REPO_ROOT / "backend")}


def extract_schema(path: Path) -> dict:
    """{"ok": true, "nodes": [...]} или {"ok": false, "error": "..."} — в отдельном процессе."""
    try:
        r = subprocess.run([sys.executable, "-m", "pioneer.blockhost", "--schema", str(path)], env=_env(),
                           capture_output=True, text=True, timeout=SCHEMA_TIMEOUT_S, cwd=path.parent)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"импорт блока дольше {SCHEMA_TIMEOUT_S:g} с (бесконечный цикл на верхнем уровне?)"}
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": (r.stderr.strip().splitlines() or ["процесс проверки упал"])[-1]}


# ---------------------------------------------------------------- worker блоков проекта
class BlockHostClient:
    """Процесс pioneer.blockhost одного проекта."""

    def __init__(self, on_async, on_log, on_hardware=None):
        self.on_async, self.on_log = on_async, on_log
        self.on_hardware = on_hardware
        parent, child = socket.socketpair()
        self.sock = parent
        env = {**_env(), "PIONEER_WORKER_FD": str(child.fileno())}
        self.proc = subprocess.Popen([sys.executable, "-m", "pioneer.blockhost"], pass_fds=[child.fileno()],
                                     env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        child.close()
        self._ids = iter(range(1, 1 << 62))
        self._pending: dict[int, list] = {}
        self._send_lock = threading.Lock()
        self.dead: str | None = None
        self._ready = threading.Event()
        threading.Thread(target=self._reader, name="blockhost-rpc", daemon=True).start()
        threading.Thread(target=self._output, name="blockhost-out", daemon=True).start()
        # Cold Python imports on the A35 must not consume the handler's 1 s budget.
        if not self._ready.wait(WORKER_START_TIMEOUT_S) or self.dead:
            self.dead = self.dead or f"процесс блоков не готов за {WORKER_START_TIMEOUT_S:g} с"
            self.close()
            raise NodeError(self.dead)

    def _reader(self) -> None:
        try:
            for line in self.sock.makefile("r", encoding="utf-8"):
                msg = json.loads(line)
                if msg.get("ready") is True:
                    self._ready.set()
                elif "hardware" in msg:
                    try:
                        result=self.on_hardware(msg["node"],msg["op"],msg["args"])
                        reply={"hardware_reply":msg["hardware"],"ok":True,"result":result}
                    except Exception as e:
                        reply={"hardware_reply":msg["hardware"],"ok":False,"error":str(e)[:1000]}
                    with self._send_lock:self.sock.sendall((json.dumps(reply)+"\n").encode())
                elif "async" in msg:
                    self.on_async(msg["async"], msg["actions"])
                elif slot := self._pending.pop(msg.get("id"), None):
                    slot[1] = msg
                    slot[0].set()
        except (OSError, ValueError):
            pass
        self.dead = self.dead or "процесс блоков завершился"
        self._ready.set()
        for slot in list(self._pending.values()):
            slot[0].set()

    def _output(self) -> None:  # print() из блоков и трейсбеки — в лог проекта
        for raw in iter(self.proc.stdout.readline, b""):
            text = raw.decode("utf-8", "replace").rstrip()
            if text:
                self.on_log(text)

    def call(self, req: dict, timeout: float = CALL_TIMEOUT_S) -> list:
        if self.dead:
            raise NodeError(f"{self.dead} — «Применить» перезапустит проект")
        rid = next(self._ids)
        slot = [threading.Event(), None]
        self._pending[rid] = slot
        data = (json.dumps({"id": rid, **req}, ensure_ascii=False, default=str) + "\n").encode()
        try:
            with self._send_lock:
                self.sock.sendall(data)
        except OSError:
            self.dead = "процесс блоков недоступен"
            raise NodeError(self.dead) from None
        if not slot[0].wait(timeout):
            self._pending.pop(rid, None)
            self.dead = f"блок не ответил за {timeout:g} с, процесс блоков остановлен"
            self.close()
            raise NodeError(self.dead)
        resp = slot[1]
        if resp is None:
            raise NodeError(f"{self.dead} — «Применить» перезапустит проект")
        if not resp.get("ok"):
            raise NodeError(resp.get("error", "ошибка блока"))
        return resp.get("actions", [])

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(1.0)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            self.sock.close()
        except OSError:
            pass


class BlockProxy(Node):
    """Нода Python-блока в движке: обработчики — в worker-процессе проекта."""
    FILE: ClassVar[str]
    BLOCK_ID: ClassVar[str]

    def _host(self) -> BlockHostClient:
        return self._rt.blockhost()

    def _inputs(self) -> dict:
        return {name: jsonable(getattr(self, name), 100_000)
                for name, spec in self.TYPE.inputs.items() if spec.type != "Event"}

    def _call(self, req: dict) -> None:
        self._apply(self._host().call(req))

    def _apply(self, actions: list) -> None:
        for act in actions:
            kind = act[0]
            if kind == "log":
                self.log(act[1])
            elif kind == "error":
                raise NodeError(act[1])
            elif kind in ("set", "emit"):
                spec = self.TYPE.outputs.get(act[1])
                if spec is None:
                    raise NodeError(f"нет выхода {act[1]!r}")
                port = getattr(self, act[1])
                if kind == "set":
                    if spec.type == "Event":
                        raise NodeError(f"{act[1]}: Event-выход — используйте emit()")
                    port.set(act[2])
                else:
                    if spec.type != "Event":
                        raise NodeError(f"{act[1]}: emit() только для Event-выходов")
                    port.emit(act[2])

    def setup(self):
        self._rt.block_files[self.FILE] = os.path.getmtime(self.FILE)
        try:
            self._call({"op": "load", "file": self.FILE, "block": self.BLOCK_ID, "node": self.id, "params": self.params})
        except NodeError as e:
            raise NodeError(f"блок: {e}") from None

    def start(self):
        self._call({"op": "call", "method": "start", "node": self.id, "inputs": self._inputs()})

    def on_change(self, changed):
        self._call({"op": "call", "method": "on_change", "node": self.id, "inputs": self._inputs(),
                    "changed": sorted(changed)})

    def on_event(self, name, ev):
        self._call({"op": "call", "method": "on_event", "node": self.id, "inputs": self._inputs(),
                    "name": name, "payload": jsonable(ev.payload, 100_000)})

    def stop(self):
        host = self._rt._blockhost
        if host is not None and not host.dead:
            try:
                host.call({"op": "stop", "node": self.id})
            except NodeError:
                pass


def open_blockhost(rt) -> BlockHostClient:
    """Фабрика для ProjectRuntime.blockhost(): async-действия блоков — новой волной."""
    def on_async(node_id: str, actions: list) -> None:
        node = rt.nodes.get(node_id)
        if isinstance(node, BlockProxy):
            rt.engine.inject_call(rt.project_id, Origin("block", node_id), lambda: node._apply(actions), node=node)

    def on_log(text: str) -> None:
        rt.engine.loop.call_soon_threadsafe(rt.log, text)
    from pioneer.block_hardware import HardwareRPC
    return BlockHostClient(on_async, on_log, HardwareRPC(rt).call)


# ---------------------------------------------------------------- хранилище и регистрация
def _proxy_type(schema: dict, file: Path, registry: NodeRegistry, *, prefix=PREFIX, version=None):
    attrs: dict[str, Any] = {"FILE": str(file), "BLOCK_ID": schema["id"]}
    for i in schema["inputs"]:
        attrs[i["name"]] = In(i["type"], i.get("default"), title=i.get("title"), doc=i.get("doc") or "",
                              min=i.get("min"), max=i.get("max"), unit=i.get("unit"))
    for o in schema["outputs"]:
        attrs[o["name"]] = Out(o["type"], title=o.get("title"), doc=o.get("doc") or "")
    for p in schema["params"]:
        attrs[p["name"]] = Param(p["kind"], p.get("default"), title=p.get("title") or p["name"], doc=p.get("doc") or "",
                                 choices=[tuple(c) for c in p.get("choices") or ()] or None,
                                 min=p.get("min"), max=p.get("max"), unit=p.get("unit"), modes=tuple(p.get("modes", ())), required=p.get("required", False))
    cls = type(f"Block_{re.sub(r'\\W', '_', schema['id'])}", (BlockProxy,), attrs)
    doc = schema.get("doc") or f"Пользовательский блок «{schema['title']}» (Python, файл {file.name})."
    return node(prefix + schema["id"], title=schema["title"], category=schema.get("category") or "Мои блоки",
                doc=doc, version=version if version is not None else schema.get("version", 1), color=schema.get("color") or "#7a5a2e",
                breaks_cycle=schema.get("breaks_cycle", False), registry=registry)(cls)


class BlockError(Exception):
    def __init__(self, message: str, code: int = 409):
        super().__init__(message)
        self.code = code


class BlockManager:
    def __init__(self, data_dir: Path, registry: NodeRegistry):
        self.dir = data_dir / "blocks"
        migrate(self.dir, "block.py")
        self.registry = registry
        if SCRIPT_KEY not in registry.types:
            register_script_type(registry, data_dir / "script_cache")
        self._types: dict[str, list[str]] = {}  # slug -> ключи зарегистрированных типов
        self._lock = threading.RLock()
        for meta in sorted(self.dir.glob("*/pioneer.json")):
            try:
                m = json.loads(meta.read_text())
                if m.get("nodes"):
                    self._register(meta.parent.name, m["nodes"])
            except (ValueError, KeyError, TypeError) as e:
                log.error("блок %s не загружен: %s", meta.stem, e)

    def _paths(self, slug: str) -> tuple[Path, Path]:
        if not re.fullmatch(r"[a-z0-9]{6,32}", slug or ""):
            raise BlockError(f"плохой id блока {slug!r}", 404)
        tree = CodeTree(self.dir / slug)
        meta = tree.path("pioneer.json", metadata=True)
        entry = tree.meta().get("entry", "block.py") if meta.exists() else "block.py"
        return tree.path(entry), meta

    def _meta(self, slug: str) -> dict:
        code, meta = self._paths(slug)
        if not code.exists():
            raise BlockError(f"нет блока {slug}", 404)
        try:
            return json.loads(meta.read_text())
        except (OSError, ValueError):
            return {"name": slug, "updated": code.stat().st_mtime, "nodes": [], "error": None}

    def list(self) -> list[dict]:
        out = []
        for f in self.dir.glob("*/pioneer.json"):
            m = self._meta(f.parent.name)
            out.append({"id": f.parent.name, "name": m.get("name", f.parent.name), "updated": m.get("updated"),
                        "types": [PREFIX + n["id"] for n in m.get("nodes", [])], "error": m.get("error")})
        return sorted(out, key=lambda b: b["name"].lower())

    def get(self, slug: str) -> dict:
        m = self._meta(slug)
        return {"id": slug, **m, "code": self._paths(slug)[0].read_text(),
                "types": [PREFIX + n["id"] for n in m.get("nodes", [])]}

    def create(self, name: str, code: str | None = None) -> dict:
        slug = secrets.token_hex(4)
        return self.save(slug, code if code is not None else TEMPLATE, name=name.strip() or "Мой блок", new=True)

    def save(self, slug: str, code: str, name: str | None = None, *, new: bool = False) -> dict:
        """Сохранить код и, если он корректен, перерегистрировать типы. Код сохраняется
        и с ошибкой (работа не теряется); тогда в палитре остаётся прежняя версия."""
        code_file, meta_file = self._paths(slug)
        with self._lock:
            old = {} if new else self._meta(slug)
            tree = CodeTree(code_file.parent)
            tree.write(code_file.name, code.encode())
            res = extract_schema(code_file)
            meta = {"name": name or old.get("name") or "Мой блок", "updated": time.time(),
                    "nodes": old.get("nodes", []), "error": None, "entry": old.get("entry", "block.py")}
            if res.get("ok"):
                try:
                    self._check_ids(slug, res["nodes"])
                    self._register(slug, res["nodes"])
                    meta["nodes"] = res["nodes"]
                except (ValueError, AssertionError, TypeError) as e:
                    meta["error"] = str(e) or type(e).__name__
            else:
                meta["error"] = res.get("error")
            tree.set_meta(meta)
        return {"id": slug, **meta, "types": [PREFIX + n["id"] for n in meta["nodes"]]}

    def validate(self, code: str) -> dict:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "block.py"
            p.write_text(code)
            return extract_schema(p)

    def delete(self, slug: str) -> None:
        code, meta = self._paths(slug)
        with self._lock:
            for key in self._types.pop(slug, []):
                self.registry.remove(key)
            tree = CodeTree(meta.parent)
            tree.files()
            from pioneer.dependencies import remove
            remove(tree.root)
            shutil.rmtree(tree.root)

    def _check_ids(self, slug: str, nodes: list[dict]) -> None:
        ids = [n["id"] for n in nodes]
        for i in ids:
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", i):
                raise ValueError(f"id блока {i!r}: латиница в нижнем регистре, цифры, _")
        for other, keys in self._types.items():
            if other == slug:
                continue
            for key in keys:
                if key.split("@")[0][len(PREFIX):] in ids:
                    raise ValueError(f"блок с id {key.split('@')[0]!r} уже есть в «{self._meta(other).get('name')}»")

    def _register(self, slug: str, nodes: list[dict]) -> None:
        code_file = self._paths(slug)[0]
        old = self._types.pop(slug, [])
        for key in old:
            self.registry.remove(key)
        new: list[str] = []
        try:
            for schema in nodes:
                cls = _proxy_type(schema, code_file, self.registry)
                new.append(cls.TYPE.key)
        except Exception:
            for key in new:
                self.registry.remove(key)
            self._types[slug] = []
            raise
        self._types[slug] = new


# ---------------------------------------------------------------- нода «Скрипт»
SCRIPT_KEY = "builtin.script@1"
SCRIPT_TEMPLATE = '''from pioneer.block import node, Node, In, Out, Number


@node(id="script", title="Скрипт")
class Script(Node):
    """Код этой ноды. Входы/выходы объявляются как в своих блоках."""

    value = In(Number, 0, title="Вход")
    out = Out(Number, title="Выход")

    def on_change(self, changed):
        self.out.set(self.value * 2)
'''
SCRIPT_DOC = """
Быстрый одноразовый блок: код на Python живёт прямо в ноде этого проекта (в палитру не
попадает). Синтаксис — как у своих блоков (`pioneer.block`): входы, выходы и параметры
ноды берутся из кода — после правки кода нода перестраивается, провода к исчезнувшим
сокетам снимаются (вернутся по ⌘Z).

Исполняется в процессе блоков проекта, обработчик — не дольше 1 с. Для повторного
использования в других проектах — вынесите код на страницу «Блоки».
"""


class ScriptTypes:
    """Resolver динамического типа «Скрипт»: схема по хэшу кода, кэш на диске
    (data/script_cache/<sha>.py + .json) — после перезапуска сервиса код не перепроверяется."""

    def __init__(self, cache_dir: Path):
        self.dir = cache_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._mem: dict[str, Any] = {}
        self._lock = threading.Lock()

    def __call__(self, params: dict):
        import hashlib
        code = params.get("code") or SCRIPT_TEMPLATE
        h = hashlib.sha256(code.encode()).hexdigest()[:20]
        with self._lock:
            if h in self._mem:
                return self._mem[h]
            root = self.dir / h
            root.mkdir(exist_ok=True)
            py, js = root / "block.py", root / "schema.json"
            if not py.exists():
                py.write_text(code)
            try:
                res = json.loads(js.read_text())
            except (OSError, ValueError):
                res = extract_schema(py)
                js.write_text(json.dumps(res, ensure_ascii=False))
            self._mem[h] = nt = self._build(res, py)
            return nt

    def _build(self, res: dict, py: Path):
        reg = NodeRegistry()
        code_param = {"name": "code", "kind": "code", "title": "Код", "doc": "Python (pioneer.block)",
                      "default": SCRIPT_TEMPLATE}
        error = None
        schema = None
        if not res.get("ok"):
            error = f"скрипт: {res.get('error')}"
        elif len(res["nodes"]) != 1:
            error = "скрипт: в коде должна быть ровно одна нода @node"
        else:
            schema = res["nodes"][0]
            if any(p["name"] == "code" for p in schema["params"]):
                error = "скрипт: параметр с именем code занят"
        if error:
            schema = {"id": "script", "title": "Скрипт", "inputs": [], "outputs": [], "params": []}
        schema = {**schema, "params": [code_param, *schema["params"]], "category": "Скрипт", "color": "#6a4c93",
                  "doc": (schema.get("doc") or "") + "\n\n" + SCRIPT_DOC.strip()}
        try:
            cls = _proxy_type(schema, py, reg)
        except (ValueError, AssertionError, TypeError) as e:
            schema = {**schema, "inputs": [], "outputs": [], "params": [code_param]}
            cls = _proxy_type(schema, py, NodeRegistry())
            error = f"скрипт: {e}"
        nt = cls.TYPE
        nt.error = error
        return nt


def register_script_type(registry: NodeRegistry, cache_dir: Path) -> None:
    class ScriptBase(BlockProxy):
        FILE = ""
        BLOCK_ID = "script"
        code = Param("code", SCRIPT_TEMPLATE, title="Код", doc="Python (pioneer.block)")
    node("builtin.script", title="Скрипт", category="Скрипт", color="#6a4c93", doc=SCRIPT_DOC,
         registry=registry)(ScriptBase)
    registry.dynamic[SCRIPT_KEY] = ScriptTypes(cache_dir)
