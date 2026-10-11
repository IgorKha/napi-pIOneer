"""Soft-PWM поверх любого OutputLine: отдельный поток, SCHED_FIFO если возможно.

Замеры на плате (spec 11.1): без RT под нагрузкой джиттер до 20 мс, с RT — p95 < 0.1 мс
до 500 Гц. Выше MAX_FREQ не пускаем.
"""
from __future__ import annotations

import threading
import time

from pioneer.hal.base import OutputLine, PwmLine, try_realtime

MAX_FREQ = 500.0


def check_pwm(freq: float | None, duty: float | None) -> None:
    if freq is not None and not 0 < freq <= MAX_FREQ:
        raise ValueError(f"частота soft-PWM 0…{MAX_FREQ:g} Гц")
    if duty is not None and not 0.0 <= duty <= 1.0:
        raise ValueError("скважность 0…1")


class SoftPwm(PwmLine):
    def __init__(self, line: OutputLine, freq: float = 100.0, duty: float = 0.0):
        self.line = line
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.freq, self.duty = 100.0, 0.0
        self.set(freq, duty)
        self.realtime = False
        self._thread = threading.Thread(target=self._run, name="pioneer-softpwm", daemon=True)
        self._thread.start()

    def set(self, freq: float | None = None, duty: float | None = None) -> None:
        check_pwm(freq, duty)
        with self._lock:
            if freq is not None:
                self.freq = float(freq)
            if duty is not None:
                self.duty = float(duty)

    def close(self, safe_value: bool = False) -> None:
        self._stop.set()
        self._thread.join()
        self.line.write(safe_value)
        self.line.close()

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
                if self._stop.wait(max(0.0, t + period * duty - time.monotonic())):
                    break
                self.line.write(False)
                t += period
            delay = t - time.monotonic()
            if delay < -period:  # сильно отстали (стоп/SIGSTOP) — не догоняем пачкой
                t = time.monotonic()
            else:
                self._stop.wait(max(0.0, delay))
