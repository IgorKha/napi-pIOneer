"""События и время (spec 3.7). Таймеры — через self.later/every/at движка: продолжения
наследуют origin и контекст запроса волны."""
from __future__ import annotations

import datetime as dt

from pioneer.engine.cron import parse_cron
from pioneer.engine.node import In, Node, NodeError, Out, Param, node

CAT, COLOR = "События и время", "#8a6d1f"


@node("builtin.edge", title="Фронт", category=CAT, color=COLOR, doc="""
Превращает изменение Bool в событие: по включению (false→true), выключению или обоим.

Пример: Гистерезис → Фронт (включение) → Счётчик — сколько раз включался насос.
""")
class Edge(Node):
    mode = Param("choice", "rising", title="Когда",
                 choices=[("rising", "включение"), ("falling", "выключение"), ("both", "оба")], doc="Какой фронт ловить")
    value = In("Bool", False, title="Вход")
    event = Out("Event", title="Событие", doc="payload — новое значение входа")

    def start(self):
        self._prev = self.value

    def on_change(self, changed):
        v = self.value
        if v != self._prev and (self.mode == "both" or (self.mode == "rising") == v):
            self.event.emit(v)
        self._prev = v


@node("builtin.pulse", title="Импульс", category=CAT, color=COLOR, doc="""
По событию включает выход на заданное время. С «Перезапуск» повторное событие продлевает
импульс, без него — игнорируется, пока импульс идёт.

Пример: кнопка (Включение) → Импульс 10 с → реле — «свет на 10 секунд».
""")
class Pulse(Node):
    retrigger = Param("bool", True, title="Перезапуск", doc="Новое событие во время импульса начинает отсчёт заново")
    trigger = In("Event", title="Запуск")
    duration_ms = In("Number", 1000.0, title="Длительность", min=1, unit="мс")
    out = Out("Bool", title="Выход")

    def start(self):
        self._timer = None
        self.out.set(False)

    def on_event(self, name, ev):
        if self._timer is not None and self._timer.active:
            if not self.retrigger:
                return
            self._timer.cancel()
        self.out.set(True)
        self._timer = self.later(max(1.0, self.duration_ms) / 1000, lambda: self.out.set(False))


@node("builtin.toggle", title="Переключатель", category=CAT, color=COLOR, doc="""
Каждое событие «Переключить» меняет выход на противоположный; «Сброс» возвращает к
начальному значению.

Пример: кнопка (Включение) → Переключатель → LED — одна кнопка вкл/выкл.
""")
class Toggle(Node):
    initial = Param("bool", False, title="Начальное", doc="Значение при старте и после сброса")
    toggle = In("Event", title="Переключить")
    reset = In("Event", title="Сброс")
    out = Out("Bool", title="Состояние")

    def start(self):
        self.out.set(self.initial)

    def on_event(self, name, ev):
        self.out.set(self.initial if name == "reset" else not self.out.value)


@node("builtin.counter", title="Счётчик", category=CAT, color=COLOR, doc="""
Считает события: «+» прибавляет шаг, «−» вычитает, «Сброс» — к начальному значению.
""")
class Counter(Node):
    step = Param("float", 1.0, title="Шаг")
    initial = Param("float", 0.0, title="Начальное")
    up = In("Event", title="+")
    down = In("Event", title="−")
    reset = In("Event", title="Сброс")
    count = Out("Number", title="Счёт")

    def start(self):
        self.count.set(self.initial)

    def on_event(self, name, ev):
        cur = self.count.value or 0.0
        self.count.set({"up": cur + self.step, "down": cur - self.step, "reset": self.initial}[name])


@node("builtin.latch", title="Защёлка (SR)", category=CAT, color=COLOR, breaks_cycle=True, doc="""
RS-триггер: «Установить» → true, «Сбросить» → false; держит состояние между событиями.
Через защёлку можно замкнуть граф в цикл.

Пример: датчик протечки (Включение) → Установить; кнопка «сброс аварии» → Сбросить.
""")
class Latch(Node):
    initial = Param("bool", False, title="Начальное")
    set_on = In("Event", title="Установить")
    reset = In("Event", title="Сбросить")
    out = Out("Bool", title="Состояние")

    def start(self):
        self.out.set(self.initial)

    def on_event(self, name, ev):
        self.out.set(name == "set_on")


@node("builtin.debounce", title="Антидребезг", category=CAT, color=COLOR, doc="""
Пропускает изменение, только если вход продержался в новом состоянии заданное время.
Программный антидребезг для любых Bool (у Цифрового входа есть свой, ядерный).
""")
class Debounce(Node):
    value = In("Bool", False, title="Вход")
    time_ms = In("Number", 50.0, title="Время", min=0, unit="мс")
    out = Out("Bool", title="Выход")

    def start(self):
        self._timer = None
        self.out.set(self.value)

    def on_change(self, changed):
        if "value" not in changed:
            return
        if self._timer is not None:
            self._timer.cancel()
        target = self.value
        if target == self.out.value:
            return
        self._timer = self.later(max(0.0, self.time_ms) / 1000, lambda: self.out.set(target))


