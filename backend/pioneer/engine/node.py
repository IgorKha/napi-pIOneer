"""Описание типов нод и базовый класс встроенной ноды (spec 3.6).

    @node("builtin.hysteresis", title="Гистерезис", category="Логика", doc="…")
    class Hysteresis(Node):
        value = In("Number", 0, doc="…")
        low = In("Number", 30)
        high = In("Number", 35)
        invert = Param("bool", False, title="Инвертировать")
        out = Out("Bool")

        def on_change(self, changed):
            ...
            self.out.set(state != self.invert)

Входы читаются атрибутом (`self.value` — текущее значение, приведённое к типу входа),
выходы — `self.out.set(v)` / `self.out.emit(payload)`, параметры — `self.invert`.
Тот же стиль ляжет на Python-блоки (фаза 2).
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from pioneer.engine.types import DEFAULTS, SOCKET_COLORS, SOCKET_TYPES, EventValue, Origin, Sample

if TYPE_CHECKING:
    from pioneer.engine.runtime import ProjectRuntime, Timer

PARAM_KINDS = ("bool", "int", "float", "str", "choice", "pin", "interface", "led", "cron", "expression")
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_RESERVED = {"id", "params", "label"}  # атрибуты экземпляра Node


# ---------------------------------------------------------------- сокеты и параметры
@dataclass
class In:
    """Вход ноды. Неподключённый вход берёт inline-значение из графа или default."""
    type: str
    default: Any = None
    title: str | None = None
    doc: str = ""
    min: float | None = None
    max: float | None = None
    step: float | None = None
    unit: str | None = None
    name: str = ""

    def __post_init__(self):
        assert self.type in SOCKET_TYPES, self.type
        if self.default is None:
            self.default = DEFAULTS.get(self.type)

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, obj: Node | None, owner=None):
        if obj is None:
            return self
        return obj._rt.input_value(obj, self)

    def schema(self) -> dict:
        d = {"name": self.name, "type": self.type, "title": self.title or self.name, "doc": self.doc,
             "inline": self.type in ("Bool", "Number", "String")}
        if self.type not in ("Event", "Request", "Image", "Any"):
            d["default"] = self.default
        d.update({k: v for k in ("min", "max", "step", "unit") if (v := getattr(self, k)) is not None})
        return d


@dataclass
class Out:
    type: str
    title: str | None = None
    doc: str = ""
    name: str = ""

    def __post_init__(self):
        assert self.type in SOCKET_TYPES and self.type != "Any", self.type

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, obj: Node | None, owner=None):
        if obj is None:
            return self
        return OutPort(obj, self)

    def schema(self) -> dict:
        return {"name": self.name, "type": self.type, "title": self.title or self.name, "doc": self.doc}


class OutPort:
    """Выход конкретной ноды: set() для значений, emit() для событий."""
    __slots__ = ("node", "spec")

    def __init__(self, node: Node, spec: Out):
        self.node, self.spec = node, spec

    def set(self, value: Any) -> None:
        """Установить значение; изменилось — зависимые ноды пересчитаются в этой волне."""
        self.node._rt.set_output(self.node, self.spec, value)

    def emit(self, payload: Any = None) -> None:
        """Послать событие (только для Event-выходов). Контекст запроса волны едет с ним."""
        self.node._rt.emit(self.node, self.spec, payload)

    @property
    def value(self) -> Any:
        s = self.node._rt.sample(self.node.id, self.spec.name)
        return s.value if s else None


@dataclass
class Param:
    """Неподключаемая настройка ноды (дропдаун, чекбокс, пин)."""
    kind: str
    default: Any = None
    title: str | None = None
    doc: str = ""
    choices: list[tuple[str, str]] | None = None  # (значение, подпись)
    min: float | None = None
    max: float | None = None
    unit: str | None = None
    modes: tuple[str, ...] = ()  # для pin: допустимые рантайм-режимы; для interface — тип шины
    required: bool = False
    name: str = ""

    def __post_init__(self):
        assert self.kind in PARAM_KINDS, self.kind

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, obj: Node | None, owner=None):
        if obj is None:
            return self
        return obj.params.get(self.name, self.default)

    def schema(self) -> dict:
        d = {"name": self.name, "kind": self.kind, "title": self.title or self.name, "doc": self.doc,
             "default": self.default, "required": self.required}
        if self.choices:
            d["choices"] = [{"value": v, "title": t} for v, t in self.choices]
        if self.modes:
            d["modes"] = list(self.modes)
        d.update({k: v for k in ("min", "max", "unit") if (v := getattr(self, k)) is not None})
        return d


# ---------------------------------------------------------------- тип ноды
@dataclass
class Declaration:
    """Объявление в манифесте проекта (команда / состояние / событие, idea 003 п.4)."""
    kind: str  # command | state | event
    name: str
    schema: dict = field(default_factory=dict)
    node: str = ""


@dataclass
class NodeType:
    id: str
    version: int
    title: str
    category: str
    doc: str
    cls: type[Node]
    color: str | None = None
    inputs: dict[str, In] = field(default_factory=dict)
    outputs: dict[str, Out] = field(default_factory=dict)
    params: dict[str, Param] = field(default_factory=dict)
    breaks_cycle: bool = False  # Delay/Latch/Feedback: через неё можно замкнуть цикл
    hidden: bool = False

    @property
    def key(self) -> str:
        return f"{self.id}@{self.version}"

    def schema(self) -> dict:
        return {
            "type": self.key, "id": self.id, "version": self.version, "title": self.title,
            "category": self.category, "color": self.color, "doc": self.doc,
            "breaks_cycle": self.breaks_cycle,
            "inputs": [i.schema() for i in self.inputs.values()],
            "outputs": [o.schema() for o in self.outputs.values()],
            "params": [p.schema() for p in self.params.values()],
        }


class NodeRegistry:
    """Реестр типов нод с неймспейсами: builtin.*, user.* (фаза 2), mod.<module>.* (idea 001)."""

    def __init__(self):
        self.types: dict[str, NodeType] = {}

    def add(self, nt: NodeType) -> None:
        if nt.key in self.types:
            raise ValueError(f"тип ноды {nt.key} уже зарегистрирован")
        self.types[nt.key] = nt

    def get(self, key: str) -> NodeType:
        try:
            return self.types[key]
        except KeyError:
            raise KeyError(f"нет типа ноды {key!r}") from None

    def resolve(self, ref: str) -> NodeType:
        """`builtin.and@1` или `builtin.and` (последняя версия)."""
        if "@" in ref:
            return self.get(ref)
        versions = [t for t in self.types.values() if t.id == ref]
        if not versions:
            raise KeyError(f"нет типа ноды {ref!r}")
        return max(versions, key=lambda t: t.version)

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self.types.values() if not t.hidden]


BUILTIN = NodeRegistry()


def node(id: str, *, title: str, category: str, doc: str, version: int = 1, color: str | None = None,
         breaks_cycle: bool = False, registry: NodeRegistry | None = None, hidden: bool = False):
    """Декоратор: зарегистрировать класс как тип ноды."""
    def deco(cls: type[Node]) -> type[Node]:
        nt = NodeType(id=id, version=version, title=title, category=category, doc=doc.strip(), cls=cls,
                      color=color, breaks_cycle=breaks_cycle, hidden=hidden)
        for klass in reversed(cls.__mro__):
            for name, attr in vars(klass).items():
                if isinstance(attr, (In, Out, Param)):
                    if not _NAME.match(name) or hasattr(Node, name) or name in _RESERVED:
                        raise ValueError(f"{id}: недопустимое имя сокета/параметра {name!r}")
                    {In: nt.inputs, Out: nt.outputs, Param: nt.params}[type(attr)][name] = attr
        cls.TYPE = nt
        (registry if registry is not None else BUILTIN).add(nt)
        return cls
    return deco


# ---------------------------------------------------------------- базовый класс
class NodeError(Exception):
    """Ошибка ноды с понятным текстом (без трейсбека в логе)."""


class Node:
    TYPE: ClassVar[NodeType]

    def __init__(self, rt: ProjectRuntime, node_id: str, params: dict, label: str | None):
        self._rt = rt
        self.id = node_id
        self.params = params
        self.label = label

    @property
    def title(self) -> str:
        return self.label or self.TYPE.title

    # ---- жизненный цикл (переопределяются)
    def resources(self) -> dict[str, str]:
        """Ресурсы железа {id: режим}, захватываемые при старте проекта (spec 3.5)."""
        return {}

    def declarations(self) -> list[Declaration]:
        """Объявления для манифеста проекта (команды/состояния/события — фаза 2)."""
        return []

    def setup(self) -> None:
        """Открыть железо (ресурсы уже захвачены). Ошибка — проект не стартует."""

    def start(self) -> None:
        """Начальные значения: вызывается в стартовой волне (origin=start)."""

    def on_change(self, changed: set[str]) -> None:
        """Изменились значения входов (имена в changed)."""

    def on_event(self, name: str, ev: EventValue) -> None:
        """Пришло событие на Event-вход name."""

    def stop(self) -> None:
        """Проект останавливается: таймеры уже отменены; освободить своё."""

    # ---- помощники
    @property
    def hw(self):
        return self._rt.hw

    @property
    def wave(self):
        """Текущая волна: origin, ctx (контекст запроса), id."""
        return self._rt.wave

    def sample(self, input_name: str) -> Sample | None:
        """Значение на входе с источником и временем (None — вход не подключён)."""
        return self._rt.input_sample(self, input_name)

    def connected(self, input_name: str) -> bool:
        return self._rt.is_connected(self.id, input_name)

    def later(self, seconds: float, fn: Callable[[], None]) -> Timer:
        """Вызвать fn через seconds в новой волне — с тем же origin и контекстом запроса."""
        return self._rt.later(self, seconds, fn)

    def every(self, seconds: float, fn: Callable[[], None], *, origin: Origin | None = None) -> Timer:
        """Вызывать fn периодически (без дрейфа), каждый раз — новая волна origin=timer."""
        return self._rt.every(self, seconds, fn, origin)

    def at(self, when: Callable[[], float | None], fn: Callable[[], None]) -> Timer:
        """Вызывать fn в моменты времени (time.time()), которые возвращает when() (Schedule)."""
        return self._rt.at(self, when, fn)

    def from_thread(self, origin: Origin, fn: Callable[[], None]) -> None:
        """Потокобезопасно: выполнить fn в новой волне (колбэки HAL, ридеры портов)."""
        self._rt.engine.inject_call(self._rt.project_id, origin, fn, node=self)

    def show(self, view: dict) -> None:
        """Состояние для UI (Watch, контролы): уходит по WS как node_view."""
        self._rt.set_view(self, view)

    def log(self, *parts: Any) -> None:
        self._rt.log(" ".join(str(p) for p in parts), node=self)

    def ui(self, value: Any) -> None:
        """Значение от UI-контрола (только для нод-контролов): вызывается в волне origin=ui."""
        raise NodeError("эта нода не принимает значения из UI")

    @staticmethod
    def now() -> float:
        return time.time()
