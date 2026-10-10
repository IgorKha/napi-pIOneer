"""SDK Python-блоков: свои ноды графа (spec 3.6 b). Код исполняется в worker-процессе
проекта (pioneer.blockhost), не в core: падение или зависание блока не роняет сервис.

    from pioneer.block import node, Node, In, Out, Param, Number, Bool

    @node(id="hysteresis", title="Гистерезис", category="Мои блоки")
    class Hysteresis(Node):
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

Входы читаются атрибутом (`self.value`), выходы — `self.out.set(v)` / `self.out.emit(payload)`
(для Event), параметры — `self.invert`. Таймеры — `self.every(сек, fn)` / `self.later(сек, fn)`,
лог — `self.log(...)`. id блока получает префикс `user.`; версия — `version=` (по умолчанию 1).

Обработчик должен уложиться в 1 с, иначе нода уходит в ошибку, а процесс блоков проекта
перезапускается при «Применить».
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = ["node", "Node", "In", "Out", "Param", "Number", "Bool", "Event", "String", "Json", "Any_"]

Number, Bool, Event, String, Json, Any_ = "Number", "Bool", "Event", "String", "Json", "Any"
_TYPES: dict = {**{t: t for t in (Number, Bool, Event, String, Json, Any_)},
                int: Number, float: Number, bool: Bool, str: String, dict: Json, list: Json}
_PARAM_KINDS = {bool: "bool", int: "int", float: "float", str: "str"}

# Ноды, объявленные при импорте файла блока: {id: класс}
REGISTRY: dict[str, type[Node]] = {}


def _type(t) -> str:
    try:
        return _TYPES[t]
    except (KeyError, TypeError):
        pass
    raise TypeError(f"тип сокета {t!r}: используйте Number, Bool, Event, String, Json или Any_")


@dataclass
class In:
    """Вход блока. Неподключённый — inline-значение на ноде или default."""
    type: Any
    default: Any = None
    title: str | None = None
    doc: str = ""
    min: float | None = None
    max: float | None = None
    unit: str | None = None
    name: str = ""

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, obj, owner=None):
        return self if obj is None else obj._inputs.get(self.name, self.default)

    def schema(self) -> dict:
        t = _type(self.type)
        return {"name": self.name, "type": t, "title": self.title, "doc": self.doc, "default": self.default,
                "min": self.min, "max": self.max, "unit": self.unit}


@dataclass
class Out:
    """Выход блока."""
    type: Any
    title: str | None = None
    doc: str = ""
    name: str = ""

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, obj, owner=None):
        return self if obj is None else _Port(obj, self.name)

    def schema(self) -> dict:
        t = _type(self.type)
        if t == Any_:
            raise TypeError(f"выход {self.name}: тип Any_ — только для входов")
        return {"name": self.name, "type": t, "title": self.title, "doc": self.doc}


@dataclass
class Param:
    """Неподключаемая настройка: Param(bool, False), Param(int, 5, min=0), Param(["a", "b"], "a")."""
    type: Any
    default: Any = None
    title: str | None = None
    doc: str = ""
    min: float | None = None
    max: float | None = None
    unit: str | None = None
    name: str = ""

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, obj, owner=None):
        return self if obj is None else obj._params.get(self.name, self.default)

    def schema(self) -> dict:
        if isinstance(self.type, (list, tuple)):
            choices = [c if isinstance(c, (list, tuple)) else (c, c) for c in self.type]
            return {"name": self.name, "kind": "choice", "default": self.default, "title": self.title,
                    "doc": self.doc, "choices": [[str(v), str(t)] for v, t in choices]}
        kind = _PARAM_KINDS.get(self.type)
        if kind is None:
            raise TypeError(f"параметр {self.name}: тип bool, int, float, str или список вариантов")
        return {"name": self.name, "kind": kind, "default": self.default, "title": self.title, "doc": self.doc,
                "min": self.min, "max": self.max, "unit": self.unit}


class _Port:
    __slots__ = ("node", "name")

    def __init__(self, node: Node, name: str):
        self.node, self.name = node, name

    def set(self, value: Any) -> None:
        self.node._actions.append(["set", self.name, value])

    def emit(self, payload: Any = None) -> None:
        self.node._actions.append(["emit", self.name, payload])


class Node:
    """Базовый класс блока. Переопределяйте setup / start / on_change / on_event / stop."""
    BLOCK: dict

    def __init__(self, node_id: str, params: dict):
        self.id = node_id
        self._params = params
        self._inputs: dict[str, Any] = {}
        self._actions: list = []
        self._host = None  # BlockHost: таймеры

    def setup(self) -> None:
        """Подготовка (вызывается при старте проекта)."""

    def start(self) -> None:
        """Начальные значения выходов (стартовая волна)."""

    def on_change(self, changed: set[str]) -> None:
        """Изменились входы (их имена в changed)."""

    def on_event(self, name: str, payload: Any) -> None:
        """Событие на Event-вход name; payload — данные события."""

    def stop(self) -> None:
        """Проект останавливается."""

    def log(self, *parts: Any) -> None:
        """Строка в лог проекта."""
        self._actions.append(["log", " ".join(str(p) for p in parts)])

    def later(self, seconds: float, fn: Callable[[], None]) -> None:
        """Однократно вызвать fn через seconds — выходы, выставленные в fn, уйдут новой волной."""
        self._host.timer(self, seconds, fn, None)

    def every(self, seconds: float, fn: Callable[[], None]) -> None:
        """Вызывать fn каждые seconds секунд."""
        if seconds <= 0:
            raise ValueError("период должен быть > 0")
        self._host.timer(self, seconds, fn, seconds)


def node(*, id: str, title: str, category: str = "Мои блоки", doc: str | None = None, color: str | None = None,
         version: int = 1, breaks_cycle: bool = False):
    """Декоратор: объявить класс блоком (нодой палитры)."""
    def deco(cls: type[Node]) -> type[Node]:
        if not issubclass(cls, Node):
            raise TypeError(f"{cls.__name__}: блок должен наследоваться от Node")
        attrs = {}
        for klass in reversed(cls.__mro__):
            attrs.update({k: v for k, v in vars(klass).items() if isinstance(v, (In, Out, Param))})
        cls.BLOCK = {
            "id": id, "title": title, "category": category, "doc": (doc or cls.__doc__ or "").strip(),
            "color": color, "version": int(version), "breaks_cycle": breaks_cycle,
            "inputs": [a.schema() for a in attrs.values() if isinstance(a, In)],
            "outputs": [a.schema() for a in attrs.values() if isinstance(a, Out)],
            "params": [a.schema() for a in attrs.values() if isinstance(a, Param)],
        }
        REGISTRY[id] = cls
        return cls
    return deco
