"""Исполнение графов (spec 3.4): реактивная push-модель, волны-транзакции.

Engine — один поток с asyncio-циклом на все запущенные проекты. Всё, что меняет граф
снаружи (фронт GPIO из потока HAL, таймер, значение UI-контрола, команда транспорта в
фазе 2), входит единым путём — Engine.inject / inject_call — и начинает новую **волну**.

Волна: изменённые выходы помечают зависимые ноды; ноды пересчитываются по возрастанию
топологического ранга — каждая видит согласованные входы (без «глитчей»). Волна несёт
origin (кто начал) и, опционально, контекст запроса; продолжения через later() (Delay,
Pulse) наследуют и то и другое. По завершении волны вызываются хуки (on_wave_done), а
запрос, у которого не осталось продолжений, — завершается (авто-подтверждение команды).

Ошибка в ноде изолирована: нода «краснеет», текст — в лог (с троттлингом), проект живёт.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import heapq
import itertools
import logging
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pioneer.engine.graph import Graph, param_resources, ranks
from pioneer.engine.node import Declaration, In, Node, NodeError, NodeRegistry, NodeType, Out
from pioneer.engine.types import EventValue, Origin, RequestContext, Sample, coerce, jsonable
from pioneer.hw import HW
from pioneer.resources import ResourceConflict

log = logging.getLogger(__name__)

MAX_EVALS_PER_WAVE = 10_000
ERROR_HOLD_S = 5.0  # столько нода остаётся красной после последней ошибки
ERROR_LOG_EVERY_S = 5.0  # одинаковая ошибка ноды пишется в лог не чаще


class StartError(Exception):
    pass


# ---------------------------------------------------------------- волна и таймеры
@dataclass
class Wave:
    id: int
    origin: Origin
    ctx: RequestContext | None
    heap: list = field(default_factory=list)
    queued: set[str] = field(default_factory=set)
    dirty: dict[str, set[str]] = field(default_factory=dict)
    events: dict[str, list[tuple[str, EventValue]]] = field(default_factory=dict)
    evals: int = 0
    nodes_run: list[str] = field(default_factory=list)


class Timer:
    """Отложенный вызов ноды; cancel() снимает его (и продолжение запроса)."""

    def __init__(self, rt: ProjectRuntime, ctx: RequestContext | None = None):
        self.rt, self.ctx = rt, ctx
        self.handle: asyncio.TimerHandle | None = None
        self.active = True

    def cancel(self) -> None:
        if not self.active:
            return
        self.active = False
        if self.handle:
            self.handle.cancel()
        self.rt.timers.discard(self)
        if self.ctx is not None:
            self.rt._ctx_release(self.ctx)


@dataclass
class _NodeError:
    message: str
    ts: float
    logged: float
    repeats: int = 0


# ---------------------------------------------------------------- железо проекта
class ProjectHW:
    """HW от имени проекта: owner = project:<id>; слушатели фронтов — на ноду."""

    def __init__(self, hw: HW, owner: str, owner_name: str):
        self.hw, self.owner, self.owner_name = hw, owner, owner_name
        self.board = hw.board

    def input(self, node: Node, pin: str, debounce_ms: int, on_edge) -> None:
        self.hw.acquire(self.owner, self.owner_name, pin, "gpio_in", debounce_ms=debounce_ms,
                        on_edge=on_edge, listener_key=f"{self.owner}#{node.id}")

    def output(self, pin: str, initial: bool) -> None:
        self.hw.acquire(self.owner, self.owner_name, pin, "gpio_out", initial=initial)

    def pwm(self, pin: str) -> None:
        self.hw.acquire(self.owner, self.owner_name, pin, "soft_pwm")

    def adc(self, pin: str) -> None:
        self.hw.acquire(self.owner, self.owner_name, pin, "adc")

    def write(self, pin: str, value: bool) -> None:
        self.hw.write(pin, value)

    def set_pwm(self, pin: str, *, freq: float | None = None, duty: float | None = None) -> None:
        self.hw.set_pwm(pin, freq=freq, duty=duty)

    def value(self, pin: str):
        return self.hw.value(pin)

    def uart(self, iface: str, baud: int):
        return self.hw.uart(self.owner, self.owner_name, iface, baud=baud)

    def led(self, led: str):
        return self.hw.led(self.owner, self.owner_name, led)

    def release(self, resource: str, *, safe_value: bool = False) -> None:
        self.hw.release(self.owner, resource, safe_value=safe_value)


# ---------------------------------------------------------------- проект
class ProjectRuntime:
    def __init__(self, engine: Engine, project_id: str, name: str, graph: Graph, rev: int = 0,
                 aliases: dict[str, list[str]] | None = None):
        self.engine, self.project_id, self.name, self.graph, self.rev = engine, project_id, name, graph, rev
        # live-ключ внутреннего выхода группы → ключи выходов нод-групп (граф развёрнут, groups.py)
        self.aliases = aliases or {}
        self.group_revs: dict[str, int] = {}
        self.owner, self.owner_name = f"project:{project_id}", f"проект «{name}»"
        self.hw = ProjectHW(engine.hw, self.owner, self.owner_name)
        self.types: dict[str, NodeType] = {nid: engine.registry.for_node(n) for nid, n in graph.nodes.items()}
        for nid, nt in self.types.items():
            if nt.error:
                raise StartError(f"{nid}: {nt.error}")
        self.nodes: dict[str, Node] = {
            nid: self.types[nid].cls(self, nid, dict(n.params), n.label) for nid, n in graph.nodes.items()}
        self.rank = ranks(graph, self.types)
        self.order = sorted(self.nodes, key=lambda n: (self.rank[n], n))
        self.fanout: dict[tuple[str, str], list[tuple[str, str]]] = {}
        self.edge_to: dict[tuple[str, str], tuple[str, str]] = {}
        for e in graph.edges:
            self.fanout.setdefault(e.src, []).append(e.dst)
            self.edge_to[e.dst] = e.src
        self.samples: dict[tuple[str, str], Sample] = {}
        self.wave: Wave | None = None
        self.timers: set[Timer] = set()
        self.running = False
        self.started: float | None = None
        self.errors: dict[str, _NodeError] = {}
        self.views: dict[str, dict] = {}
        self._blockhost = None  # процесс Python-блоков проекта (по требованию)
        self.block_files: dict[str, float] = {}  # файл блока -> mtime при старте («граф изменён»)
        # live (читается из потока LiveHub)
        self._live_lock = threading.Lock()
        self._live_values: dict[str, dict] = {}
        self._live_events: dict[str, dict] = {}
        self._live_views: dict[str, dict] = {}
        self._live_states: set[str] = set()
        self._event_counts: dict[str, int] = {}

    # ---- жизненный цикл (поток движка)
    def manifest(self) -> list[Declaration]:
        out = []
        for nid in self.order:
            for d in self.nodes[nid].declarations():
                out.append(Declaration(d.kind, d.name, d.schema, nid))
        return out

    def resources(self) -> dict[str, str]:
        wants: dict[str, str] = {}
        for nid, n in self.nodes.items():
            res = param_resources(self.types[nid], n.params, self.engine.hw.board)
            res.update(n.resources())
            for rid, mode in res.items():
                if wants.get(rid, mode) != mode:
                    raise StartError(f"{rid} нужен в двух режимах: {wants[rid]} и {mode}")
                wants[rid] = mode
        return wants

    def start(self) -> None:
        try:
            self.engine.hw.res.claim_many(self.owner, self.owner_name, self.resources())
        except ResourceConflict as e:
            raise StartError(str(e)) from None
        try:
            for nid in self.order:
                node = self.nodes[nid]
                try:
                    node.setup()
                except (NodeError, ResourceConflict, OSError, ValueError, KeyError) as e:
                    raise StartError(f"{node.title}: {_err_text(e)}") from None
        except BaseException:
            self._teardown()
            raise
        self.running, self.started = True, time.time()
        self.log("запущен")
        self.run_wave(Origin("start"), None, self._start_nodes)

    def _start_nodes(self) -> None:
        for nid in self.order:
            node = self.nodes[nid]
            self._guard(node, node.start)

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        for t in list(self.timers):
            t.cancel()
        self._teardown()
        self.log("остановлен")

    def blockhost(self):
        """Worker-процесс Python-блоков этого проекта (создаётся первой нодой-блоком)."""
        if self._blockhost is None:
            from pioneer.userblocks import open_blockhost
            self._blockhost = open_blockhost(self)
        return self._blockhost

    def _teardown(self) -> None:
        for nid in reversed(self.order):
            node = self.nodes[nid]
            try:
                node.stop()
            except Exception as e:  # стоп не должен застревать на одной ноде
                self.log(f"{node.title}: ошибка при остановке: {_err_text(e)}", node=node, level="error")
        if self._blockhost is not None:
            self._blockhost.close()
            self._blockhost = None
        self.engine.hw.release(self.owner, safe_value=False)

    # ---- волна
    def run_wave(self, origin: Origin, ctx: RequestContext | None, fn: Callable[[], None],
                 node: Node | None = None) -> Wave | None:
        if self.wave is not None:  # уже внутри волны (синхронный вызов из ноды) — та же волна
            self._guard(node, fn) if node else fn()
            return None
        w = self.wave = Wave(next(self.engine._wave_ids), origin, ctx)
        try:
            if node is not None:
                self._guard(node, fn)
            else:
                fn()
            while w.heap:
                _, _, nid = heapq.heappop(w.heap)
                w.queued.discard(nid)
                w.evals += 1
                if w.evals > MAX_EVALS_PER_WAVE:
                    self.log(f"волна остановлена: больше {MAX_EVALS_PER_WAVE} пересчётов — похоже на "
                             f"бесконечный цикл через {self.nodes[nid].title}", level="error")
                    break
                n = self.nodes[nid]
                w.nodes_run.append(nid)
                for sock, ev in w.events.pop(nid, ()):
                    self._guard(n, n.on_event, sock, ev)
                changed = w.dirty.pop(nid, None)
                if changed:
                    self._guard(n, n.on_change, changed)
        finally:
            self.wave = None
            for hook in self.engine.wave_hooks:
                try:
                    hook(self, w)
                except Exception:
                    log.exception("on_wave_done")
            if ctx is not None:
                self._ctx_check(ctx)
        return w

    def _queue(self, w: Wave, nid: str) -> None:
        if nid not in w.queued:
            w.queued.add(nid)
            heapq.heappush(w.heap, (self.rank[nid], next(self.engine._seq), nid))

    def _in_wave(self, fn: Callable[[Wave], None]) -> None:
        if self.wave is not None:
            fn(self.wave)
        else:  # выход выставлен вне волны (не должно быть) — своя волна
            self.run_wave(Origin("system"), None, lambda: fn(self.wave))

    def set_output(self, node: Node, spec: Out, value: Any) -> None:
        if spec.type == "Event":
            raise NodeError(f"{spec.name}: Event-выход — используйте emit()")
        value = coerce(value, spec.type)
        key = (node.id, spec.name)

        def do(w: Wave):
            prev = self.samples.get(key)
            if prev is not None and prev.value == value and type(prev.value) is type(value):
                return
            s = self.samples[key] = Sample(value, w.origin, time.time())
            with self._live_lock:
                k = f"{node.id}.{spec.name}"
                entry = {"v": jsonable(value), "o": str(s.origin), "t": s.ts}
                self._live_values[k] = entry
                for a in self.aliases.get(k, ()):
                    self._live_values[a] = entry
            for dst_node, dst_sock in self.fanout.get(key, ()):
                w.dirty.setdefault(dst_node, set()).add(dst_sock)
                self._queue(w, dst_node)
        self._in_wave(do)

    def emit(self, node: Node, spec: Out, payload: Any = None) -> None:
        if spec.type != "Event":
            raise NodeError(f"{spec.name}: emit() только для Event-выходов")
        key = f"{node.id}.{spec.name}"

        def do(w: Wave):
            ev = EventValue(time.time(), payload, w.ctx, w.origin)
            with self._live_lock:
                n = self._event_counts[key] = self._event_counts.get(key, 0) + 1
                self._live_events[key] = {"n": n, "t": ev.ts, "o": str(w.origin), "p": jsonable(payload, 200)}
                for a in self.aliases.get(key, ()):
                    self._event_counts[a] = n
                    self._live_events[a] = self._live_events[key]
            for dst_node, dst_sock in self.fanout.get((node.id, spec.name), ()):
                w.events.setdefault(dst_node, []).append((dst_sock, ev))
                self._queue(w, dst_node)
        self._in_wave(do)

    # ---- входы
    def input_sample(self, node: Node, name: str) -> Sample | None:
        src = self.edge_to.get((node.id, name))
        return self.samples.get(src) if src else None

    def input_value(self, node: Node, spec: In) -> Any:
        if spec.type == "Event":
            return None
        src = self.edge_to.get((node.id, spec.name))
        if src is not None:
            s = self.samples.get(src)
            return coerce(s.value, spec.type) if s is not None else coerce(spec.default, spec.type)
        inline = self.graph.nodes[node.id].inputs.get(spec.name)
        return coerce(inline if inline is not None else spec.default, spec.type)

    def is_connected(self, node_id: str, name: str) -> bool:
        return (node_id, name) in self.edge_to

    def sample(self, node_id: str, output: str) -> Sample | None:
        return self.samples.get((node_id, output))

    # ---- таймеры
    def later(self, node: Node, seconds: float, fn: Callable[[], None]) -> Timer:
        w = self.wave
        ctx = w.ctx if w else None
        origin = w.origin.again() if w else Origin("timer", node.id)
        t = Timer(self, ctx)
        if ctx is not None:
            ctx.pending += 1

        def fire():
            if not t.active:
                return
            t.active = False
            self.timers.discard(t)
            if ctx is not None:
                ctx.pending -= 1
            if self.running:
                self.run_wave(origin, ctx, fn, node)
            elif ctx is not None:
                self._ctx_check(ctx)
        t.handle = self.engine.loop.call_later(max(0.0, seconds), fire)
        self.timers.add(t)
        return t

    def run_async(self, node: Node, work: Callable[[], Any], done: Callable[[Any, BaseException | None], None]) -> None:
        """work() — в пуле потоков (сеть, долгий ввод-вывод); done(result, error) — новой волной
        с тем же origin и контекстом запроса (запрос не завершится, пока работа идёт)."""
        w = self.wave
        ctx = w.ctx if w else None
        origin = w.origin.again() if w else Origin("system", node.id)
        if ctx is not None:
            ctx.pending += 1
        fut = self.engine.loop.run_in_executor(None, work)

        def finished(f):
            if ctx is not None:
                ctx.pending -= 1
            err = f.exception()
            result = None if err else f.result()
            if self.running and self.nodes.get(node.id) is node:
                self.run_wave(origin, ctx, lambda: done(result, err), node)
            elif ctx is not None:
                self._ctx_check(ctx)
        fut.add_done_callback(finished)

    def every(self, node: Node, seconds: float, fn: Callable[[], None], origin: Origin | None) -> Timer:
        if seconds <= 0:
            raise NodeError("период должен быть > 0")
        t = Timer(self)
        loop = self.engine.loop
        due = loop.time() + seconds

        def fire():
            nonlocal due
            if not t.active or not self.running:
                return
            self.run_wave(origin.again() if origin else Origin("timer", node.id), None, fn, node)
            now = loop.time()
            due += seconds
            if due <= now:  # сильно отстали — не догоняем пачкой
                due = now + seconds
            if t.active:
                t.handle = loop.call_at(due, fire)
        t.handle = loop.call_at(due, fire)
        self.timers.add(t)
        return t

    def at(self, node: Node, when: Callable[[], float | None], fn: Callable[[], None]) -> Timer:
        t = Timer(self)
        loop = self.engine.loop

        def arm():
            nxt = when()
            if nxt is None or not t.active:
                return
            t.handle = loop.call_later(max(0.0, nxt - time.time()), fire)

        def fire():
            if not t.active or not self.running:
                return
            self.run_wave(Origin("timer", node.id), None, fn, node)
            arm()
        arm()
        self.timers.add(t)
        return t

    # ---- контекст запроса
    def _ctx_release(self, ctx: RequestContext) -> None:
        ctx.pending -= 1
        if self.wave is None:
            self._ctx_check(ctx)

    def _ctx_check(self, ctx: RequestContext) -> None:
        if ctx.done or ctx.pending > 0:
            return
        ctx.done = True
        for cb in ctx.on_complete:
            try:
                cb(ctx)
            except Exception:
                log.exception("request on_complete")

    # ---- ошибки, лог, UI
    def _guard(self, node: Node | None, fn: Callable, *args) -> None:
        try:
            fn(*args)
        except Exception as e:
            if node is None:
                raise
            self.node_error(node, e)

    def node_error(self, node: Node, e: Exception | str) -> None:
        text = e if isinstance(e, str) else _err_text(e)
        now = time.time()
        err = self.errors.get(node.id)
        if err and err.message == text and now - err.logged < ERROR_LOG_EVERY_S:
            err.repeats += 1
            err.ts = now
        else:
            extra = f" (ещё {err.repeats} раз)" if err and err.repeats else ""
            self.errors[node.id] = _NodeError(text, now, now)
            self.log(f"{node.title}: {text}{extra}", node=node, level="error")
            if not isinstance(e, (str, NodeError, ResourceConflict, OSError, ValueError)):
                log.debug("node %s", node.id, exc_info=e)
        with self._live_lock:
            self._live_states.add(node.id)

    def node_state(self, nid: str) -> dict:
        err = self.errors.get(nid)
        if err and time.time() - err.ts < ERROR_HOLD_S:
            return {"state": "error", "error": err.message}
        return {"state": "running" if self.running else "stopped", "error": err.message if err else None}

    def set_view(self, node: Node, view: dict) -> None:
        self.views[node.id] = view
        with self._live_lock:
            self._live_views[node.id] = view

    def log(self, text: str, *, node: Node | None = None, level: str = "info") -> None:
        self.engine.on_log(self.project_id, {"t": time.time(), "level": level,
                                             "node": node.id if node else None, "text": text})

    def snapshot(self) -> dict:
        """Полное live-состояние проекта (для нового подписчика)."""
        with self._live_lock:
            values = {f"{n}.{s}": {"v": jsonable(x.value), "o": str(x.origin), "t": x.ts}
                      for (n, s), x in self.samples.items()}
            for k, aka in self.aliases.items():
                if k in values:
                    for a in aka:
                        values[a] = values[k]
            events = {k: {"n": c} for k, c in self._event_counts.items()}
        return {"values": values, "events": events, "views": dict(self.views),
                "nodes": {nid: self.node_state(nid) for nid in self.nodes}}

    def drain_live(self) -> dict | None:
        with self._live_lock:
            values, events, views = self._live_values, self._live_events, self._live_views
            states = self._live_states
            self._live_values, self._live_events, self._live_views, self._live_states = {}, {}, {}, set()
        # нода, у которой истёк «красный» период, тоже меняет состояние
        now = time.time()
        for nid, err in self.errors.items():
            if ERROR_HOLD_S <= now - err.ts < ERROR_HOLD_S + 1.0:
                states.add(nid)
        if not (values or events or views or states):
            return None
        return {"values": values, "events": events, "views": views,
                "nodes": {nid: self.node_state(nid) for nid in states if nid in self.nodes}}


def _err_text(e: BaseException) -> str:
    if isinstance(e, KeyError):
        return str(e.args[0]) if e.args else "нет такого ключа"
    if isinstance(e, OSError) and e.strerror:
        return f"{e.strerror} (errno {e.errno})"
    if isinstance(e, (NodeError, ResourceConflict, ValueError, StartError)):
        return str(e)
    return f"{type(e).__name__}: {e}" + ("" if str(e) else f" @ {traceback.format_exc(limit=-1).strip()}")


# ---------------------------------------------------------------- движок
class Engine:
    """Поток движка с asyncio-циклом; запущенные проекты — ProjectRuntime."""

    def __init__(self, hw: HW, registry: NodeRegistry):
        self.hw, self.registry = hw, registry
        self.loop = asyncio.new_event_loop()
        self.runtimes: dict[str, ProjectRuntime] = {}
        self.wave_hooks: list[Callable[[ProjectRuntime, Wave], None]] = []
        self.on_log: Callable[[str, dict], None] = lambda pid, line: None
        self.mqtt = None  # pioneer.mqtt.MqttBridge: ноды «Публикация», «Связь»
        self.remote = None  # pioneer.remote.Remote: публикация состояний (MQTT)
        self.secrets = None  # SecretStore: {{secret:имя}} в параметрах нод (spec 6)
        self._wave_ids = itertools.count(1)
        self._seq = itertools.count()
        self._thread = threading.Thread(target=self._run, name="pioneer-engine", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def close(self) -> None:
        if self.loop.is_closed():
            return
        self.call(self._stop_all)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(5)
        self.loop.close()

    # ---- вызовы в потоке движка
    @property
    def in_engine_thread(self) -> bool:
        return threading.current_thread() is self._thread

    def call(self, fn: Callable, *args, timeout: float = 10.0):
        """Выполнить fn в потоке движка и дождаться результата (исключения пробрасываются)."""
        if self.in_engine_thread:
            return fn(*args)
        fut: concurrent.futures.Future = concurrent.futures.Future()

        def run():
            try:
                fut.set_result(fn(*args))
            except BaseException as e:
                fut.set_exception(e)
        self.loop.call_soon_threadsafe(run)
        return fut.result(timeout)

    def sync(self) -> None:
        """Дождаться, пока обработаются все уже поставленные волны (тесты)."""
        self.call(lambda: None)

    # ---- внешние источники: единый путь (spec 3.4)
    def inject(self, project_id: str, origin: Origin, values: dict[tuple[str, str], Any] | None = None, *,
               events: dict[tuple[str, str], Any] | None = None, ctx: RequestContext | None = None) -> None:
        """Начать новую волну: выставить выходы нод-источников (values) и/или послать
        события (events) — {(нода, выход): значение/payload}. Потокобезопасно.
        ctx — контекст запроса: поедет с волной и её продолжениями."""
        def fn(rt: ProjectRuntime):
            for (nid, sock), v in (values or {}).items():
                rt.set_output(rt.nodes[nid], rt.types[nid].outputs[sock], v)
            for (nid, sock), p in (events or {}).items():
                rt.emit(rt.nodes[nid], rt.types[nid].outputs[sock], p)
        self._submit(project_id, origin, ctx, fn)

    def inject_call(self, project_id: str, origin: Origin, fn: Callable[[], None], *,
                    ctx: RequestContext | None = None, node: Node | None = None) -> None:
        """Выполнить fn ноды в новой волне (колбэки HAL, ридеры, UI). Потокобезопасно."""
        self._submit(project_id, origin, ctx, lambda rt: fn(), node)

    def _submit(self, project_id: str, origin: Origin, ctx: RequestContext | None,
                fn: Callable[[ProjectRuntime], None], node: Node | None = None) -> None:
        def run():
            rt = self.runtimes.get(project_id)
            if rt is None or not rt.running or (node is not None and rt.nodes.get(node.id) is not node):
                if ctx is not None and not ctx.done:
                    ctx.done = True  # проект остановлен — запрос не будет обработан
                return
            if ctx is not None and ctx.deadline is not None:
                self._arm_deadline(ctx)
            rt.run_wave(origin, ctx, lambda: fn(rt), node)
        if self.in_engine_thread:
            self.loop.call_soon(run)
        else:
            self.loop.call_soon_threadsafe(run)

    def _arm_deadline(self, ctx: RequestContext) -> None:
        if ctx.meta.get("_deadline_armed"):
            return
        ctx.meta["_deadline_armed"] = True

        def check():
            if not ctx.done:
                ctx.timed_out = True
                for cb in ctx.on_timeout:
                    try:
                        cb(ctx)
                    except Exception:
                        log.exception("request on_timeout")
        self.loop.call_later(max(0.0, ctx.deadline - time.monotonic()), check)

    def ui(self, project_id: str, node_id: str, value: Any) -> None:
        """Значение UI-контрола из браузера (origin=ui)."""
        def run():
            rt = self.runtimes.get(project_id)
            if rt is None or not rt.running:
                raise NodeError("проект не запущен")
            node = rt.nodes.get(node_id)
            if node is None:
                raise NodeError(f"нет ноды {node_id}")
            if type(node).ui is Node.ui:
                raise NodeError(f"{node.title} не принимает значения из UI")
            rt.run_wave(Origin("ui", node_id), None, lambda: node.ui(value), node)
        self.call(run)

    def on_wave_done(self, hook: Callable[[ProjectRuntime, Wave], None]) -> None:
        self.wave_hooks.append(hook)

    # ---- проекты
    def start_project(self, project_id: str, name: str, graph: Graph, rev: int = 0,
                      aliases: dict[str, list[str]] | None = None) -> ProjectRuntime:
        def run():
            if project_id in self.runtimes:
                raise StartError("проект уже запущен")
            try:
                rt = ProjectRuntime(self, project_id, name, graph, rev, aliases)
            except KeyError as e:
                raise StartError(str(e.args[0])) from None
            self._check_manifest(rt)
            rt.start()
            self.runtimes[project_id] = rt
            return rt
        return self.call(run)

    def _check_manifest(self, rt: ProjectRuntime) -> None:
        mine: dict[tuple[str, str], str] = {}
        for d in rt.manifest():
            key = (d.kind, d.name)
            if key in mine:
                raise StartError(f"{d.kind} «{d.name}» объявлен в проекте дважды")
            mine[key] = d.node
        for other in self.runtimes.values():
            for d in other.manifest():
                if (d.kind, d.name) in mine:
                    raise StartError(f"{d.kind} «{d.name}» уже объявлен проектом «{other.name}»")

    def stop_project(self, project_id: str) -> bool:
        def run():
            rt = self.runtimes.pop(project_id, None)
            if rt is None:
                return False
            rt.stop()
            return True
        return self.call(run)

    def _stop_all(self) -> None:
        for pid in list(self.runtimes):
            self.runtimes.pop(pid).stop()

    def stop_all(self) -> None:
        self.call(self._stop_all)