@node("builtin.long_press", title="Долгое нажатие", category=CAT, color=COLOR, doc="""
Различает короткое и долгое нажатие: «Долгое» — сразу, как кнопка продержалась заданное
время; «Короткое» — при отпускании раньше.

Пример: кнопка → Долгое нажатие: короткое — свет, долгое — выключить всё.
""")
class LongPress(Node):
    time_ms = Param("int", 800, title="Порог", min=50, max=60_000, unit="мс", doc="Сколько держать для «долгого»")
    pressed = In("Bool", False, title="Нажата")
    long = Out("Event", title="Долгое")
    short = Out("Event", title="Короткое")

    def start(self):
        self._timer, self._fired, self._prev = None, False, self.pressed

    def on_change(self, changed):
        p = self.pressed
        if p == self._prev:
            return
        self._prev = p
        if p:
            self._fired = False
            self._timer = self.later(self.time_ms / 1000, self._long)
        else:
            if self._timer is not None:
                self._timer.cancel()
            if not self._fired:
                self.short.emit()

    def _long(self):
        self._fired = True
        self.long.emit()


@node("builtin.delay", title="Задержка", category=CAT, color=COLOR, breaks_cycle=True, doc="""
Задерживает Bool:
- «сдвиг» — каждое изменение повторяется на выходе через заданное время;
- «включения» — выход включается, если вход продержался true всё время (выключается сразу);
- «выключения» — выход выключается, если вход продержался false всё время (включается сразу).

Через задержку можно замкнуть граф в цикл.

Пример: Гистерезис → Задержка включения 5 с → реле: не дёргать насос от коротких всплесков.
""")
class Delay(Node):
    mode = Param("choice", "on", title="Режим",
                 choices=[("both", "сдвиг"), ("on", "включения"), ("off", "выключения")], doc="Что задерживать")
    value = In("Bool", False, title="Вход")
    delay_ms = In("Number", 1000.0, title="Задержка", min=0, unit="мс")
    out = Out("Bool", title="Выход")

    def start(self):
        self._timers = []
        self.out.set(self.value)

    def on_change(self, changed):
        if "value" not in changed:
            return
        v, d = self.value, max(0.0, self.delay_ms) / 1000
        self._timers = [t for t in self._timers if t.active]
        if self.mode == "both":
            self._timers.append(self.later(d, lambda: self.out.set(v)))
            return
        for t in self._timers:
            t.cancel()
        delayed = v if self.mode == "on" else not v
        if delayed:
            self._timers = [self.later(d, lambda: self.out.set(v))]
        else:
            self.out.set(v)


@node("builtin.feedback", title="Обратная связь", category=CAT, color=COLOR, breaks_cycle=True, doc="""
Передаёт число на выход в следующей волне (задержка на «один шаг»). Нужна, чтобы
замкнуть цикл в графе, например накопитель: Формула(value + a) → Обратная связь → a.
""")
class Feedback(Node):
    value = In("Number", 0.0, title="Вход")
    out = Out("Number", title="Выход")

    def start(self):
        self.out.set(self.value)

    def on_change(self, changed):
        v = self.value
        self.later(0, lambda: self.out.set(v))


@node("builtin.interval", title="Интервал", category=CAT, color=COLOR, doc="""
Генерирует событие каждые N мс, пока «Включено» true. payload — номер тика.

Пример: Интервал 1000 мс → Переключатель → LED — мигалка.
""")
class Interval(Node):
    enabled = In("Bool", True, title="Включено")
    period_ms = In("Number", 1000.0, title="Период", min=10, unit="мс")
    tick = Out("Event", title="Тик")

    def start(self):
        self._timer, self._n = None, 0
        self._arm()

    def on_change(self, changed):
        self._arm()

    def _arm(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self.enabled:
            if self.period_ms < 10:
                raise NodeError("период меньше 10 мс")
            self._timer = self.every(self.period_ms / 1000, self._tick)

    def _tick(self):
        self._n += 1
        self.tick.emit(self._n)


@node("builtin.schedule", title="Расписание", category=CAT, color=COLOR, doc="""
Событие по расписанию cron (местное время платы): «минута час день месяц день-недели».

Примеры: `0 8 * * *` — каждый день в 8:00; `*/15 8-20 * * 1-5` — каждые 15 минут с 8 до
20 по будням; `@hourly` — каждый час.

Без RTC часы платы идут от NTP: без сети после перезагрузки время может быть неверным.
""")
class Schedule(Node):
    cron = Param("cron", "0 8 * * *", title="Расписание (cron)", required=True, doc="минута час день месяц день-недели")
    tick = Out("Event", title="Сработало", doc="payload — время срабатывания (ISO)")

    def setup(self):
        self._cron = parse_cron(self.cron)

    def start(self):
        self.at(lambda: self._cron.next_after(dt.datetime.now()).timestamp(), self._fire)

    def _fire(self):
        self.tick.emit(dt.datetime.now().isoformat(timespec="seconds"))


@node("builtin.on_start", title="При запуске", category=CAT, color=COLOR, doc="""
Одно событие при запуске проекта (и после перезагрузки платы, если включён автозапуск).
""")
class OnStart(Node):
    started = Out("Event", title="Запуск")

    def start(self):
        self.started.emit()
