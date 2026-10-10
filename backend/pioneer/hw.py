"""HW: захват ресурса (ResourceManager) + открытие его в HAL.

Единая точка доступа к железу для движка проектов, скриптов и ручного управления.
Разделяемый вход — одна линия HAL, события раздаются всем подписчикам.
I2C-шина открывается одна на core; владелец получает I2CDevice, привязанный к адресу.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from pioneer.board import Board, RuntimeMode
from pioneer.hal.base import HAL, EdgeEvent, I2CBus, InputLine, OutputLine, PwmLine, SPIDevice, UARTPort
from pioneer.resources import ResourceManager, i2c_id, spi_id, uart_id

PinListener = Callable[[str, EdgeEvent], None]


@dataclass
class _Open:
    mode: RuntimeMode
    line: InputLine | OutputLine | PwmLine | None = None
    listeners: dict[str, PinListener] = field(default_factory=dict)


class I2CDevice:
    """Устройство по одному адресу на общей шине."""

    def __init__(self, bus: I2CBus, bus_no: int, addr: int):
        self.bus, self.bus_no, self.addr = bus, bus_no, addr

    def write(self, data: bytes) -> None:
        self.bus.write(self.addr, data)

    def read(self, n: int) -> bytes:
        return self.bus.read(self.addr, n)

    def write_read(self, data: bytes, n: int) -> bytes:
        return self.bus.write_read(self.addr, data, n)


class HW:
    def __init__(self, board: Board, hal: HAL, resources: ResourceManager):
        self.board, self.hal, self.res = board, hal, resources
        self._open: dict[str, _Open] = {}
        self._handles: dict[str, SPIDevice | UARTPort] = {}  # эксклюзивные открытые ресурсы шин
        self._lock = threading.RLock()
        self._watchers: list[PinListener] = []  # все изменения входов (live-уровни для UI)

    def watch(self, cb: PinListener) -> None:
        self._watchers.append(cb)

    # ---- пины
    def acquire(self, owner: str, owner_name: str, pin_id: str, mode: RuntimeMode, *,
                initial: bool = False, debounce_ms: int = 0,
                on_edge: PinListener | None = None) -> None:
        with self._lock:
            self.res.claim(owner, owner_name, pin_id, mode)
            cur = self._open.get(pin_id)
            if cur and cur.mode != mode:  # единственный владелец сменил режим
                self._close_pin(pin_id)
                cur = None
            if cur is None:
                # Разделяемый вход открывается с debounce первого захватившего.
                try:
                    cur = self._open[pin_id] = self._open_line(pin_id, mode, initial, debounce_ms)
                except Exception:
                    self.res.release(owner, pin_id)
                    raise
            if on_edge:
                cur.listeners[owner] = on_edge

    def write(self, pin_id: str, value: bool) -> None:
        self._require(pin_id, "gpio_out").line.write(value)  # type: ignore[union-attr]

    def set_pwm(self, pin_id: str, *, freq: float | None = None, duty: float | None = None) -> None:
        self._require(pin_id, "soft_pwm").line.set(freq, duty)  # type: ignore[union-attr]

    def value(self, pin_id: str) -> bool | float | None:
        o = self._open.get(pin_id)
        if o is None:
            return None
        if o.mode == "adc":
            return self.hal.read_adc(self.board.pin(pin_id).adc_channel)  # type: ignore[arg-type]
        if o.mode == "soft_pwm":
            return o.line.duty  # type: ignore[union-attr]
        return o.line.read()  # type: ignore[union-attr]

    # ---- шины
    def i2c(self, owner: str, owner_name: str, bus: int, addr: int) -> I2CDevice:
        with self._lock:
            self.res.claim(owner, owner_name, i2c_id(bus, addr), "i2c")
            try:
                return I2CDevice(self.hal.open_i2c(bus), bus, addr)
            except Exception:
                self.res.release(owner, i2c_id(bus, addr))
                raise

    def spi(self, owner: str, owner_name: str, bus: int, cs: int, *, mode: int = 0,
            speed_hz: int = 1_000_000) -> SPIDevice:
        return self._exclusive(owner, owner_name, spi_id(bus, cs), "spi",
                               lambda: self.hal.open_spi(bus, cs, mode=mode, speed_hz=speed_hz))

    def uart(self, owner: str, owner_name: str, device: str, *, baud: int = 9600) -> UARTPort:
        device = device if device.startswith("/dev/") else f"/dev/{device}"
        return self._exclusive(owner, owner_name, uart_id(device), "uart",
                               lambda: self.hal.open_uart(device, baud=baud))

    def _exclusive(self, owner, owner_name, rid, mode, opener):
        with self._lock:
            self.res.claim(owner, owner_name, rid, mode)
            if rid not in self._handles:
                try:
                    self._handles[rid] = opener()
                except Exception:
                    self.res.release(owner, rid)
                    raise
            return self._handles[rid]

    # ---- освобождение
    def release(self, owner: str, resource: str | None = None, *, safe_value: bool = False) -> None:
        with self._lock:
            for rid in self.res.release(owner, resource):
                self._close(rid, safe_value)
            for rid, o in self._open.items():
                if resource is None or rid == resource:
                    o.listeners.pop(owner, None)

    def close_all(self, safe_value: bool = False) -> None:
        with self._lock:
            for rid in list(self.res.claims):
                self._close(rid, safe_value)
            self.res.claims.clear()

    # ---- внутреннее
    def _require(self, pin_id: str, mode: RuntimeMode) -> _Open:
        o = self._open.get(pin_id)
        if o is None or o.mode != mode:
            raise RuntimeError(f"{pin_id} не открыт в режиме {mode}")
        return o

    def _open_line(self, pin_id: str, mode: RuntimeMode, initial: bool, debounce_ms: int) -> _Open:
        pin = self.board.pin(pin_id)
        if mode == "adc":
            return _Open(mode)
        assert pin.gpio
        if mode == "gpio_out":
            return _Open(mode, self.hal.open_output(pin.gpio, initial=initial))
        if mode == "soft_pwm":
            return _Open(mode, self.hal.open_pwm(pin.gpio))
        return _Open(mode, self.hal.open_input(
            pin.gpio, edge="both", debounce_ms=debounce_ms,
            on_edge=lambda ev, pid=pin_id: self._dispatch(pid, ev)))

    def _dispatch(self, pin_id: str, ev: EdgeEvent) -> None:
        o = self._open.get(pin_id)
        for cb in [*(o.listeners.values() if o else ()), *self._watchers]:
            cb(pin_id, ev)

    def _close(self, rid: str, safe_value: bool) -> None:
        if rid in self._open:
            self._close_pin(rid, safe_value)
        elif h := self._handles.pop(rid, None):
            h.close()
        # i2c: шина общая и остаётся открытой; захват адреса уже снят

    def _close_pin(self, pin_id: str, safe_value: bool = False) -> None:
        o = self._open.pop(pin_id, None)
        if o is None or o.line is None:
            return
        if isinstance(o.line, PwmLine):
            o.line.close(safe_value)
            return
        if isinstance(o.line, OutputLine):
            o.line.write(safe_value)
        o.line.close()
