"""Live-состояние пинов по WebSocket (spec 4.2).

Изменения (edge-события из потока HAL, ручное управление, стенд) помечают пин/модель «грязными»;
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
from pioneer.hal import BenchHAL
from pioneer.hw import HW

log = logging.getLogger(__name__)
FLUSH_S = 0.05


class LiveHub:
    def __init__(self, board: Board, hw: HW):
        self.board, self.hw = board, hw
        self.clients: set[WebSocket] = set()
        self._dirty: set[str] = set()
        self._bench_dirty: set[str | None] = set()  # None — изменился состав моделей
        self._lock = threading.Lock()
        self._last: dict[str, dict] = {}
        self._task: asyncio.Task | None = None
        hw.watch(lambda pin_id, ev: self.mark(pin_id))

    def mark(self, *pin_ids: str) -> None:
        """Потокобезопасно: вызывается и из потока HAL."""
        with self._lock:
            self._dirty.update(pin_ids)

    def mark_bench(self, model_id: str | None) -> None:
        with self._lock:
            self._bench_dirty.add(model_id)

    def pin_state(self, pin_id: str) -> dict:
        claim = self.hw.res.claims.get(pin_id)
        value = self.hw.value(pin_id)
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
            bench_dirty, self._bench_dirty = self._bench_dirty, set()
        # Аналоговые и PWM-пины меняются без событий — опрашиваем.
        dirty |= {pid for pid, c in self.hw.res.claims.items() if c.mode in ("adc", "soft_pwm")}
        changed = {}
        for pid in dirty:
            st = self.pin_state(pid)
            if self._last.get(pid) != st:
                self._last[pid] = changed[pid] = st
        msgs = []
        if changed:
            msgs.append({"type": "pins", "pins": changed})
        if bench_dirty and isinstance(self.hw.hal, BenchHAL):
            models = self.hw.hal.snapshot()
            if None not in bench_dirty:
                models = [m for m in models if m["id"] in bench_dirty]
            msgs.append({"type": "bench", "full": None in bench_dirty, "models": models})
        if msgs and self.clients:
            await self._send(msgs)

    async def _send(self, msgs: list[dict]) -> None:
        for ws in list(self.clients):
            try:
                for msg in msgs:
                    await ws.send_json(msg)
            except Exception:
                self.clients.discard(ws)
