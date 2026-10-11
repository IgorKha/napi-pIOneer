"""BenchHAL — виртуальный стенд: HAL без железа, с моделями устройств (docs/ideas/001).

Работает везде: на Mac, в тестах и на плате «всухую» (hal = "bench").

Уровень пина (сеть) вычисляется так: выход core (GPIO-out/PWM) → модель, которая
вела пин последней → аппаратная подтяжка пина из boards/*.yaml (none → 0).

Модели (pioneer.hal.models) подключены к ресурсам: смотрят на пины (on_gpio/on_pwm),
ведут пины и ADC-каналы, отвечают на транзакции I2C/SPI по своему адресу/CS, слушают
и пишут в UART. Состояние модели — state(), действия из UI — action().

Колбэки core (edge-события) доставляются из отдельного потока — как в LinuxHAL,
чтобы код поверх HAL вёл себя одинаково на стенде и на железе.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from pioneer.board import Board, GpioRef
from pioneer.hal.base import (
    HAL, I2C_NACK_ERRNO, EdgeCallback, EdgeEvent, EdgeMode, I2CBus, InputLine, LedLine, LineBusy,
    OutputLine, PwmLine, SPIDevice, UARTPort,
)
from pioneer.hal.softpwm import check_pwm

if TYPE_CHECKING:
    from pioneer.hal.models import Model

log = logging.getLogger(__name__)
CORE = "core"  # источник изменений: выход core


class BenchError(ValueError):
    pass


# ---------------------------------------------------------------- линии core
class _Input(InputLine):
    def __init__(self, bench: BenchHAL, pin: str, edge: EdgeMode, debounce_ms: int,
                 on_edge: EdgeCallback | None):
        self.bench, self.pin = bench, pin
        self.edge, self.debounce_s, self.on_edge = edge, debounce_ms / 1000, on_edge
        self.seqno = 0
        self.reported = bench.level(pin)
        self._timer: threading.Timer | None = None

    def read(self) -> bool:
        return self.bench.level(self.pin)

    def close(self) -> None:
        if self._timer:
            self._timer.cancel()
        self.bench._close_input(self)

    def _changed(self) -> None:  # под локом стенда
        if self.debounce_s <= 0:
            self._report(self.bench.level(self.pin))
            return
        # Ядерная семантика debounce: событие — после debounce_s стабильного уровня.
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(self.debounce_s, self._settled)
        self._timer.daemon = True
        self._timer.start()

    def _settled(self) -> None:
        with self.bench._lock:
            self._report(self.bench.level(self.pin))

    def _report(self, level: bool) -> None:
        if level == self.reported:
            return
        self.reported = level
        if self.on_edge is None or self.edge == "none":
            return
        if self.edge == "rising" and not level or self.edge == "falling" and level:
            return
        self.seqno += 1
        self.bench._emit(self.on_edge, EdgeEvent(rising=level, timestamp_ns=time.monotonic_ns(),
                                                 seqno=self.seqno))


class _Output(OutputLine):
    def __init__(self, bench: BenchHAL, pin: str, initial: bool):
        self.bench, self.pin = bench, pin
        self.value = bool(initial)
        bench._drive_core(pin, self.value)

    def write(self, value: bool) -> None:
        self.value = bool(value)
        self.bench._drive_core(self.pin, self.value)

    def read(self) -> bool:
        return self.value

    def close(self) -> None:
        self.bench._release_core(self.pin)


class _Pwm(PwmLine):
    """Семантический PWM: модели получают freq/duty (on_pwm), без переключения линии.
    Уровень сети для остальных — duty ≥ 0.5."""

    def __init__(self, bench: BenchHAL, pin: str, freq: float, duty: float):
        self.bench, self.pin = bench, pin
        self.freq, self.duty = 100.0, 0.0
        self.set(freq, duty)

    def set(self, freq: float | None = None, duty: float | None = None) -> None:
        check_pwm(freq, duty)
        if freq is not None:
            self.freq = float(freq)
        if duty is not None:
            self.duty = float(duty)
        self.bench._set_pwm(self.pin, self.freq, self.duty)

    def close(self, safe_value: bool = False) -> None:
        self.bench._set_pwm(self.pin, None, None)
        self.bench._drive_core(self.pin, safe_value)
        self.bench._release_core(self.pin)


class _I2C(I2CBus):
    def __init__(self, bench: BenchHAL, bus: int):
        self.bench, self.bus = bench, bus

    def _dev(self, addr: int) -> Model:
        dev = self.bench._i2c.get((self.bus, addr))
        if dev is None:
            raise OSError(I2C_NACK_ERRNO, f"i2c-{self.bus}: нет ACK от 0x{addr:02x} (на стенде нет устройства)")
        return dev

    def write(self, addr: int, data: bytes) -> None:
        with self.bench._lock:
            self._dev(addr).i2c_write(self.bus, addr, bytes(data))

    def read(self, addr: int, n: int) -> bytes:
        with self.bench._lock:
            return bytes(self._dev(addr).i2c_read(self.bus, addr, n))

    def write_read(self, addr: int, data: bytes, n: int) -> bytes:
        with self.bench._lock:
            dev = self._dev(addr)
            dev.i2c_write(self.bus, addr, bytes(data))
            return bytes(dev.i2c_read(self.bus, addr, n))

    def close(self) -> None:
        pass  # шина общая, закрывается вместе с HAL


class _SPI(SPIDevice):
    def __init__(self, bench: BenchHAL, bus: int, cs: int):
        self.bench, self.bus, self.cs = bench, bus, cs

    def xfer(self, data: bytes) -> bytes:
        with self.bench._lock:
            dev = self.bench._spi.get((self.bus, self.cs))
            if dev is None:
                return b"\xff" * len(data)  # MISO никто не ведёт
            out = bytes(dev.spi_xfer(self.bus, self.cs, bytes(data)))
            return out.ljust(len(data), b"\xff")[:len(data)]

    def close(self) -> None:
        pass


class _UART(UARTPort):
    def __init__(self, bench: BenchHAL, device: str, baud: int):
        self.bench, self.device, self.baud = bench, device, baud
        self._rx = bytearray()
        self._cond = threading.Condition()

    def write(self, data: bytes) -> None:
        with self.bench._lock:
            for m in self.bench._uart_models(self.device):
                m.uart_rx(self.device, bytes(data), self.baud)

    def read(self, max_bytes: int = 4096, timeout: float = 0.0) -> bytes:
        with self._cond:
            if not self._rx and timeout > 0:
                self._cond.wait(timeout)
            out = bytes(self._rx[:max_bytes])
            del self._rx[:max_bytes]
            return out

    def set_baud(self, baud: int) -> None:
        self.baud = baud

    def close(self) -> None:
        with self.bench._lock:
            self.bench._uarts.pop(self.device, None)

    def _feed(self, data: bytes) -> None:
        with self._cond:
            self._rx += data
            self._cond.notify_all()


LED_TRIGGERS = ["none", "timer", "oneshot", "heartbeat", "default-on", "mmc0", "mmc1"]


class _Led(LedLine):
    def __init__(self, bench: BenchHAL, sysfs: str, default_trigger: str):
        self.bench, self.sysfs, self.default = bench, sysfs, default_trigger

    def set(self, on: bool) -> None:
        self.bench._leds[self.sysfs] = {"trigger": "none", "on": bool(on)}

    def set_trigger(self, trigger: str) -> None:
        if trigger not in LED_TRIGGERS:
            raise ValueError(f"нет триггера {trigger!r}")
        self.bench._leds[self.sysfs] = {"trigger": trigger, "on": False}

    def close(self) -> None:
        self.bench._leds[self.sysfs] = {"trigger": self.default, "on": False}


# ---------------------------------------------------------------- стенд
class BenchHAL(HAL):
    name = "bench"

    def __init__(self, board: Board, store: Path | None = None):
        self.board = board
        self.store = store
        self._lock = threading.RLock()
        self._pin_of: dict[GpioRef, str] = {p.gpio: p.id for p in board.pins.values() if p.gpio}
        self._busy: set[str] = set()
        self._core: dict[str, bool] = {}  # пины, которые ведёт core
        self._model_drive: dict[str, dict[str, bool]] = {}  # пин -> модель -> уровень (порядок = давность)
        self._levels: dict[str, bool] = {}
        self._inputs: dict[str, _Input] = {}
        self._pwm: dict[str, tuple[float, float]] = {}
        self._adc: dict[int, dict[str, float]] = {}
        self._i2c: dict[tuple[int, int], Model] = {}
        self._spi: dict[tuple[int, int], Model] = {}
        self._uarts: dict[str, _UART] = {}
        self._leds = {led.sysfs: {"trigger": led.default_trigger, "on": False} for led in board.leds.values()}
        self.models: dict[str, Model] = {}
        self._listeners: list[Callable[[str | None], None]] = []
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._dispatch, name="pioneer-bench", daemon=True)
        self._thread.start()
        for p in self._pin_of.values():
            self._levels[p] = self._resolve(p)
        if store and store.exists():
            self.load(json.loads(store.read_text()))

    # ---- HAL: GPIO / ADC
    def open_input(self, gpio: GpioRef, *, edge: EdgeMode = "none", debounce_ms: int = 0,
                   on_edge: EdgeCallback | None = None) -> InputLine:
        with self._lock:
            pin = self._claim(gpio)
            line = self._inputs[pin] = _Input(self, pin, edge, debounce_ms, on_edge)
            return line

    def open_output(self, gpio: GpioRef, *, initial: bool = False) -> OutputLine:
        with self._lock:
            return _Output(self, self._claim(gpio), initial)

    def open_hw_pwm(self, interface, board):
        from pioneer.hal.hardware_pwm import settings
        gpio = board.pin(next(iter(interface.pins))).gpio
        line = self.open_pwm(gpio)
        def set_hw(freq=None, duty=None):
            f, d = line.freq if freq is None else freq, line.duty if duty is None else duty
            settings(f, d)
            line.freq, line.duty = f, d
            self._set_pwm(line.pin, f, d)
        line.set = set_hw
        return line

    def open_pwm(self, gpio: GpioRef, *, freq: float = 100.0, duty: float = 0.0) -> PwmLine:
        with self._lock:
            pin = self._claim(gpio)
            try:
                return _Pwm(self, pin, freq, duty)
            except ValueError:
                self._busy.discard(pin)
                raise

    def read_adc(self, channel: int) -> float:
        with self._lock:
            sources = self._adc.get(channel)
            volts = list(sources.values())[-1] if sources else 0.0
        lsb = self.board.adc_vref / (1 << self.board.adc_bits)
        raw = min(max(round(volts / lsb), 0), (1 << self.board.adc_bits) - 1)
        return raw * lsb  # как в железе: 10 бит, потолок 1023 LSB

    # ---- HAL: шины
    def open_i2c(self, bus: int) -> I2CBus:
        return _I2C(self, bus)

    def open_spi(self, bus: int, cs: int, *, mode: int = 0, speed_hz: int = 1_000_000) -> SPIDevice:
        return _SPI(self, bus, cs)

    def open_uart(self, device: str, *, baud: int = 9600) -> UARTPort:
        with self._lock:
            if device in self._uarts:
                raise LineBusy(f"{device} уже открыт")
            port = self._uarts[device] = _UART(self, device, baud)
            return port

    def open_led(self, sysfs: str, *, default_trigger: str) -> LedLine:
        if sysfs not in self._leds:
            raise OSError(f"нет LED {sysfs}")
        return _Led(self, sysfs, default_trigger)

    def led_state(self, sysfs: str) -> dict:
        st, t = self._leds[sysfs], time.monotonic()
        on = {
            "none": st["on"],
            "default-on": True,
            "timer": t % 1.0 < 0.5,
            "heartbeat": t % 1.2 < 0.07 or 0.2 <= t % 1.2 < 0.27,  # «тук-тук», как в ядре
        }.get(st["trigger"], False)
        return {"on": on, "trigger": st["trigger"], "triggers": LED_TRIGGERS}

    def close(self) -> None:
        for model in self.models.values(): model.detach()
        self._queue.put(None)

    # ---- для моделей
    def level(self, pin: str) -> bool:
        return self._levels[pin]

    def driver(self, pin: str) -> str | None:
        """Кто ведёт пин: 'core', id модели или None (подтяжка)."""
        if pin in self._core:
            return CORE
        drives = self._model_drive.get(pin)
        return next(reversed(drives)) if drives else None

    def pwm(self, pin: str) -> tuple[float, float] | None:
        return self._pwm.get(pin)

    def drive(self, model: Model, pin: str, level: bool | None) -> None:
        """Модель ведёт пин (None — отпустить)."""
        with self._lock:
            drives = self._model_drive.setdefault(pin, {})
            drives.pop(model.id, None)
            if level is not None:
                drives[model.id] = bool(level)
            self._update(pin, model.id)

    def set_voltage(self, model: Model, channel: int, volts: float | None) -> None:
        with self._lock:
            sources = self._adc.setdefault(channel, {})
            sources.pop(model.id, None)
            if volts is not None:
                sources[model.id] = float(volts)

    def attach_i2c(self, model: Model, bus: int, addr: int) -> None:
        with self._lock:
            other = self._i2c.get((bus, addr))
            if other and other is not model:
                raise BenchError(f"на i2c-{bus}@0x{addr:02x} уже модель {other.id}")
            self._i2c[(bus, addr)] = model

    def attach_spi(self, model: Model, bus: int, cs: int) -> None:
        with self._lock:
            other = self._spi.get((bus, cs))
            if other and other is not model:
                raise BenchError(f"на spi{bus}.{cs} уже модель {other.id}")
            self._spi[(bus, cs)] = model

    def uart_send(self, device: str, data: bytes) -> None:
        """Модель передаёт данные в RX порта core (если порт не открыт — теряются, как в железе)."""
        port = self._uarts.get(device)
        if port:
            port._feed(bytes(data))

    def model_changed(self, model: Model) -> None:
        for cb in self._listeners:
            cb(model.id)

    # ---- реестр моделей
    def on_change(self, cb: Callable[[str | None], None]) -> None:
        self._listeners.append(cb)

    def add(self, kind: str, model_id: str | None = None, **config) -> Model:
        from pioneer.hal.models import MODEL_KINDS
        cls = MODEL_KINDS.get(kind)
        if cls is None:
            raise BenchError(f"нет модели {kind!r}")
        with self._lock:
            model_id = model_id or f"{kind}-{uuid.uuid4().hex[:6]}"
            if model_id in self.models:
                raise BenchError(f"модель {model_id} уже есть")
            try:
                model = cls(self, model_id, **config)
            except (TypeError, ValueError, OSError) as e:
                raise BenchError(f"{kind}: {e}") from e
            try: model.attach()
            except (ValueError, OSError) as e:
                model.detach()
                raise BenchError(f"{kind}: {e}") from e
            except BaseException:
                model.detach(); raise
            self.models[model_id] = model
        self._save()
        for cb in self._listeners:
            cb(None)
        return model

    def remove(self, model_id: str) -> None:
        with self._lock:
            model = self.models.pop(model_id, None)
            if model is None:
                raise BenchError(f"нет модели {model_id}")
            model.detach()
            for pin, drives in self._model_drive.items():
                if drives.pop(model_id, None) is not None:
                    self._update(pin, model_id)
            for sources in self._adc.values():
                sources.pop(model_id, None)
            for table in (self._i2c, self._spi):
                for k in [k for k, m in table.items() if m is model]:
                    del table[k]
        self._save()
        for cb in self._listeners:
            cb(None)

    def action(self, model_id: str, name: str, payload: dict | None = None) -> dict:
        with self._lock:
            model = self.models.get(model_id)
            if model is None:
                raise BenchError(f"нет модели {model_id}")
            model.action(name, payload or {})
            state = model.state()
        self._save()
        self.model_changed(model)
        return state

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [m.describe() for m in self.models.values()]

    def dump(self) -> list[dict]:
        return [{"id": m.id, "kind": m.kind, "config": m.config} for m in self.models.values()]

    def load(self, items: list[dict]) -> None:
        for it in items:
            try:
                self.add(it["kind"], it["id"], **it.get("config", {}))
            except (BenchError, KeyError) as e:
                log.warning("стенд: пропускаю модель %s: %s", it, e)

    # ---- внутреннее
    def _claim(self, gpio: GpioRef) -> str:
        pin = self._pin_of.get(gpio)
        if pin is None:
            raise OSError(f"gpiochip{gpio.chip}:{gpio.line} не выведена на гребёнку")
        if pin in self._busy:
            raise LineBusy(f"{pin} (gpiochip{gpio.chip}:{gpio.line}) уже запрошена")
        self._busy.add(pin)
        return pin

    def _close_input(self, line: _Input) -> None:
        with self._lock:
            if self._inputs.get(line.pin) is line:
                del self._inputs[line.pin]
                self._busy.discard(line.pin)

    def _drive_core(self, pin: str, value: bool) -> None:
        with self._lock:
            self._core[pin] = value
            self._update(pin, CORE)

    def _release_core(self, pin: str) -> None:
        with self._lock:
            self._core.pop(pin, None)
            self._busy.discard(pin)
            self._update(pin, CORE)

    def _set_pwm(self, pin: str, freq: float | None, duty: float | None) -> None:
        with self._lock:
            if freq is None:
                self._pwm.pop(pin, None)
            else:
                self._pwm[pin] = (freq, duty)
                self._core[pin] = duty >= 0.5
                self._update(pin, CORE)
            for m in list(self.models.values()):
                if pin in m.watch_pins:
                    m.on_pwm(pin, self._pwm.get(pin))

    def _resolve(self, pin: str) -> bool:
        if pin in self._core:
            return self._core[pin]
        drives = self._model_drive.get(pin)
        if drives:
            return list(drives.values())[-1]
        return self.board.pins[pin].default_pull == "up"

    def _update(self, pin: str, source: str) -> None:
        level = self._resolve(pin)
        if self._levels.get(pin) == level:
            return
        self._levels[pin] = level
        if inp := self._inputs.get(pin):
            inp._changed()
        for m in list(self.models.values()):
            if pin in m.watch_pins:
                m.on_gpio(pin, level, source)

    def _uart_models(self, device: str) -> list[Model]:
        return [m for m in self.models.values() if device in m.watch_uarts]

    def _emit(self, cb: EdgeCallback, ev: EdgeEvent) -> None:
        self._queue.put((cb, ev))

    def _dispatch(self) -> None:
        while (item := self._queue.get()) is not None:
            cb, ev = item
            try:
                cb(ev)
            except Exception:
                log.exception("ошибка в обработчике edge-события")

    def _save(self) -> None:
        if self.store:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            self.store.write_text(json.dumps(self.dump(), ensure_ascii=False, indent=1))
