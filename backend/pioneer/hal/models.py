"""Встроенные модели устройств для стенда (BenchHAL).

Модель — «что подключено к пинам/шинам»: смотрит на выходы core, ведёт входы,
отвечает на шину, отдаёт state() для UI и принимает action() из UI. Позже такие же
модели будут приносить модули (docs/ideas/001-modules-and-simulation.md).

config модели = kwargs конструктора; сохраняется в bench.json и восстанавливается.
"""
from __future__ import annotations

import random
import math
import struct
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

BUS_PARAMS = {"bus": {"type": "int", "default": 1}, "addr": {"type": "int"}}


class Registers(Model):
    def __init__(self, bench, model_id, *, bus=1, addr=0x76, **config):
        super().__init__(bench, model_id, bus=bus, addr=addr, **config)
        self.regs, self.ptr = bytearray(256), 0

    def attach(self):
        self.bench.attach_i2c(self, self.config["bus"], self.config["addr"])

    def i2c_write(self, bus, addr, data):
        if data:
            self.ptr = data[0]
            for b in data[1:]:
                self.regs[self.ptr] = b
                self.ptr = (self.ptr + 1) % 256
            self.changed()

    def i2c_read(self, bus, addr, n):
        data = bytes(self.regs[(self.ptr + i) % 256] for i in range(n))
        self.ptr = (self.ptr + n) % 256
        return data


class BME280(Registers):
    kind, title = "bme280", "BME280 (Adafruit)"
    params = {
        **BUS_PARAMS,
        "addr": {"type": "int", "default": 0x76},
        "temperature": {"type": "float", "default": 25},
        "humidity": {"type": "float", "default": 50},
        "pressure": {"type": "float", "default": 1013.25},
    }
    actions = ["set"]

    def __init__(
        self, bench, model_id, *, bus=1, addr=0x76, temperature=25, humidity=50, pressure=1013.25
    ):
        super().__init__(
            bench,
            model_id,
            bus=bus,
            addr=addr,
            temperature=temperature,
            humidity=humidity,
            pressure=pressure,
        )
        self.regs[0xD0] = 0x60
        self.regs[0x88:0xA0] = struct.pack("<HhhHhhhhhhhh", 20000, 25600, 0, 32768, *([0] * 8))
        self.regs[0xE1:0xE8] = struct.pack("<hBbBbb", 1024, 0, 0, 0, 0, 0)
        self.action("set", {"temperature": temperature, "humidity": humidity, "pressure": pressure})

    def action(self, name, payload):
        if name != "set":
            return super().action(name, payload)
        values = dict(self.config)
        for key, lower, upper in (
            ("temperature", -40, 85),
            ("humidity", 0, 100),
            ("pressure", 300, 1100),
        ):
            value = float(payload.get(key, values[key]))
            if not math.isfinite(value) or not lower <= value <= upper:
                raise ValueError(f"{key}: {lower}…{upper}")
            values[key] = value
        self.config.update(values)
        t = round(320000 + values["temperature"] * 3276.8)
        p = round(1048576 - values["pressure"] * 524.288)
        self.regs[0xF7:0xFA] = (p << 4).to_bytes(3, "big")
        self.regs[0xFA:0xFD] = (t << 4).to_bytes(3, "big")
        self.regs[0xFD:0xFF] = round(values["humidity"] * 64).to_bytes(2, "big")
        self.changed()

    def state(self):
        return {k: self.config[k] for k in ("temperature", "humidity", "pressure")}


