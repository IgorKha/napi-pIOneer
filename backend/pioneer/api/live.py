"""Live-состояние пинов по WebSocket (spec 4.2).

Изменения (edge-события из потока HAL, ручное управление, mock) помечают пин «грязным»;
раз в FLUSH_S хаб рассылает состояние грязных пинов и опрашивает ADC/soft-PWM —
так частые фронты (soft-PWM на петле) не заливают сокет: ≤ 20 сообщений/с.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import threading

from fastapi import WebSocket, WebSocketDisconnect

from pioneer.board import Board
from pioneer.pins import PinIO

log = logging.getLogger(__name__)
FLUSH_S = 0.05


class LiveHub:
    def __init__(self, board: Board, pins: PinIO):
        self.board, self.pins = board, pins
        self.clients: set[WebSocket] = set()
        self._dirty: set[str] = set()
        self._lock = threading.Lock()
        self._last: dict[str, dict] = {}
        self._task: asyncio.Task | None = None
        pins.watch(lambda pin_id, ev: self.mark(pin_id))

    def mark(self, *pin_ids: str) -> None:
        """Потокобезопасно: вызывается и из потока HAL."""
        with self._lock:
            self._dirty.update(pin_ids)

    def pin_state(self, pin_id: str) -> dict:
        claim = self.pins.res.claims.get(pin_id)
        value = self.pins.value(pin_id)
        if isinstance(value, float):
            value = round(value, 4)
        return {"value": value,
                "claim": {"mode": claim.mode, "owners": claim.owners} if claim else None}

    def snapshot(self) -> dict[str, dict]:
        return {pid: self.pin_state(pid) for pid in self.board.pins}

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)
        try:
            await ws.send_json({"type": "pins", "pins": self.snapshot()})
            while True:
                await ws.receive_text()  # фаза 0: от клиента ничего не ждём
        except WebSocketDisconnect:
            pass
        finally:
            self.clients.discard(ws)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_S)
            try:
                await self._flush()
            except Exception:
                log.exception("live flush")

    async def _flush(self) -> None:
        with self._lock:
            dirty, self._dirty = self._dirty, set()
        # Аналоговые и PWM-пины меняются без событий — опрашиваем.
        dirty |= {pid for pid, c in self.pins.res.claims.items() if c.mode in ("adc", "soft_pwm")}
        changed = {}
        for pid in dirty:
            st = self.pin_state(pid)
            if self._last.get(pid) != st:
                self._last[pid] = changed[pid] = st
        if not changed or not self.clients:
            return
        msg = {"type": "pins", "pins": changed}
        for ws in list(self.clients):
            try:
                await ws.send_json(msg)
            except Exception:
                self.clients.discard(ws)
