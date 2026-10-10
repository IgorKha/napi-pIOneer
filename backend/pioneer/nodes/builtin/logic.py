"""Логика и математика (spec 3.7)."""
from __future__ import annotations

import collections
import math

from pioneer.engine.expr import ExprError, compile_expr
from pioneer.engine.node import In, Node, NodeError, Out, Param, node

LOGIC, LOGIC_COLOR = "Логика", "#3b7a57"
MATH, MATH_COLOR = "Математика", "#3a5f8f"


class Func(Node):
    """Чистая функция входов: пересчёт при старте и при любом изменении входа."""

    def start(self):
        self.compute()

    def on_change(self, changed):
        self.compute()

    def compute(self) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------- логика
@node("builtin.and", title="И (AND)", category=LOGIC, color=LOGIC_COLOR, doc="""
true, если оба входа true. Неподключённый вход — галочка на ноде.

Пример: «Дверь закрыта» И «Ночь» → сигнализация.
""")
class And(Func):
    a = In("Bool", True, title="A")
    b = In("Bool", True, title="B")
    out = Out("Bool", title="Результат", doc="A и B")

    def compute(self):
        self.out.set(self.a and self.b)


@node("builtin.or", title="ИЛИ (OR)", category=LOGIC, color=LOGIC_COLOR, doc="""
true, если хотя бы один вход true.

Пример: кнопка на стене ИЛИ кнопка в пульте → свет.
""")
class Or(Func):
    a = In("Bool", False, title="A")
    b = In("Bool", False, title="B")
    out = Out("Bool", title="Результат", doc="A или B")

    def compute(self):
        self.out.set(self.a or self.b)


@node("builtin.xor", title="Исключающее ИЛИ (XOR)", category=LOGIC, color=LOGIC_COLOR, doc="""
true, если входы различаются (ровно один true). Классика — проходной выключатель.
""")
class Xor(Func):
    a = In("Bool", False, title="A")
    b = In("Bool", False, title="B")
    out = Out("Bool", title="Результат", doc="A ≠ B")

    def compute(self):
        self.out.set(self.a != self.b)


@node("builtin.not", title="НЕ (NOT)", category=LOGIC, color=LOGIC_COLOR, doc="""
Инверсия: true ↔ false.

Пример: датчик «дверь закрыта» → НЕ → «дверь открыта» → сигнал.
""")
class Not(Func):
    value = In("Bool", False, title="Вход")
    out = Out("Bool", title="Результат", doc="НЕ вход")

    def compute(self):
        self.out.set(not self.value)


_CMP = {"gt": (">", lambda a, b, e: a > b), "ge": ("≥", lambda a, b, e: a >= b),
        "lt": ("<", lambda a, b, e: a < b), "le": ("≤", lambda a, b, e: a <= b),
        "eq": ("=", lambda a, b, e: abs(a - b) <= e), "ne": ("≠", lambda a, b, e: abs(a - b) > e)}


@node("builtin.compare", title="Сравнение", category=LOGIC, color=LOGIC_COLOR, doc="""
Сравнивает два числа: A > B, A ≥ B, A < B, A ≤ B, A = B, A ≠ B. Для «=» и «≠» есть допуск
(числа с АЦП почти никогда не равны точно).

Для порога с «залипанием» (без дребезга около порога) используйте Гистерезис.
""")
class Compare(Func):
    op = Param("choice", "gt", title="Операция", choices=[(k, v[0]) for k, v in _CMP.items()], doc="Как сравнивать")
    epsilon = Param("float", 1e-6, title="Допуск", min=0, doc="Для = и ≠: |A − B| ≤ допуска считается равенством")
    a = In("Number", 0.0, title="A")
    b = In("Number", 0.0, title="B")
    out = Out("Bool", title="Результат")

    def compute(self):
        self.out.set(_CMP[self.op][1](self.a, self.b, self.epsilon))


@node("builtin.hysteresis", title="Гистерезис", category=LOGIC, color=LOGIC_COLOR, doc="""
Порог с двумя уровнями: выход становится true, когда значение ≥ «Верхний», и false —
когда ≤ «Нижний»; между ними держит прежнее состояние. Убирает дребезг реле около порога.

Пример: температура → Гистерезис (30/35) → вентилятор; с «Инвертировать» — обогреватель.
""")
class Hysteresis(Node):
    invert = Param("bool", False, title="Инвертировать", doc="true при низком значении (нагрев вместо охлаждения)")
    value = In("Number", 0.0, title="Значение")
    low = In("Number", 30.0, title="Нижний", doc="Ниже или равно — выключить")
    high = In("Number", 35.0, title="Верхний", doc="Выше или равно — включить")
    out = Out("Bool", title="Выход")

    def start(self):
        self.state = self.value >= self.high
        self.out.set(self.state != self.invert)

    def on_change(self, changed):
        if self.low > self.high:
            raise NodeError("«Нижний» больше «Верхнего»")
        if self.value >= self.high:
            self.state = True
        elif self.value <= self.low:
            self.state = False
        self.out.set(self.state != self.invert)


@node("builtin.select", title="Выбор", category=LOGIC, color=LOGIC_COLOR, doc="""
Переключатель: если «Условие» true — на выходе «Если да», иначе «Если нет».

Пример: режим «Ручной» → Выбор(слайдер, автомат) → ШИМ.
""")
class Select(Func):
    cond = In("Bool", False, title="Условие")
    if_true = In("Number", 1.0, title="Если да")
    if_false = In("Number", 0.0, title="Если нет")
    out = Out("Number", title="Результат")

    def compute(self):
        self.out.set(self.if_true if self.cond else self.if_false)


