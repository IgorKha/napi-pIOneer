"""Встроенные модели устройств для стенда (BenchHAL).

Модель — «что подключено к пинам/шинам»: смотрит на выходы core, ведёт входы,
отвечает на шину, отдаёт state() для UI и принимает action() из UI. Позже такие же
модели будут приносить модули (docs/ideas/001-modules-and-simulation.md).

config модели = kwargs конструктора; сохраняется в bench.json и восстанавливается.
"""
from __future__ import annotations

import random
import threading
import time
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from pioneer.hal.bench import BenchHAL


class Model:
    kind: ClassVar[str]
    title: ClassVar[str]
    # Схема параметров для UI: имя -> {"type": "pin"|"adc_pin"|"int"|"float"|"bool"|"choice"|"str", ...}
    params: ClassVar[dict[str, dict[str, Any]]] = {}
    actions: ClassVar[list[str]] = []

    def __init__(self, bench: BenchHAL, model_id: str, **config):
        self.bench, self.id, self.config = bench, model_id, config
        self.watch_pins: set[str] = set()
        self.watch_uarts: set[str] = set()

    # жизненный цикл
    def attach(self) -> None: ...
    def detach(self) -> None: ...

    # события
    def on_gpio(self, pin: str, level: bool, source: str) -> None: ...
    def on_pwm(self, pin: str, pwm: tuple[float, float] | None) -> None: ...

    # шины (вызываются под локом стенда)
    def i2c_write(self, bus: int, addr: int, data: bytes) -> None: ...
    def i2c_read(self, bus: int, addr: int, n: int) -> bytes:
        return b"\xff" * n

    def spi_xfer(self, bus: int, cs: int, data: bytes) -> bytes:
        return b"\xff" * len(data)

    def uart_rx(self, device: str, data: bytes, baud: int) -> None: ...

    # UI
    def state(self) -> dict:
        return {}

    def action(self, name: str, payload: dict) -> None:
        raise ValueError(f"{self.kind}: нет действия {name!r}")

    def describe(self) -> dict:
        return {"id": self.id, "kind": self.kind, "title": self.title, "config": self.config,
                "actions": self.actions, "state": self.state()}

    # помощники
    def _gpio_pin(self, pin: str) -> str:
        p = self.bench.board.pins.get(pin)
        if p is None or p.gpio is None:
            raise TypeError(f"{pin!r} — не GPIO-пин")
        return pin

    def changed(self) -> None:
        self.bench.model_changed(self)


class Level(Model):
    """Внешний уровень на пине (бывший MockHAL.set_level)."""
    kind, title = "level", "Внешний уровень"
    params = {"pin": {"type": "pin"}, "value": {"type": "bool", "default": False}}
    actions = ["set"]

    def __init__(self, bench, model_id, *, pin: str, value: bool = False):
        super().__init__(bench, model_id, pin=pin, value=bool(value))
        self.pin = self._gpio_pin(pin)

    def attach(self):
        self.bench.drive(self, self.pin, self.config["value"])

    def state(self):
        return {"value": self.config["value"]}

    def action(self, name, payload):
        if name != "set":
            super().action(name, payload)
        self.config["value"] = bool(payload.get("value"))
        self.bench.drive(self, self.pin, self.config["value"])


class Wire(Model):
    """Перемычка между двумя пинами: уровень, который ведут на одной стороне, повторяется
    на другой (бывший MockHAL.wire; как J9-16↔J9-18 в пробе на плате)."""
    kind, title = "wire", "Перемычка"
    params = {"a": {"type": "pin"}, "b": {"type": "pin"}}

    def __init__(self, bench, model_id, *, a: str, b: str):
        super().__init__(bench, model_id, a=a, b=b)
        self.a, self.b = self._gpio_pin(a), self._gpio_pin(b)
        if a == b:
            raise TypeError("перемычка на один и тот же пин")
        self.watch_pins = {a, b}

    def attach(self):
        for src, dst in ((self.a, self.b), (self.b, self.a)):
            if self.bench.driver(src) is not None:
                self.bench.drive(self, dst, self.bench.level(src))
                return

    def on_gpio(self, pin, level, source):
        if source == self.id:
            return
        self.bench.drive(self, self.b if pin == self.a else self.a, level)

    def state(self):
        return {"level": self.bench.level(self.a)}


