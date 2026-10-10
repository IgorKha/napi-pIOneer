"""SDK скриптов pioneer — исполняется в worker-процессе, железо — через core (RPC).

    from pioneer.sdk import Input, Output, log

    led = Output("J9-7")
    button = Input("J10-2", debounce_ms=30)

    @button.on_press
    def toggle(event):
        led.toggle()

Если после выполнения скрипта есть обработчики или таймеры, цикл событий запускается
сам (явный run() не обязателен). Ресурсы освобождаются при остановке скрипта, выходы
уходят в безопасное состояние (LOW).
"""
from __future__ import annotations

import heapq
import itertools
import queue
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass

from pioneer.sdk import _rpc

__all__ = ["Input", "Output", "Pwm", "Adc", "I2C", "SPI", "UART", "Event",
           "every", "after", "run", "sleep", "log", "PioneerError"]

PioneerError = _rpc.PioneerError


@dataclass(frozen=True)
class Event:
    pin: str
    rising: bool
    time: float  # монотонное время фронта, секунды

    @property
    def falling(self) -> bool:
        return not self.rising


def log(*args, **kwargs) -> None:
    print(*args, **kwargs, flush=True)


# ---------------------------------------------------------------- GPIO / ADC
class Output:
    """Цифровой выход. initial — уровень при захвате."""

    def __init__(self, pin: str, initial: bool = False):
        self.pin, self._value = pin, bool(initial)
        _rpc.call("acquire", pin=pin, mode="gpio_out", initial=self._value)

    @property
    def value(self) -> bool:
        return self._value

    @value.setter
    def value(self, v: bool) -> None:
        self._value = bool(v)
        _rpc.call("write", pin=self.pin, value=self._value)

    def on(self) -> None:
        self.value = True

    def off(self) -> None:
        self.value = False

    def toggle(self) -> None:
        self.value = not self._value


class Input:
    """Цифровой вход с ядерным debounce. active_low — «нажатие» = спад (кнопка на GND
    с подтяжкой вверх); подтяжку софтом не поменять — см. страницу «Плата»."""

    def __init__(self, pin: str, debounce_ms: int = 0, active_low: bool = False):
        self.pin, self.active_low = pin, active_low
        self._handlers: list[tuple[str, Callable[[Event], None]]] = []
        _rpc.call("acquire", pin=pin, mode="gpio_in", debounce_ms=int(debounce_ms))
        _loop.inputs.setdefault(pin, []).append(self)

    @property
    def value(self) -> bool:
        return bool(_rpc.call("read", pin=self.pin))

    @property
    def pressed(self) -> bool:
        return self.value != self.active_low

    def _on(self, kind: str):
        def deco(fn):
            self._handlers.append((kind, fn))
            return fn
        return deco

    def on_rise(self, fn):
        return self._on("rise")(fn)

    def on_fall(self, fn):
        return self._on("fall")(fn)

    def on_change(self, fn):
        return self._on("change")(fn)

    def on_press(self, fn):
        return self._on("fall" if self.active_low else "rise")(fn)

    def on_release(self, fn):
        return self._on("rise" if self.active_low else "fall")(fn)

    def _fire(self, ev: Event) -> None:
        for kind, fn in self._handlers:
            if kind == "change" or (kind == "rise") == ev.rising:
                _loop.call(fn, ev)


class Pwm:
    """Soft-PWM (≤ 500 Гц). duty — 0…1."""

    def __init__(self, pin: str, freq: float = 100.0, duty: float = 0.0):
        self.pin = pin
        _rpc.call("acquire", pin=pin, mode="soft_pwm")
        self._freq, self._duty = float(freq), float(duty)
        _rpc.call("pwm", pin=pin, freq=self._freq, duty=self._duty)

    @property
    def duty(self) -> float:
        return self._duty

    @duty.setter
    def duty(self, d: float) -> None:
        self._duty = float(d)
        _rpc.call("pwm", pin=self.pin, duty=self._duty)

    @property
    def freq(self) -> float:
        return self._freq

    @freq.setter
    def freq(self, f: float) -> None:
        self._freq = float(f)
        _rpc.call("pwm", pin=self.pin, freq=self._freq)


class Adc:
    """Вход АЦП (J9-26, J10-3): 0…1.8 В, 10 бит."""

    def __init__(self, pin: str):
        self.pin = pin
        _rpc.call("acquire", pin=pin, mode="adc")

    @property
    def volts(self) -> float:
        return float(_rpc.call("read", pin=self.pin))


