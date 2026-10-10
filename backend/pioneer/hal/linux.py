"""HAL на реальном железе Napi-C (Linux):

- GPIO — libgpiod v2 (python gpiod 2.1.x). Edge-события читает один поток (SCHED_FIFO,
  если можно) через selectors по fd всех запросов; буфер — EVENT_BUFFER_SIZE (spec 11.1);
- ADC — iio sysfs; I2C — smbus2 (i2c_rdwr); SPI — spidev; UART — pyserial.

Все библиотеки стоят на плате; импортируются лениво, чтобы модуль не нужен был на Mac.
"""
from __future__ import annotations

import logging
import os
import selectors
import threading
from datetime import timedelta
from pathlib import Path

from pioneer.board import GpioRef
from pioneer.hal.base import (
    EVENT_BUFFER_SIZE, HAL, EdgeCallback, EdgeEvent, EdgeMode, I2CBus, InputLine, OutputLine,
    SPIDevice, UARTPort, try_realtime,
)

log = logging.getLogger(__name__)
CONSUMER = "pioneer"


# ---------------------------------------------------------------- GPIO
class _Input(InputLine):
    def __init__(self, hal: LinuxHAL, req, line: int):
        self.hal, self.req, self.line = hal, req, line

    def read(self) -> bool:
        from gpiod.line import Value
        return self.req.get_value(self.line) == Value.ACTIVE

    def close(self) -> None:
        self.hal._reader.remove(self.req)
        self.req.release()


class _Output(OutputLine):
    def __init__(self, req, line: int, initial: bool):
        self.req, self.line, self.value = req, line, initial

    def write(self, value: bool) -> None:
        from gpiod.line import Value
        self.value = bool(value)
        self.req.set_value(self.line, Value.ACTIVE if value else Value.INACTIVE)

    def read(self) -> bool:
        return self.value

    def close(self) -> None:
        self.req.release()


class _EventReader:
    def __init__(self):
        self.sel = selectors.DefaultSelector()
        self._wake_r, self._wake_w = os.pipe()
        self.sel.register(self._wake_r, selectors.EVENT_READ)
        self._lock = threading.Lock()
        self._stop = False
        self.thread = threading.Thread(target=self._run, name="pioneer-gpio-events", daemon=True)
        self.thread.start()

    def add(self, req, cb: EdgeCallback) -> None:
        with self._lock:
            self.sel.register(req.fd, selectors.EVENT_READ, (req, cb))
        os.write(self._wake_w, b"x")

    def remove(self, req) -> None:
        with self._lock:
            try:
                self.sel.unregister(req.fd)
            except (KeyError, ValueError):
                pass
        os.write(self._wake_w, b"x")

    def stop(self) -> None:
        self._stop = True
        os.write(self._wake_w, b"x")
        self.thread.join(timeout=1)

    def _run(self) -> None:
        if not try_realtime():
            log.warning("edge-reader: SCHED_FIFO недоступен, латентность под нагрузкой хуже")
        while not self._stop:
            for key, _ in self.sel.select():
                if key.fd == self._wake_r:
                    os.read(self._wake_r, 64)
                    continue
                req, cb = key.data
                try:
                    events = req.read_edge_events()
                except OSError:
                    continue  # запрос закрыли между select и read
                for ev in events:
                    try:
                        cb(EdgeEvent(rising=ev.event_type == ev.Type.RISING_EDGE,
                                     timestamp_ns=ev.timestamp_ns, seqno=ev.line_seqno))
                    except Exception:
                        log.exception("ошибка в обработчике edge-события")


# ---------------------------------------------------------------- шины
class _I2C(I2CBus):
    def __init__(self, bus: int):
        from smbus2 import SMBus
        self.smbus = SMBus(bus)
        self._lock = threading.Lock()  # транзакции разных владельцев не перемешиваются

    def _rdwr(self, *msgs) -> None:
        with self._lock:
            self.smbus.i2c_rdwr(*msgs)

    def write(self, addr: int, data: bytes) -> None:
        from smbus2 import i2c_msg
        self._rdwr(i2c_msg.write(addr, bytes(data)))

    def read(self, addr: int, n: int) -> bytes:
        from smbus2 import i2c_msg
        msg = i2c_msg.read(addr, n)
        self._rdwr(msg)
        return bytes(msg)

    def write_read(self, addr: int, data: bytes, n: int) -> bytes:
        from smbus2 import i2c_msg
        rd = i2c_msg.read(addr, n)
        self._rdwr(i2c_msg.write(addr, bytes(data)), rd)
        return bytes(rd)

    def close(self) -> None:
        self.smbus.close()