class Voltage(Model):
    """Источник напряжения на входе ADC (бывший MockHAL.set_adc); он же потенциометр."""
    kind, title = "voltage", "Источник напряжения"
    params = {"pin": {"type": "adc_pin"}, "volts": {"type": "float", "min": 0, "max": 3.3, "default": 0}}
    actions = ["set"]

    def __init__(self, bench, model_id, *, pin: str, volts: float = 0.0):
        super().__init__(bench, model_id, pin=pin, volts=float(volts))
        p = bench.board.pins.get(pin)
        if p is None or p.adc_channel is None:
            raise TypeError(f"{pin!r} — не вход ADC")
        self.channel = p.adc_channel

    def attach(self):
        self.bench.set_voltage(self, self.channel, self.config["volts"])

    def state(self):
        v, vref = self.config["volts"], self.bench.board.adc_vref
        return {"volts": v, "over_range": v > vref}

    def action(self, name, payload):
        if name != "set":
            super().action(name, payload)
        self.config["volts"] = float(payload.get("volts", 0))
        self.bench.set_voltage(self, self.channel, self.config["volts"])


class Button(Model):
    """Кнопка: нажата — ведёт пин в pressed_level, отпущена — не ведёт (решает подтяжка).
    bounce_ms > 0 — дребезг контактов при каждом переключении."""
    kind, title = "button", "Кнопка"
    params = {
        "pin": {"type": "pin"},
        "pressed_level": {"type": "choice", "choices": ["auto", "high", "low"], "default": "auto"},
        "bounce_ms": {"type": "float", "min": 0, "max": 50, "default": 3},
    }
    actions = ["press", "release", "click"]

    def __init__(self, bench, model_id, *, pin: str, pressed_level: str = "auto", bounce_ms: float = 3):
        super().__init__(bench, model_id, pin=pin, pressed_level=pressed_level, bounce_ms=bounce_ms)
        self.pin = self._gpio_pin(pin)
        if pressed_level == "auto":  # кнопка замыкает на противоположное подтяжке
            pressed_level = "low" if bench.board.pins[pin].default_pull == "up" else "high"
        if pressed_level not in ("high", "low"):
            raise TypeError("pressed_level: auto | high | low")
        self.active = pressed_level == "high"
        self.bounce_s = float(bounce_ms) / 1000
        self.pressed = False
        self._gen = 0  # отменяет недоигранный дребезг

    def state(self):
        floating = self.bench.board.pins[self.pin].default_pull in ("none", None)
        return {"pressed": self.pressed, "level": self.bench.level(self.pin),
                "warning": "у пина нет подтяжки — отпущенная кнопка «висит» в воздухе" if floating else None}

    def action(self, name, payload):
        if name == "press":
            self._set(True)
        elif name == "release":
            self._set(False)
        elif name == "click":
            ms = float(payload.get("ms", 120))
            self._set(True)
            threading.Timer(ms / 1000, self._set, (False,)).start()
        else:
            super().action(name, payload)

    def _set(self, pressed: bool):
        with self.bench._lock:
            self.pressed = pressed
            self._gen += 1
            gen = self._gen
        if self.bounce_s > 0:
            threading.Thread(target=self._bounce, args=(pressed, gen), daemon=True).start()
        else:
            self._apply(pressed)
        self.changed()

    def _apply(self, contact: bool):
        self.bench.drive(self, self.pin, self.active if contact else None)

    def _bounce(self, pressed: bool, gen: int):
        end = time.monotonic() + self.bounce_s
        contact = pressed
        while time.monotonic() < end and gen == self._gen:
            self._apply(contact)
            contact = not contact
            time.sleep(random.uniform(0.05, 0.3) * self.bounce_s)
        if gen == self._gen:
            self._apply(pressed)
            self.changed()


