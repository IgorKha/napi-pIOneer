"""PinIO: захват пина (ResourceManager) + открытие линии в нужном режиме (HAL).

Единая точка для движка проектов и ручного управления со страницы «Плата».
Разделяемый вход — одна линия HAL, события раздаются всем подписчикам.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from pioneer.board import Board, RuntimeMode
from pioneer.hal.base import HAL, EdgeEvent, InputLine, OutputLine
from pioneer.hal.softpwm import SoftPwm
from pioneer.resources import ResourceManager

PinListener = Callable[[str, EdgeEvent], None]


@dataclass
class _Open:
    mode: RuntimeMode
    line: InputLine | OutputLine | None = None
    pwm: SoftPwm | None = None
    listeners: dict[str, PinListener] = field(default_factory=dict)


class PinIO:
    def __init__(self, board: Board, hal: HAL, resources: ResourceManager):
        self.board, self.hal, self.res = board, hal, resources
        self._open: dict[str, _Open] = {}
        self._lock = threading.RLock()
        self._watchers: list[PinListener] = []  # все изменения входов (live-уровни для UI)

    def watch(self, cb: PinListener) -> None:
        self._watchers.append(cb)

    def acquire(self, owner: str, owner_name: str, pin_id: str, mode: RuntimeMode, *,
                initial: bool = False, debounce_ms: int = 0,
                on_edge: PinListener | None = None) -> None:
        with self._lock:
            self.res.claim(owner, owner_name, pin_id, mode)
            cur = self._open.get(pin_id)
            if cur and cur.mode != mode:  # единственный владелец сменил режим
                self._close(pin_id)
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

    def release(self, owner: str, pin_id: str | None = None, *, safe_value: bool = False) -> None:
        with self._lock:
            for pid in self.res.release(owner, pin_id):
                self._close(pid, safe_value)
            for pid, o in self._open.items():
                if pin_id is None or pid == pin_id:
                    o.listeners.pop(owner, None)

    def write(self, pin_id: str, value: bool) -> None:
        o = self._require(pin_id, "gpio_out")
        o.line.write(value)  # type: ignore[union-attr]

    def set_pwm(self, pin_id: str, *, freq: float | None = None, duty: float | None = None) -> None:
        self._require(pin_id, "soft_pwm").pwm.set(freq, duty)  # type: ignore[union-attr]

    def value(self, pin_id: str) -> bool | float | None:
        o = self._open.get(pin_id)
        if o is None:
            return None
        if o.mode == "adc":
            return self.hal.read_adc(self.board.pin(pin_id).adc_channel)  # type: ignore[arg-type]
        if o.mode == "soft_pwm":
            return o.pwm.duty  # type: ignore[union-attr]
        return o.line.read()  # type: ignore[union-attr]

    def close_all(self, safe_value: bool = False) -> None:
        with self._lock:
            for pid in list(self._open):
                self._close(pid, safe_value)
            self.res.claims.clear()

    # --- внутреннее
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
            line = self.hal.open_output(pin.gpio, initial=False)
            return _Open(mode, line, SoftPwm(line))
        o = _Open(mode)
        o.line = self.hal.open_input(pin.gpio, edge="both", debounce_ms=debounce_ms,
                                     on_edge=lambda ev, pid=pin_id: self._dispatch(pid, ev))
        return o

    def _dispatch(self, pin_id: str, ev: EdgeEvent) -> None:
        o = self._open.get(pin_id)
        for cb in [*(o.listeners.values() if o else ()), *self._watchers]:
            cb(pin_id, ev)

    def _close(self, pin_id: str, safe_value: bool = False) -> None:
        o = self._open.pop(pin_id, None)
        if o is None:
            return
        if o.pwm:
            o.pwm.stop(safe_value)
        elif isinstance(o.line, OutputLine):
            o.line.write(safe_value)
        if o.line:
            o.line.close()
