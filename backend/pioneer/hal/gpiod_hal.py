"""HAL поверх libgpiod v2 (python gpiod 2.1.x на плате) + ADC через iio sysfs.

Edge-события читает один поток (SCHED_FIFO, если можно) через selectors по fd
всех запросов. Буфер событий — EVENT_BUFFER_SIZE (дефолтные 16 теряются, spec 11.1).
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
    EVENT_BUFFER_SIZE, HAL, EdgeCallback, EdgeEvent, EdgeMode, InputLine, OutputLine, try_realtime,
)

import gpiod  # noqa: E402  — только на Linux, импорт модуля решает pioneer.hal.create_hal
from gpiod.line import Direction, Edge, Value  # noqa: E402

log = logging.getLogger(__name__)
CONSUMER = "pioneer"
_EDGE = {"none": Edge.NONE, "rising": Edge.RISING, "falling": Edge.FALLING, "both": Edge.BOTH}


class _Input(InputLine):
    def __init__(self, hal: GpiodHAL, req, line: int):
        self.hal, self.req, self.line = hal, req, line

    def read(self) -> bool:
        return self.req.get_value(self.line) == Value.ACTIVE

    def close(self) -> None:
        self.hal._reader.remove(self.req)
        self.req.release()


class _Output(OutputLine):
    def __init__(self, req, line: int, initial: bool):
        self.req, self.line, self.value = req, line, initial

    def write(self, value: bool) -> None:
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


class GpiodHAL(HAL):
    name = "gpiod"

    def __init__(self, adc_device: str = "iio:device0"):
        self.adc_dir = Path("/sys/bus/iio/devices") / adc_device
        scale_mv = float((self.adc_dir / "in_voltage_scale").read_text())
        self.adc_scale_v = scale_mv / 1000
        self._reader = _EventReader()

    def open_input(self, gpio: GpioRef, *, edge: EdgeMode = "none", debounce_ms: int = 0,
                   on_edge: EdgeCallback | None = None) -> InputLine:
        settings = gpiod.LineSettings(direction=Direction.INPUT, edge_detection=_EDGE[edge],
                                      debounce_period=timedelta(milliseconds=debounce_ms))
        req = gpiod.request_lines(gpio.path, consumer=CONSUMER, config={gpio.line: settings},
                                  event_buffer_size=EVENT_BUFFER_SIZE)
        if on_edge and edge != "none":
            self._reader.add(req, on_edge)
        return _Input(self, req, gpio.line)

    def open_output(self, gpio: GpioRef, *, initial: bool = False) -> OutputLine:
        value = Value.ACTIVE if initial else Value.INACTIVE
        req = gpiod.request_lines(gpio.path, consumer=CONSUMER, config={gpio.line: gpiod.LineSettings(
            direction=Direction.OUTPUT, output_value=value)})
        return _Output(req, gpio.line, initial)

    def read_adc(self, channel: int) -> float:
        raw = int((self.adc_dir / f"in_voltage{channel}_raw").read_text())
        return raw * self.adc_scale_v

    def close(self) -> None:
        self._reader.stop()