class Led(Model):
    """Светодиод на выходе: горит/яркость по уровню или скважности PWM."""
    kind, title = "led", "Светодиод"
    params = {"pin": {"type": "pin"}, "active": {"type": "choice", "choices": ["high", "low"], "default": "high"},
              "color": {"type": "str", "default": "red"}}

    def __init__(self, bench, model_id, *, pin: str, active: str = "high", color: str = "red"):
        super().__init__(bench, model_id, pin=pin, active=active, color=color)
        self.pin = self._gpio_pin(pin)
        self.active_high = active == "high"
        self.watch_pins = {pin}

    def on_gpio(self, pin, level, source):
        self.changed()

    def on_pwm(self, pin, pwm):
        self.changed()

    def state(self):
        pwm = self.bench.pwm(self.pin)
        if pwm:
            duty = pwm[1] if self.active_high else 1 - pwm[1]
            return {"on": duty > 0, "brightness": round(duty, 3), "pwm_hz": pwm[0]}
        on = self.bench.level(self.pin) == self.active_high
        return {"on": on, "brightness": 1.0 if on else 0.0}


class I2CRegs(Model):
    """Регистровое I2C-устройство (как EEPROM 24C02): запись [reg, data…] ставит указатель
    и пишет, чтение — с указателя с автоинкрементом."""
    kind, title = "i2c_regs", "I2C: регистры"
    params = {"bus": {"type": "int"}, "addr": {"type": "int"}, "size": {"type": "int", "default": 256}}
    actions = ["poke"]

    def __init__(self, bench, model_id, *, bus: int, addr: int, size: int = 256):
        super().__init__(bench, model_id, bus=bus, addr=addr, size=size)
        if not 0x03 <= addr <= 0x77:
            raise TypeError("адрес I2C 0x03…0x77")
        self.regs = bytearray(size)
        self.ptr = 0

    def attach(self):
        self.bench.attach_i2c(self, self.config["bus"], self.config["addr"])

    def i2c_write(self, bus, addr, data):
        if not data:
            return
        self.ptr = data[0] % len(self.regs)
        for b in data[1:]:
            self.regs[self.ptr] = b
            self.ptr = (self.ptr + 1) % len(self.regs)
        if len(data) > 1:
            self.changed()

    def i2c_read(self, bus, addr, n):
        out = bytearray()
        for _ in range(n):
            out.append(self.regs[self.ptr])
            self.ptr = (self.ptr + 1) % len(self.regs)
        return bytes(out)

    def state(self):
        return {"ptr": self.ptr, "head": self.regs[:16].hex(" ")}

    def action(self, name, payload):
        if name != "poke":
            super().action(name, payload)
        self.regs[int(payload["reg"]) % len(self.regs)] = int(payload["value"]) & 0xFF


class UartLoopback(Model):
    """Петля TX→RX на UART (перемычка между TX и RX)."""
    kind, title = "uart_loopback", "UART: петля TX→RX"
    params = {"device": {"type": "str"}}

    def __init__(self, bench, model_id, *, device: str):
        super().__init__(bench, model_id, device=device)
        self.watch_uarts = {device}
        self.bytes = 0

    def uart_rx(self, device, data, baud):
        self.bytes += len(data)
        self.bench.uart_send(device, data)

    def state(self):
        return {"bytes": self.bytes}


class SpiLoopback(Model):
    """Петля MOSI→MISO на SPI."""
    kind, title = "spi_loopback", "SPI: петля MOSI→MISO"
    params = {"bus": {"type": "int"}, "cs": {"type": "int", "default": 0}}

    def __init__(self, bench, model_id, *, bus: int, cs: int = 0):
        super().__init__(bench, model_id, bus=bus, cs=cs)

    def attach(self):
        self.bench.attach_spi(self, self.config["bus"], self.config["cs"])

    def spi_xfer(self, bus, cs, data):
        return data


MODEL_KINDS: dict[str, type[Model]] = {
    m.kind: m for m in (Level, Wire, Voltage, Button, Led, I2CRegs, UartLoopback, SpiLoopback)
}
