"""Soft-PWM поверх любого OutputLine: отдельный поток, SCHED_FIFO если возможно.

Замеры на плате (spec 11.1): без RT под нагрузкой джиттер до 20 мс, с RT — p95 < 0.1 мс
до 500 Гц. Выше MAX_FREQ не пускаем.
"""
from __future__ import annotations

import threading
import time

from pioneer.hal.base import OutputLine, try_realtime

MAX_FREQ = 500.0


class SoftPwm:
    def __init__(self, line: OutputLine, freq: float = 100.0, duty: float = 0.0):
        self.line = line
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.set(freq, duty)
        self.realtime = False
        self._thread = threading.Thread(target=self._run, name="pioneer-softpwm", daemon=True)
        self._thread.start()

    def set(self, freq: float | None = None, duty: float | None = None) -> None:
        with self._lock:
            if freq is not None:
                if not 0 < freq <= MAX_FREQ:
                    raise ValueError(f"частота soft-PWM 0…{MAX_FREQ:g} Гц")
                self.freq = float(freq)
            if duty is not None:
                self.duty = min(max(float(duty), 0.0), 1.0)

    def stop(self, safe_value: bool = False) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        self.line.write(safe_value)

    def _run(self) -> None:
        self.realtime = try_realtime()
        t = time.monotonic()
        while not self._stop.is_set():
            with self._lock:
                period, duty = 1.0 / self.freq, self.duty
            if duty <= 0 or duty >= 1:  # постоянный уровень — не дёргаем линию зря
                self.line.write(duty >= 1)
                t += period
            else:
                self.line.write(True)
                time.sleep(max(0.0, t + period * duty - time.monotonic()))
                self.line.write(False)
                t += period
            delay = t - time.monotonic()
            if delay < -period:  # сильно отстали (стоп/SIGSTOP) — не догоняем пачкой
                t = time.monotonic()
            else:
                self._stop.wait(max(0.0, delay))
