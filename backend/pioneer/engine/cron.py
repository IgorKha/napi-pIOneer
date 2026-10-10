"""Минимальный cron (5 полей: минута час день месяц день-недели) для ноды Schedule.

Поддерживается: `*`, числа, списки `1,5`, диапазоны `1-5`, шаги `*/15`, `1-30/5`,
дни недели 0–7 (0 и 7 — воскресенье), алиасы @hourly @daily @weekly @monthly.
Если ограничены и день месяца, и день недели — срабатывает по любому (как в cron).
Время — локальное время платы.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

_ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
            "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *"}
_FIELDS = (("минута", 0, 59), ("час", 0, 23), ("день", 1, 31), ("месяц", 1, 12), ("день недели", 0, 7))


class CronError(ValueError):
    pass


@dataclass(frozen=True)
class Cron:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = воскресенье
    days_any: bool
    weekdays_any: bool

    def _day_ok(self, d: dt.date) -> bool:
        wd = (d.weekday() + 1) % 7
        dom, dow = d.day in self.days, wd in self.weekdays
        if self.days_any and self.weekdays_any:
            return True
        if self.days_any:
            return dow
        if self.weekdays_any:
            return dom
        return dom or dow

    def next_after(self, t: dt.datetime) -> dt.datetime:
        """Ближайший момент строго после t (секунды обнуляются)."""
        t = t.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        limit = t + dt.timedelta(days=366 * 5)
        while t < limit:
            if t.month not in self.months:
                t = (t.replace(day=1, hour=0, minute=0) + dt.timedelta(days=32)).replace(day=1)
                continue
            if not self._day_ok(t.date()):
                t = t.replace(hour=0, minute=0) + dt.timedelta(days=1)
                continue
            if t.hour not in self.hours:
                t = t.replace(minute=0) + dt.timedelta(hours=1)
                continue
            if t.minute not in self.minutes:
                t += dt.timedelta(minutes=1)
                continue
            return t
        raise CronError("расписание никогда не срабатывает")


def _field(text: str, name: str, lo: int, hi: int) -> tuple[frozenset[int], bool]:
    out: set[int] = set()
    for part in text.split(","):
        rng, _, step_s = part.partition("/")
        try:
            step = int(step_s) if step_s else 1
            if rng == "*":
                a, b = lo, hi
            elif "-" in rng:
                a, b = (int(x) for x in rng.split("-", 1))
            else:
                a = b = int(rng)
                if step_s:
                    b = hi
        except ValueError:
            raise CronError(f"{name}: не понимаю {part!r}") from None
        if step < 1 or not (lo <= a <= hi and lo <= b <= hi and a <= b):
            raise CronError(f"{name}: {part!r} вне диапазона {lo}–{hi}")
        out.update(range(a, b + 1, step))
    return frozenset(out), text == "*"


def parse_cron(text: str) -> Cron:
    """Разобрать строку cron.

    Raises:
        CronError: с указанием поля, где ошибка.
    """
    text = _ALIASES.get(text.strip(), text.strip())
    parts = text.split()
    if len(parts) != 5:
        raise CronError("нужно 5 полей: минута час день месяц день-недели (например «*/15 8-20 * * 1-5»)")
    vals = [_field(p, *f) for p, f in zip(parts, _FIELDS)]
    wd = frozenset(d % 7 for d in vals[4][0])
    return Cron(vals[0][0], vals[1][0], vals[2][0], vals[3][0], wd, vals[2][1], vals[4][1])
