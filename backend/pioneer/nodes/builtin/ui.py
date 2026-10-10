"""Пульт (UI-контролы — источники из браузера) и отладка (Watch, Log)."""
from __future__ import annotations

from typing import Any

from pioneer.engine.node import In, Node, NodeError, Out, Param, node
from pioneer.engine.types import format_number, jsonable

UI, UI_COLOR = "Пульт", "#6b4f9a"
DBG, DBG_COLOR = "Отладка", "#5a5a5a"


def _num(v: Any) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise NodeError("ожидается число")
    return float(v)


@node("builtin.ui_button", title="Кнопка (пульт)", category=UI, color=UI_COLOR, doc="""
Кнопка на ноде в редакторе: «Нажата» true, пока кнопку держат мышью, и событие «Клик»
при отпускании. Для проверки сценария без железа.
""")
class UiButton(Node):
    pressed = Out("Bool", title="Нажата")
    click = Out("Event", title="Клик")

    def start(self):
        self.pressed.set(False)
        self.show({"pressed": False})

    def ui(self, value):
        if value == "click":
            self.click.emit()
            return
        p = bool(value.get("pressed") if isinstance(value, dict) else value)
        was = self.pressed.value
        self.pressed.set(p)
        self.show({"pressed": p})
        if was and not p:
            self.click.emit()


@node("builtin.ui_switch", title="Выключатель (пульт)", category=UI, color=UI_COLOR, doc="""
Переключатель на ноде в редакторе: включить/выключить что-то с экрана или проверить
сценарий без железа. После перезапуска проекта — «Начальное».
""")
class UiSwitch(Node):
    initial = Param("bool", False, title="Начальное")
    value = Out("Bool", title="Значение")

    def start(self):
        self._set(self.initial)

    def ui(self, value):
        self._set(bool(value))

    def _set(self, v: bool):
        self.value.set(v)
        self.show({"value": v})


@node("builtin.ui_slider", title="Слайдер (пульт)", category=UI, color=UI_COLOR, doc="""
Ползунок на ноде в редакторе: число в диапазоне [Мин, Макс] с шагом.

Пример: Слайдер 0…1 → ШИМ — яркость LED с экрана.
""")
class UiSlider(Node):
    min = Param("float", 0.0, title="Мин")
    max = Param("float", 1.0, title="Макс")
    step = Param("float", 0.01, title="Шаг", min=0)
    initial = Param("float", 0.0, title="Начальное")
    value = Out("Number", title="Значение")

    def start(self):
        self._set(self.initial)

    def ui(self, value):
        self._set(_num(value))

    def _set(self, v: float):
        lo, hi = min(self.min, self.max), max(self.min, self.max)
        v = max(lo, min(hi, v))
        if self.step:
            v = lo + round((v - lo) / self.step) * self.step
            v = round(max(lo, min(hi, v)), 10)
        self.value.set(v)
        self.show({"value": v})


@node("builtin.ui_number", title="Число (пульт)", category=UI, color=UI_COLOR, doc="""
Поле ввода числа на ноде в редакторе — уставка, порог, время. Подключите к входу
«Верхний» Гистерезиса, чтобы менять порог на ходу.
""")
class UiNumber(Node):
    initial = Param("float", 0.0, title="Начальное")
    value = Out("Number", title="Значение")

    def start(self):
        self._set(self.initial)

    def ui(self, value):
        self._set(_num(value))

    def _set(self, v: float):
        self.value.set(v)
        self.show({"value": v})


@node("builtin.watch", title="Watch", category=DBG, color=DBG_COLOR, doc="""
Показывает значение входа прямо на ноде (число, Bool, строка, Json) и его источник;
для чисел редактор рисует спарклайн. На работу проекта не влияет.
""")
class Watch(Node):
    value = In("Any", title="Значение")

    def start(self):
        self.on_change({"value"})

    def on_change(self, changed):
        s = self.sample("value")
        v = s.value if s else self.value
        self.show({"value": jsonable(v), "origin": str(s.origin) if s else None, "t": s.ts if s else None})


@node("builtin.log", title="Лог", category=DBG, color=DBG_COLOR, doc="""
Пишет строку в лог проекта. Если вход «Когда» подключён — по событию, иначе — при
каждом изменении «Значения». В тексте `{value}` заменяется значением.

Пример: UART: чтение (Получено) → Лог «GPS: {value}».
""")
class Log(Node):
    message = Param("str", "{value}", title="Текст", doc="{value} — значение входа")
    when = In("Event", title="Когда")
    value = In("Any", title="Значение")

    def _write(self, v: Any):
        if isinstance(v, float):
            v = format_number(v)
        self.log(self.message.replace("{value}", str(v)))

    def on_event(self, name, ev):
        self._write(self.value if self.connected("value") else ev.payload)

    def on_change(self, changed):
        if not self.connected("when"):
            self._write(self.value)