# ---------------------------------------------------------------- шины
class I2C:
    """Устройство на шине I2C (bus — номер /dev/i2c-N, addr — 7-битный адрес)."""

    def __init__(self, bus: int, addr: int):
        self.bus, self.addr = bus, addr
        self._h = _rpc.call("i2c_open", bus=bus, addr=addr)

    def write(self, data: bytes) -> None:
        _rpc.call("i2c", h=self._h, kind="write", data=bytes(data).hex())

    def read(self, n: int) -> bytes:
        return bytes.fromhex(_rpc.call("i2c", h=self._h, kind="read", n=n))

    def write_read(self, data: bytes, n: int) -> bytes:
        return bytes.fromhex(_rpc.call("i2c", h=self._h, kind="write_read", data=bytes(data).hex(), n=n))

    def read_reg(self, reg: int, n: int = 1) -> bytes:
        return self.write_read(bytes([reg]), n)

    def write_reg(self, reg: int, data: bytes | int) -> None:
        self.write(bytes([reg]) + (bytes([data]) if isinstance(data, int) else bytes(data)))


class SPI:
    def __init__(self, bus: int, cs: int = 0, mode: int = 0, speed_hz: int = 1_000_000):
        self._h = _rpc.call("spi_open", bus=bus, cs=cs, mode=mode, speed_hz=speed_hz)

    def xfer(self, data: bytes) -> bytes:
        return bytes.fromhex(_rpc.call("spi_xfer", h=self._h, data=bytes(data).hex()))


class UART:
    def __init__(self, device: str, baud: int = 9600):
        self._h = _rpc.call("uart_open", device=device, baud=baud)

    def write(self, data: bytes | str) -> None:
        data = data.encode() if isinstance(data, str) else bytes(data)
        _rpc.call("uart_write", h=self._h, data=data.hex())

    def read(self, max_bytes: int = 4096, timeout: float = 0.0) -> bytes:
        return bytes.fromhex(_rpc.call("uart_read", h=self._h, max=max_bytes, timeout=timeout))


# ---------------------------------------------------------------- цикл событий
class _Loop:
    def __init__(self):
        self.inputs: dict[str, list[Input]] = {}
        self.timers: list[tuple[float, int, Callable[[], None], float | None]] = []
        self._seq = itertools.count()
        self.running = False

    @property
    def has_work(self) -> bool:
        return bool(self.timers) or any(i._handlers for ins in self.inputs.values() for i in ins)

    def add_timer(self, delay: float, fn: Callable[[], None], interval: float | None) -> None:
        heapq.heappush(self.timers, (time.monotonic() + delay, next(self._seq), fn, interval))

    @staticmethod
    def call(fn, *args) -> None:
        # Ошибка в обработчике не роняет скрипт (spec 3.4): печатаем и живём дальше.
        try:
            fn(*args)
        except PioneerError as e:
            print(f"ошибка: {e}", file=sys.stderr, flush=True)
        except Exception:
            traceback.print_exc()
            sys.stderr.flush()

    def run(self) -> None:
        self.running = True
        while True:
            timeout = None
            if self.timers:
                timeout = max(0.0, self.timers[0][0] - time.monotonic())
            try:
                msg = _rpc.events().get(timeout=timeout)
            except queue.Empty:
                msg = None
            if msg is not None:
                if msg.get("ev") == "stop":
                    return
                if msg.get("ev") == "edge":
                    ev = Event(msg["pin"], msg["rising"], msg["ts"] / 1e9)
                    for inp in self.inputs.get(ev.pin, []):
                        inp._fire(ev)
            now = time.monotonic()
            while self.timers and self.timers[0][0] <= now:
                due, _, fn, interval = heapq.heappop(self.timers)
                self.call(fn)
                if interval is not None:
                    # без накопления дрейфа; если сильно отстали — от текущего момента
                    nxt = due + interval
                    heapq.heappush(self.timers, (nxt if nxt > now else now + interval,
                                                 next(self._seq), fn, interval))


_loop = _Loop()


def every(seconds: float):
    """Декоратор: вызывать функцию каждые seconds секунд."""
    def deco(fn):
        _loop.add_timer(seconds, fn, float(seconds))
        return fn
    return deco


def after(seconds: float, fn: Callable[[], None]) -> None:
    """Однократно вызвать fn через seconds секунд."""
    _loop.add_timer(seconds, fn, None)


def run() -> None:
    """Запустить цикл событий (не возвращается до остановки скрипта)."""
    _loop.run()


def sleep(seconds: float) -> None:
    time.sleep(seconds)