# ---------------------------------------------------------------- математика
_MATH = {
    "add": ("A + B", lambda a, b: a + b), "sub": ("A − B", lambda a, b: a - b),
    "mul": ("A × B", lambda a, b: a * b), "div": ("A ÷ B", lambda a, b: a / b),
    "pow": ("A ^ B", lambda a, b: a ** b), "mod": ("A mod B", lambda a, b: math.fmod(a, b)),
    "min": ("min(A, B)", min), "max": ("max(A, B)", max),
    "abs": ("|A|", lambda a, b: abs(a)), "neg": ("−A", lambda a, b: -a),
    "round": ("округлить A", lambda a, b: float(round(a))), "floor": ("⌊A⌋", lambda a, b: float(math.floor(a))),
    "ceil": ("⌈A⌉", lambda a, b: float(math.ceil(a))), "sqrt": ("√A", lambda a, b: math.sqrt(a)),
}


@node("builtin.math", title="Математика", category=MATH, color=MATH_COLOR, doc="""
Операция над числами, выбирается дропдауном (как Math в Blender): + − × ÷ ^, mod, min,
max, |A|, −A, округление, √. Для унарных операций вход B не используется.

Деление на ноль и корень из отрицательного — ошибка ноды (она краснеет), выход сохраняет
прежнее значение.
""")
class Math(Func):
    op = Param("choice", "add", title="Операция", choices=[(k, v[0]) for k, v in _MATH.items()], doc="Что считать")
    a = In("Number", 0.0, title="A")
    b = In("Number", 0.0, title="B")
    result = Out("Number", title="Результат")

    def compute(self):
        try:
            r = _MATH[self.op][1](self.a, self.b)
        except ZeroDivisionError:
            raise NodeError("деление на ноль") from None
        except (ValueError, OverflowError) as e:
            raise NodeError(str(e)) from None
        if isinstance(r, complex) or not math.isfinite(r):
            raise NodeError("результат не число")
        self.result.set(r)


@node("builtin.map_range", title="Map Range", category=MATH, color=MATH_COLOR, doc="""
Линейно переводит значение из одного диапазона в другой: из [Из: мин, Из: макс] в
[В: мин, В: макс]. С «Ограничить» результат не выходит за целевой диапазон.

Пример: Аналоговый вход «Доля» 0…1 → Map Range → 0…100 %.
""")
class MapRange(Func):
    clamp = Param("bool", True, title="Ограничить", doc="Не выходить за [В: мин, В: макс]")
    value = In("Number", 0.0, title="Значение")
    from_min = In("Number", 0.0, title="Из: мин")
    from_max = In("Number", 1.0, title="Из: макс")
    to_min = In("Number", 0.0, title="В: мин")
    to_max = In("Number", 100.0, title="В: макс")
    result = Out("Number", title="Результат")

    def compute(self):
        if self.from_max == self.from_min:
            raise NodeError("«Из: мин» равен «Из: макс»")
        t = (self.value - self.from_min) / (self.from_max - self.from_min)
        if self.clamp:
            t = min(1.0, max(0.0, t))
        self.result.set(self.to_min + t * (self.to_max - self.to_min))


@node("builtin.clamp", title="Ограничение", category=MATH, color=MATH_COLOR, doc="""
Ограничивает значение диапазоном [Мин, Макс]: меньше Мин — Мин, больше Макс — Макс.

Пример: Формула → Ограничение 0…1 → ШИМ (заполнение не выйдет за допустимое).
""")
class Clamp(Func):
    value = In("Number", 0.0, title="Значение")
    min = In("Number", 0.0, title="Мин")
    max = In("Number", 1.0, title="Макс")
    result = Out("Number", title="Результат")

    def compute(self):
        lo, hi = self.min, self.max
        if lo > hi:
            raise NodeError("«Мин» больше «Макс»")
        self.result.set(min(hi, max(lo, self.value)))


@node("builtin.expression", title="Формула", category=MATH, color=MATH_COLOR, doc="""
Считает выражение от входов value, a, b, c: `{{value}} * 0.1`, `(a + b) / 2`,
`clamp(value * 100, 0, 100)`, `a if value > 0.5 else b`.

Доступно: + − * / // % **, сравнения, and/or/not, x if условие else y, функции abs min max
round floor ceil sqrt sin cos tan log exp clamp, константы pi e. Вычисляется безопасно
(разбор AST, без eval).

Пример: Аналоговый вход → Формула `{{value}} * 100 / 1.8` → UART: запись.
""")
class Expression(Func):
    expr = Param("expression", "{{value}} * 0.1", title="Формула", required=True, doc="Выражение от value, a, b, c")
    value = In("Number", 0.0, title="value")
    a = In("Number", 0.0, title="a")
    b = In("Number", 0.0, title="b")
    c = In("Number", 0.0, title="c")
    result = Out("Number", title="Результат")

    def setup(self):
        self._fn = compile_expr(self.expr)

    def compute(self):
        try:
            r = self._fn(value=self.value, a=self.a, b=self.b, c=self.c)
        except ExprError as e:
            raise NodeError(str(e)) from None
        self.result.set(r)


@node("builtin.smooth", title="Сглаживание", category=MATH, color=MATH_COLOR, doc="""
Скользящее среднее по последним N значениям входа. Новое значение добавляется при каждом
изменении входа. Сглаживает шум АЦП (вместе с «Усреднением» самой ноды входа).
""")
class Smooth(Node):
    window = Param("int", 10, title="Окно", min=1, max=1000, doc="Сколько последних значений усреднять")
    value = In("Number", 0.0, title="Значение")
    result = Out("Number", title="Среднее")

    def setup(self):
        self._win = collections.deque(maxlen=self.window)

    def start(self):
        self.on_change({"value"})

    def on_change(self, changed):
        self._win.append(self.value)
        self.result.set(sum(self._win) / len(self._win))