class SHT4x(Model):
    kind, title = "sht4x", "SHT4x (Adafruit)"
    params = {
        **BUS_PARAMS,
        "addr": {"type": "int", "default": 0x44},
        "temperature": {"type": "float", "default": 25},
        "humidity": {"type": "float", "default": 50},
    }
    actions = ["set"]

    def __init__(self, bench, model_id, *, bus=1, addr=0x44, temperature=25, humidity=50):
        super().__init__(
            bench, model_id, bus=bus, addr=addr, temperature=temperature, humidity=humidity
        )
        self.command = 0xFD
        self.action("set", {"temperature": temperature, "humidity": humidity})

    def attach(self):
        self.bench.attach_i2c(self, self.config["bus"], self.config["addr"])

    def i2c_write(self, bus, addr, data):
        if data:
            self.command = data[0]

    @staticmethod
    def crc(data):
        crc = 0xFF
        for value in data:
            crc ^= value
            for _ in range(8):
                crc = ((crc << 1) ^ (0x31 if crc & 0x80 else 0)) & 0xFF
        return crc

    def i2c_read(self, bus, addr, n):
        if self.command == 0x89:
            values = (0x1234, 0x5678)
        else:
            values = (
                round((self.config["temperature"] + 45) * 65535 / 175),
                round((self.config["humidity"] + 6) * 65535 / 125),
            )
        result = b""
        for value in values:
            pair = value.to_bytes(2, "big")
            result += pair + bytes([self.crc(pair)])
        return (result + bytes(max(0, n - 6)))[:n]

    def action(self, name, payload):
        if name != "set":
            return super().action(name, payload)
        for key, low, high in (("temperature", -40, 125), ("humidity", 0, 100)):
            value = float(payload.get(key, self.config[key]))
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{key}: {low}…{high}")
        self.config.update(
            {k: float(v) for k, v in payload.items() if k in ("temperature", "humidity")}
        )
        self.changed()

    def state(self):
        return {k: self.config[k] for k in ("temperature", "humidity")}


class PCA9685(Registers):
    kind, title = "pca9685", "PCA9685 (Adafruit)"
    params = {**BUS_PARAMS, "addr": {"type": "int", "default": 0x40}}

    def __init__(self, bench, model_id, *, bus=1, addr=0x40):
        super().__init__(bench, model_id, bus=bus, addr=addr)
        self.regs[0xFE] = 0x1E

    def state(self):
        channels = []
        for channel in range(16):
            on, off = struct.unpack_from("<HH", self.regs, 6 + channel * 4)
            duty = 0 if off & 4096 else 65535 if on & 4096 else ((off - on) & 4095) << 4
            channels.append(duty)
        return {"frequency": 25000000 / (4096 * (self.regs[0xFE] + 1)), "channels": channels}


class SSD1306(Model):
    kind, title = "ssd1306", "SSD1306 OLED (Adafruit)"
    params = {
        **BUS_PARAMS,
        "addr": {"type": "int", "default": 0x3C},
        "width": {"type": "int", "default": 128},
        "height": {"type": "int", "default": 64},
    }

    def __init__(self, bench, model_id, *, bus=1, addr=0x3C, width=128, height=64):
        if width not in (64, 128) or height not in (32, 64):
            raise ValueError("SSD1306 width 64/128, height 32/64")
        super().__init__(bench, model_id, bus=bus, addr=addr, width=width, height=height)
        self.buffer = bytearray(width * height // 8)
        self.commands = []
        self.pending = []
        self.columns, self.pages, self.position = (0, width - 1), (0, height // 8 - 1), 0

    def attach(self):
        self.bench.attach_i2c(self, self.config["bus"], self.config["addr"])

    def i2c_write(self, bus, addr, data):
        if not data:
            return
        if data[0] & 0x40:
            width = self.config["width"]
            span = self.columns[1] - self.columns[0] + 1
            for value in data[1:]:
                page = self.pages[0] + self.position // span
                col = self.columns[0] + self.position % span
                index = page * width + col
                if 0 <= index < len(self.buffer):
                    self.buffer[index] = value
                self.position = (self.position + 1) % (span * (self.pages[1] - self.pages[0] + 1))
            self.changed()
        else:
            for value in data[1:]:
                if self.pending:
                    self.pending.append(value)
                    needed = 3 if self.pending[0] in (0x21, 0x22) else 2
                    if len(self.pending) == needed:
                        if self.pending[0] == 0x21:
                            self.columns = tuple(self.pending[1:])
                            self.position = 0
                        if self.pending[0] == 0x22:
                            self.pages = tuple(self.pending[1:])
                            self.position = 0
                        self.pending = []
                elif value in (0x21, 0x22, 0x20, 0x81, 0x8D, 0xA8, 0xD3, 0xD5, 0xD9, 0xDA, 0xDB):
                    self.pending = [value]
            self.commands = (self.commands + list(data[1:]))[-64:]

    def i2c_read(self, bus, addr, n):
        return bytes(n)

    def state(self):
        return {
            "width": self.config["width"],
            "height": self.config["height"],
            "frame": self.buffer.hex(),
            "lit_pixels": sum(v.bit_count() for v in self.buffer),
        }


MODEL_KINDS.update({m.kind: m for m in (BME280, SHT4x, SSD1306, PCA9685)})
