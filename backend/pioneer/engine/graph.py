"""Граф проекта и его проверка (spec 3.2, 3.3, 3.5, 3.7a).

Граф = {nodes[], edges[], groups[]}; нода = {id, type: "builtin.and@1", position, params,
inputs (inline-значения неподключённых входов), collapsed, label}; ребро = {from: [node,
socket], to: [node, socket]}. Привязка к пину — параметр `pin` hardware-ноды.

validate() возвращает ошибки (граф нельзя сохранить/запустить) и предупреждения (зависят
от состояния платы и других проектов — например, пин сейчас отдан интерфейсу).
"""
from __future__ import annotations

import copy
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from pioneer.board import Board, InterfaceError
from pioneer.engine.cron import CronError, parse_cron
from pioneer.engine.expr import ExprError, compile_expr
from pioneer.engine.node import NodeRegistry, NodeType, Param
from pioneer.engine.types import can_connect, check_value
from pioneer.resources import i2c_id, spi_id, uart_id

EXCLUSIVE = {"gpio_out", "soft_pwm"}


@dataclass
class GNode:
    id: str
    type: str
    position: list[float] = field(default_factory=lambda: [0.0, 0.0])
    params: dict[str, Any] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)
    collapsed: bool = False
    label: str | None = None

    def to_json(self) -> dict:
        d = {"id": self.id, "type": self.type, "position": self.position, "params": self.params,
             "inputs": self.inputs}
        if self.collapsed:
            d["collapsed"] = True
        if self.label:
            d["label"] = self.label
        return d


@dataclass(frozen=True)
class GEdge:
    src: tuple[str, str]
    dst: tuple[str, str]

    def to_json(self) -> dict:
        return {"from": list(self.src), "to": list(self.dst)}


@dataclass
class Graph:
    nodes: dict[str, GNode] = field(default_factory=dict)
    edges: list[GEdge] = field(default_factory=list)
    groups: list[dict] = field(default_factory=list)
    # состояние редактора, не влияющее на исполнение: положение Board-ноды ("@board") и т.п.
    view: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, raw: dict | None) -> Graph:
        raw = raw or {}
        g = cls()
        for n in raw.get("nodes", []):
            g.nodes[n["id"]] = GNode(id=n["id"], type=n["type"], position=list(n.get("position", [0, 0])),
                                     params=dict(n.get("params", {})), inputs=dict(n.get("inputs", {})),
                                     collapsed=bool(n.get("collapsed", False)), label=n.get("label"))
        g.edges = [GEdge(tuple(e["from"]), tuple(e["to"])) for e in raw.get("edges", [])]
        g.groups = list(raw.get("groups", []))
        g.view = dict(raw.get("view", {}))
        return g

    def to_json(self) -> dict:
        return {"nodes": [n.to_json() for n in self.nodes.values()],
                "edges": [e.to_json() for e in self.edges], "groups": self.groups, "view": self.view}

    def copy(self) -> Graph:
        return copy.deepcopy(self)

    def edge_to(self, node: str, socket: str) -> GEdge | None:
        return next((e for e in self.edges if e.dst == (node, socket)), None)


