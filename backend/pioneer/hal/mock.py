"""Mock-HAL: симуляция пинов для разработки на Mac и тестов.

- `set_level(gpio, v)` — «внешний мир» выставляет уровень на входе;
- `wire(out, in_)` — перемычка: выход повторяется на входе (как J9-16↔J9-18 в пробе);
- `set_adc(ch, volts)` — значение на ADC;
- выходы без перемычек просто хранят значение, в `levels` видно всё.

Debounce моделирует ядерную семантику: событие приходит, только если линия
простояла в новом уровне debounce_ms.
"""
from __future__ import annotations

import threading
import time

from pioneer.board import GpioRef
from pioneer.hal.base import HAL, EdgeCallback, EdgeEvent, EdgeMode, InputLine, OutputLine


class LineBusy(OSError):
    pass


class _MockInput(InputLine):
    def __init__(self, hal: MockHAL, gpio: GpioRef, edge: EdgeMode, debounce_ms: int,
                 on_edge: EdgeCallback | None):
        self.hal, self.gpio = hal, gpio
        self.edge, self.debounce_s, self.on_edge = edge, debounce_ms / 1000, on_edge
        self.seqno = 0
        self.reported = hal.levels.get(gpio, False)
        self._timer: threading.Timer | None = None

    def read(self) -> bool:
        return self.hal.levels.get(self.gpio, False)

    def close(self) -> None:
        if self._timer:
            self._timer.cancel()
        self.hal._release(self.gpio, self)

    def _changed(self, level: bool) -> None:
        if self.debounce_s <= 0:
            self._report(level)
            return
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(self.debounce_s, self._settled)
        self._timer.daemon = True
        self._timer.start()

    def _settled(self) -> None:
        self._report(self.read())

    def _report(self, level: bool) -> None:
        if level == self.reported:
            return
        self.reported = level
        if self.on_edge is None or self.edge == "none":
            return
        if self.edge == "rising" and not level or self.edge == "falling" and level:
            return
        self.seqno += 1
        self.on_edge(EdgeEvent(rising=level, timestamp_ns=time.monotonic_ns(), seqno=self.seqno))


class _MockOutput(OutputLine):
    def __init__(self, hal: MockHAL, gpio: GpioRef, initial: bool):
        self.hal, self.gpio = hal, gpio
        self.write(initial)

    def write(self, value: bool) -> None:
        self.hal._drive(self.gpio, bool(value))

    def read(self) -> bool:
        return self.hal.levels.get(self.gpio, False)

    def close(self) -> None:
        self.hal._release(self.gpio, self)


class MockHAL(HAL):
    name = "mock"

    def __init__(self, adc_vref: float = 1.8):
        self.adc_vref = adc_vref
        self.levels: dict[GpioRef, bool] = {}
        self.adc: dict[int, float] = {}
        self.wires: dict[GpioRef, list[GpioRef]] = {}
        self._owners: dict[GpioRef, _MockInput | _MockOutput] = {}
        self._lock = threading.RLock()

    # --- HAL
    def open_input(self, gpio: GpioRef, *, edge: EdgeMode = "none", debounce_ms: int = 0,
                   on_edge: EdgeCallback | None = None) -> InputLine:
        with self._lock:
            self._claim(gpio)
            line = _MockInput(self, gpio, edge, debounce_ms, on_edge)
            self._owners[gpio] = line
            return line

    def open_output(self, gpio: GpioRef, *, initial: bool = False) -> OutputLine:
        with self._lock:
            self._claim(gpio)
            self._owners[gpio] = None  # type: ignore[assignment]  # занят до конструктора
            line = _MockOutput(self, gpio, initial)
            self._owners[gpio] = line
            return line

    def read_adc(self, channel: int) -> float:
        return min(max(self.adc.get(channel, 0.0), 0.0), self.adc_vref)

    def close(self) -> None:
        with self._lock:
            self._owners.clear()

    # --- управление симуляцией
    def set_level(self, gpio: GpioRef, value: bool) -> None:
        self._drive(gpio, bool(value))

    def set_adc(self, channel: int, volts: float) -> None:
        self.adc[channel] = volts

    def wire(self, a: GpioRef, b: GpioRef) -> None:
        self.wires.setdefault(a, []).append(b)

    # --- внутреннее
    def _claim(self, gpio: GpioRef) -> None:
        if gpio in self._owners:
            raise LineBusy(f"gpiochip{gpio.chip}:{gpio.line} уже запрошена")

    def _release(self, gpio: GpioRef, owner: object) -> None:
        with self._lock:
            if self._owners.get(gpio) is owner:
                del self._owners[gpio]

    def _drive(self, gpio: GpioRef, value: bool) -> None:
        with self._lock:
            targets = [gpio, *self.wires.get(gpio, ())]
            changed = []
            for g in targets:
                if self.levels.get(g, False) != value:
                    self.levels[g] = value
                    changed.append(g)
            inputs = [o for g in changed if isinstance(o := self._owners.get(g), _MockInput)]
        for inp in inputs:
            inp._changed(value)