class _SPI(SPIDevice):
    def __init__(self, bus: int, cs: int, mode: int, speed_hz: int):
        import spidev
        self.dev = spidev.SpiDev()
        self.dev.open(bus, cs)
        self.dev.mode = mode
        self.dev.max_speed_hz = speed_hz

    def xfer(self, data: bytes) -> bytes:
        return bytes(self.dev.xfer2(list(data)))

    def close(self) -> None:
        self.dev.close()


class _UART(UARTPort):
    def __init__(self, device: str, baud: int):
        import serial
        self.port = serial.Serial(device, baud, timeout=0)
        self.baud = baud

    def write(self, data: bytes) -> None:
        self.port.write(data)

    def read(self, max_bytes: int = 4096, timeout: float = 0.0) -> bytes:
        self.port.timeout = timeout
        first = self.port.read(1)  # ждём не дольше timeout хотя бы байт
        if not first:
            return b""
        self.port.timeout = 0
        return first + self.port.read(max_bytes - 1)

    def set_baud(self, baud: int) -> None:
        self.port.baudrate = self.baud = baud

    def close(self) -> None:
        self.port.close()


# ---------------------------------------------------------------- HAL
class LinuxHAL(HAL):
    name = "linux"

    def __init__(self, adc_device: str = "iio:device0"):
        self.adc_dir = Path("/sys/bus/iio/devices") / adc_device
        self.adc_scale_v = float((self.adc_dir / "in_voltage_scale").read_text()) / 1000
        self._reader = _EventReader()
        self._i2c: dict[int, _I2C] = {}

    def open_input(self, gpio: GpioRef, *, edge: EdgeMode = "none", debounce_ms: int = 0,
                   on_edge: EdgeCallback | None = None) -> InputLine:
        import gpiod
        from gpiod.line import Direction, Edge
        edges = {"none": Edge.NONE, "rising": Edge.RISING, "falling": Edge.FALLING, "both": Edge.BOTH}
        settings = gpiod.LineSettings(direction=Direction.INPUT, edge_detection=edges[edge],
                                      debounce_period=timedelta(milliseconds=debounce_ms))
        req = gpiod.request_lines(gpio.path, consumer=CONSUMER, config={gpio.line: settings},
                                  event_buffer_size=EVENT_BUFFER_SIZE)
        if on_edge and edge != "none":
            self._reader.add(req, on_edge)
        return _Input(self, req, gpio.line)

    def open_output(self, gpio: GpioRef, *, initial: bool = False) -> OutputLine:
        import gpiod
        from gpiod.line import Direction, Value
        req = gpiod.request_lines(gpio.path, consumer=CONSUMER, config={gpio.line: gpiod.LineSettings(
            direction=Direction.OUTPUT, output_value=Value.ACTIVE if initial else Value.INACTIVE)})
        return _Output(req, gpio.line, initial)

    def read_adc(self, channel: int) -> float:
        return int((self.adc_dir / f"in_voltage{channel}_raw").read_text()) * self.adc_scale_v

    def open_i2c(self, bus: int) -> I2CBus:
        # Одна шина — один fd на весь core; владельцы адресов делят его (захват — в ResourceManager).
        if bus not in self._i2c:
            self._i2c[bus] = _I2C(bus)
        return self._i2c[bus]

    def open_spi(self, bus: int, cs: int, *, mode: int = 0, speed_hz: int = 1_000_000) -> SPIDevice:
        return _SPI(bus, cs, mode, speed_hz)

    def open_uart(self, device: str, *, baud: int = 9600) -> UARTPort:
        return _UART(device, baud)

    def close(self) -> None:
        self._reader.stop()
        for bus in self._i2c.values():
            bus.close()