@dataclass
class Issue:
    message: str
    node: str | None = None
    socket: str | None = None
    param: str | None = None

    def to_json(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Report:
    """errors — граф некорректен (пакет операций отклоняется); incomplete — граф можно
    сохранить, но не запустить (не выбран пин, пин занят двумя нодами); warnings — зависят
    от состояния платы и других проектов."""
    errors: list[Issue] = field(default_factory=list)
    incomplete: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def can_start(self) -> bool:
        return not self.errors and not self.incomplete

    def to_json(self) -> dict:
        return {"ok": self.ok, "can_start": self.can_start,
                "errors": [i.to_json() for i in self.errors],
                "incomplete": [i.to_json() for i in self.incomplete],
                "warnings": [i.to_json() for i in self.warnings]}

    def text(self) -> str:
        return "; ".join(f"{i.node + ': ' if i.node else ''}{i.message}" for i in self.errors + self.incomplete)


# ---------------------------------------------------------------- параметры и ресурсы
def static_modes(board: Board, pin_id: str) -> set[str]:
    """Режимы, которые пин поддерживает в принципе (без учёта текущих оверлеев)."""
    pin = board.pin(pin_id)
    if pin.gpio:
        return {"gpio_in", "gpio_out", "soft_pwm"}
    if pin.adc_channel is not None:
        return {"adc"}
    return set()


def check_param(p: Param, value: Any, board: Board | None) -> Any:
    """Проверить и нормализовать значение параметра.

    Raises:
        ValueError: с понятным текстом.
    """
    if value is None:  # «не задан»; обязательность проверяет validate() (incomplete)
        return None
    match p.kind:
        case "bool":
            if not isinstance(value, bool):
                raise ValueError("ожидается true/false")
        case "int" | "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("ожидается число")
            if p.kind == "int":
                if value != int(value):
                    raise ValueError("ожидается целое")
                value = int(value)
            else:
                value = float(value)
            if p.min is not None and value < p.min or p.max is not None and value > p.max:
                raise ValueError(f"допустимо {p.min if p.min is not None else '…'}…"
                                 f"{p.max if p.max is not None else '…'}")
        case "str" | "expression" | "cron" | "pin" | "interface" | "led" | "choice" | "code":
            if not isinstance(value, str):
                raise ValueError("ожидается строка")
    match p.kind:
        case "choice":
            if value not in [v for v, _ in p.choices or ()]:
                raise ValueError(f"одно из: {', '.join(v for v, _ in p.choices or ())}")
        case "expression":
            try:
                compile_expr(value)
            except ExprError as e:
                raise ValueError(str(e)) from None
        case "cron":
            try:
                parse_cron(value)
            except CronError as e:
                raise ValueError(str(e)) from None
        case "pin" if board is not None:
            if value not in board.pins:
                raise ValueError(f"нет пина {value!r} на плате")
            mode = p.modes[0]
            if mode not in static_modes(board, value):
                pin = board.pin(value)
                raise ValueError(f"{value} ({pin.name or pin.kind}) не поддерживает режим {mode}")
        case "interface" if board is not None:
            kind = p.modes[0]
            names = {i.controller for i in board.interfaces.values() if i.kind == kind} | \
                    {i.id for i in board.interfaces.values() if i.kind == kind}
            if value not in names:
                raise ValueError(f"нет интерфейса {value!r}; есть: "
                                 f"{', '.join(sorted({i.controller for i in board.interfaces.values() if i.kind == kind}))}")
        case "led" if board is not None:
            if value not in board.leds:
                raise ValueError(f"нет встроенного LED {value!r}")
    return value


def param_resources(nt: NodeType, params: dict, board: Board) -> dict[str, str]:
    """Ресурсы, которые нода захватит при старте, — по её параметрам pin/interface/led."""
    out: dict[str, str] = {}
    for p in nt.params.values():
        v = params.get(p.name, p.default)
        if not v:
            continue
        if p.kind == "pin":
            out[v] = p.modes[0]
        elif p.kind == "led":
            out[f"led:{v}"] = "led"
        elif p.kind == "interface":
            kind = p.modes[0]
            ctl = board.interfaces[v].controller if v in board.interfaces else v
            if kind == "uart":
                out[uart_id(ctl)] = "uart"
            elif kind == "i2c" and "addr" in params:
                out[i2c_id(ctl, int(params["addr"]))] = "i2c"
            elif kind == "spi":
                out[spi_id(ctl, int(params.get("cs", 0)))] = "spi"
    return out


# ---------------------------------------------------------------- проверка графа
def validate(graph: Graph, registry: NodeRegistry, board: Board | None = None, *,
             foreign_claims: dict[str, tuple[str, str]] | None = None) -> Report:
    """Проверить граф целиком.

    Args:
        foreign_claims: занятые другими владельцами ресурсы {id: (режим, имя владельца)} —
            для предупреждений о конфликтах с запущенными проектами/скриптами.
    """
    rep = Report()
    types: dict[str, NodeType] = {}
    for n in graph.nodes.values():
        try:
            nt = types[n.id] = registry.for_node(n)
        except KeyError as e:
            rep.errors.append(Issue(str(e.args[0]), node=n.id))
            continue
        if nt.error:
            rep.incomplete.append(Issue(nt.error, node=n.id, param="code"))
        for name, value in n.params.items():
            p = nt.params.get(name)
            if p is None:
                rep.errors.append(Issue(f"нет параметра {name!r}", node=n.id, param=name))
                continue
            try:
                check_param(p, value, board)
            except ValueError as e:
                rep.errors.append(Issue(f"{p.title or name}: {e}", node=n.id, param=name))
        for p in nt.params.values():
            if p.required and n.params.get(p.name) in (None, ""):
                rep.incomplete.append(Issue(f"не задан параметр «{p.title or p.name}»", node=n.id, param=p.name))
        for name, value in n.inputs.items():
            i = nt.inputs.get(name)
            if i is None:
                rep.errors.append(Issue(f"нет входа {name!r}", node=n.id, socket=name))
                continue
            try:
                check_value(value, i.type)
            except ValueError as e:
                rep.errors.append(Issue(f"{i.title or name}: {e}", node=n.id, socket=name))

    seen_inputs: set[tuple[str, str]] = set()
    for e in graph.edges:
        a, b = types.get(e.src[0]), types.get(e.dst[0])
        if e.src[0] not in graph.nodes or e.dst[0] not in graph.nodes:
            rep.errors.append(Issue(f"провод к несуществующей ноде {e.src} → {e.dst}"))
            continue
        if a is None or b is None:
            continue
        out, inp = a.outputs.get(e.src[1]), b.inputs.get(e.dst[1])
        if out is None:
            rep.errors.append(Issue(f"нет выхода {e.src[1]!r}", node=e.src[0], socket=e.src[1]))
            continue
        if inp is None:
            rep.errors.append(Issue(f"нет входа {e.dst[1]!r}", node=e.dst[0], socket=e.dst[1]))
            continue
        if not can_connect(out.type, inp.type):
            rep.errors.append(Issue(f"нельзя соединить {out.type} → {inp.type}", node=e.dst[0], socket=e.dst[1]))
        if e.dst in seen_inputs:
            rep.errors.append(Issue("к входу подключено больше одного провода", node=e.dst[0], socket=e.dst[1]))
        seen_inputs.add(e.dst)
        if e.src[0] == e.dst[0] and not b.breaks_cycle:
            rep.errors.append(Issue("нода соединена сама с собой", node=e.dst[0]))

    cycle = find_cycle(graph, types)
    if cycle:
        rep.errors.append(Issue("цикл без ноды-разрыва (Delay, Latch, Feedback): " + " → ".join(cycle),
                                node=cycle[0]))

    if board is not None:
        _check_resources(graph, types, board, rep, foreign_claims or {})
    return rep


def _check_resources(graph: Graph, types: dict[str, NodeType], board: Board, rep: Report,
                     foreign: dict[str, tuple[str, str]]) -> None:
    users: dict[str, list[tuple[str, str]]] = defaultdict(list)  # ресурс -> [(нода, режим)]
    for n in graph.nodes.values():
        nt = types.get(n.id)
        if nt is None:
            continue
        try:
            res = param_resources(nt, n.params, board)
        except (KeyError, ValueError):
            continue  # ошибка параметра уже записана
        for rid, mode in res.items():
            users[rid].append((n.id, mode))
    for rid, us in users.items():
        modes = {m for _, m in us}
        if rid in board.pins and (len(modes) > 1 and modes & EXCLUSIVE or
                                  len(us) > 1 and modes <= EXCLUSIVE):
            rep.incomplete.append(Issue(f"{rid} используется несколькими нодами как выход/вход одновременно",
                                    node=us[1][0], param="pin"))
            continue
        node = us[0][0]
        if rid in board.pins:
            mode = us[0][1]
            if mode not in board.runtime_modes(rid):
                func = board.function(rid)
                why = (f"отдан интерфейсу {board.interfaces[func].title}" if func in board.interfaces
                       else "занят драйвером ядра" if func == "kernel" else f"сейчас недоступен как {mode}")
                rep.warnings.append(Issue(f"{rid} {why} — проект не запустится", node=node, param="pin"))
        else:
            ctl = rid.split("@")[0].split(".")[0]
            if not rid.startswith("led:"):
                try:
                    board.bus_interface(ctl)
                except InterfaceError as e:
                    rep.warnings.append(Issue(f"{e} — проект не запустится", node=node, param="interface"))
        if rid in foreign:
            fmode, owner = foreign[rid]
            if not (fmode == us[0][1] and fmode in ("gpio_in", "adc")):
                rep.warnings.append(Issue(f"{rid} сейчас занят: {owner}", node=node))


def find_cycle(graph: Graph, types: dict[str, NodeType]) -> list[str] | None:
    """Цикл по проводам, не проходящий через вход ноды-разрыва; None — циклов нет."""
    adj: dict[str, list[str]] = defaultdict(list)
    for e in graph.edges:
        nt = types.get(e.dst[0])
        if nt is not None and not nt.breaks_cycle and e.src[0] in graph.nodes:
            adj[e.src[0]].append(e.dst[0])
    color: dict[str, int] = {}
    stack: list[str] = []

    def dfs(v: str) -> list[str] | None:
        color[v] = 1
        stack.append(v)
        for w in adj[v]:
            if color.get(w) == 1:
                return stack[stack.index(w):] + [w]
            if w not in color and (c := dfs(w)):
                return c
        stack.pop()
        color[v] = 2
        return None

    for v in graph.nodes:
        if v not in color and (c := dfs(v)):
            return c
    return None


def ranks(graph: Graph, types: dict[str, NodeType]) -> dict[str, int]:
    """Топологический ранг ноды (по проводам, кроме входов нод-разрывов)."""
    indeg = {n: 0 for n in graph.nodes}
    adj: dict[str, list[str]] = defaultdict(list)
    for e in graph.edges:
        nt = types.get(e.dst[0])
        if nt is not None and not nt.breaks_cycle and e.src[0] in indeg and e.dst[0] in indeg:
            adj[e.src[0]].append(e.dst[0])
            indeg[e.dst[0]] += 1
    rank: dict[str, int] = {}
    q = deque(n for n, d in indeg.items() if d == 0)
    while q:
        v = q.popleft()
        rank.setdefault(v, 0)
        for w in adj[v]:
            rank[w] = max(rank.get(w, 0), rank[v] + 1)
            indeg[w] -= 1
            if indeg[w] == 0:
                q.append(w)
    for n in graph.nodes:  # на случай цикла (граф не прошёл бы validate)
        rank.setdefault(n, len(graph.nodes))
    return rank
