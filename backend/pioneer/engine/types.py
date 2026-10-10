"""Типы сокетов, значения на проводах и контекст волны (spec 3.3, 3.4, idea 003).

Значение на выходе хранится как Sample: само значение + кто начал волну, в которой оно
изменилось (Origin), и когда. Event не хранится — это импульс EventValue с данными
(payload) и, опционально, контекстом запроса (RequestContext).
"""
from __future__ import annotations

import itertools
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

SocketType = Literal["Bool", "Number", "Event", "String", "Json", "Image", "Request", "Any"]
SOCKET_TYPES: tuple[str, ...] = ("Bool", "Number", "Event", "String", "Json", "Image", "Request", "Any")

# Цвета в духе Blender (spec 3.3); фронт берёт их из /node-types.
SOCKET_COLORS = {
    "Bool": "#c6d84a", "Number": "#a1a1a1", "Event": "#e5c13a", "String": "#6fb6e0",
    "Json": "#e07ab4", "Image": "#9d7ae0", "Request": "#f2f2f2", "Any": "#6e6e6e",
}

DEFAULTS: dict[str, Any] = {"Bool": False, "Number": 0.0, "String": "", "Json": None,
                            "Image": None, "Request": None, "Any": None}

# Что можно подключить к входу данного типа (кроме совпадения типов).
_IMPLICIT = {
    "Number": {"Bool"},
    "Bool": {"Number"},
    "String": {"Number", "Bool"},
    "Json": {"Bool", "Number", "String"},
    "Any": {"Bool", "Number", "String", "Json", "Image"},
}


def can_connect(src: str, dst: str) -> bool:
    """Можно ли провести провод с выхода типа src на вход типа dst (spec 3.3).

    Event — только в Event (Bool↔Event — явными нодами Edge/Pulse), Request — только в Request.
    """
    return src == dst and src != "Any" or src in _IMPLICIT.get(dst, ())


def format_number(v: float) -> str:
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    return f"{v:.6g}"


def coerce(value: Any, dst: str) -> Any:
    """Неявное приведение значения к типу входа: Bool→Number (0/1), Number→Bool (≠0),
    Number/Bool→String, остальное в Json/Any — как есть."""
    if value is None:
        return DEFAULTS.get(dst)
    match dst:
        case "Bool":
            return bool(value)
        case "Number":
            if isinstance(value, bool):
                return 1.0 if value else 0.0
            v = float(value)
            return v if math.isfinite(v) else 0.0
        case "String":
            if isinstance(value, bool):
                return "true" if value else "false"
            if isinstance(value, (int, float)):
                return format_number(float(value))
            return str(value)
    return value


def check_value(value: Any, typ: str) -> Any:
    """Проверить inline-значение входа из графа (set_input_value) и привести к типу.

    Raises:
        ValueError: значение не подходит к типу сокета.
    """
    match typ:
        case "Bool":
            if not isinstance(value, bool):
                raise ValueError("ожидается true/false")
            return value
        case "Number":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("ожидается число")
            return float(value)
        case "String":
            if not isinstance(value, str):
                raise ValueError("ожидается строка")
            return value
        case "Json" | "Any":
            return value
    raise ValueError(f"у входа типа {typ} нет inline-значения")


# ---------------------------------------------------------------- волна
@dataclass(frozen=True)
class Origin:
    """Кто начал волну: gpio:<pin>, timer, ui, start, remote:<transport>, system."""
    kind: str
    detail: str | None = None
    ts: float = field(default_factory=time.time)

    def __str__(self) -> str:
        return f"{self.kind}:{self.detail}" if self.detail else self.kind

    def again(self) -> Origin:
        """Та же причина, новое время — для продолжений волны (Delay, Pulse)."""
        return Origin(self.kind, self.detail)


_req_ids = itertools.count(1)


@dataclass(eq=False)
class RequestContext:
    """Контекст запроса (команда из транспорта, фаза 2). Едет с волной неявно.

    Движок считает отложенные продолжения (pending); когда волна закончилась и продолжений
    нет — запрос «завершён»: вызываются on_complete(ctx). Если дедлайн наступил раньше —
    on_timeout(ctx). `replied` выставляет нода «Ответ» (фаза 2) — без неё транспорт может
    подтвердить команду автоматически по завершению.
    """
    transport: str = "local"
    id: str = field(default_factory=lambda: f"r{next(_req_ids)}")
    deadline: float | None = None  # time.monotonic()
    meta: dict[str, Any] = field(default_factory=dict)
    on_complete: list[Callable[[RequestContext], None]] = field(default_factory=list)
    on_timeout: list[Callable[[RequestContext], None]] = field(default_factory=list)
    replied: bool = False
    done: bool = False
    timed_out: bool = False
    pending: int = 0


@dataclass(frozen=True)
class EventValue:
    """Импульс на Event-сокете: время, данные (payload) и контекст запроса."""
    ts: float
    payload: Any = None
    ctx: RequestContext | None = None
    origin: Origin | None = None


@dataclass(frozen=True)
class Sample:
    """Последнее значение выхода: кто и когда его установил (для «Арбитра» и отладки)."""
    value: Any
    origin: Origin
    ts: float


def jsonable(value: Any, limit: int = 2000) -> Any:
    """Значение для WebSocket/API: Image не передаётся, большие Json — урезаются."""
    if isinstance(value, float):
        return round(value, 6) if math.isfinite(value) else None
    if isinstance(value, (bool, int, str)) or value is None:
        return value[:limit] if isinstance(value, str) else value
    if isinstance(value, (bytes, bytearray)):
        return {"image": len(value)}
    try:
        import json
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)[:limit]
    return value if len(text) <= limit else text[:limit] + "…"
