"""Интерфейс HAL — единственного владельца железа в процессе core.

Реализации: LinuxHAL (gpiod / iio / smbus2 / spidev / pyserial) и BenchHAL
(виртуальный стенд с моделями устройств — Mac, тесты, dry-run на плате).

Колбэки (on_edge) вызываются из служебного потока HAL; мост в asyncio — на стороне
вызывающего (loop.call_soon_threadsafe).
"""
from __future__ import annotations

import errno
import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pioneer.board import GpioRef

EdgeMode = Literal["none", "rising", "falling", "both"]

# Ядерный дефолт — 16 событий на линию, на плате переполняется при всплесках (spec 11.1).
EVENT_BUFFER_SIZE = 1024
RT_PRIORITY = 50
# NACK на I2C: драйвер rk3x на плате отдаёт ENXIO (6), проверено 2026-10-11.
# Стенд бросает ту же ошибку, чтобы код вёл себя одинаково.
I2C_NACK_ERRNO = errno.ENXIO


@dataclass(frozen=True, slots=True)
class EdgeEvent:
    rising: bool
    timestamp_ns: int  # CLOCK_MONOTONIC
    seqno: int  # сквозной номер по линии; дырка = потерянные события


EdgeCallback = Callable[[EdgeEvent], None]


# ---------------------------------------------------------------- линии GPIO
class InputLine(ABC):
    @abstractmethod
    def read(self) -> bool: ...

    @abstractmethod
    def close(self) -> None: ...


class OutputLine(ABC):
    @abstractmethod
    def write(self, value: bool) -> None: ...

    @abstractmethod
    def read(self) -> bool:
        """Последнее выставленное значение."""

    @abstractmethod
    def close(self) -> None: ...


class PwmLine(ABC):
    freq: float
    duty: float  # 0…1

    @abstractmethod
    def set(self, freq: float | None = None, duty: float | None = None) -> None: ...

    @abstractmethod
    def close(self, safe_value: bool = False) -> None:
        """Остановить и оставить линию в safe_value."""


# ---------------------------------------------------------------- шины
class I2CBus(ABC):
    """Шина целиком; адрес — на операцию. Нет устройства (NACK) — OSError(I2C_NACK_ERRNO)."""

    @abstractmethod
    def write(self, addr: int, data: bytes) -> None: ...

    @abstractmethod
    def read(self, addr: int, n: int) -> bytes: ...

    @abstractmethod
    def write_read(self, addr: int, data: bytes, n: int) -> bytes:
        """Запись + чтение одной транзакцией (repeated start) — чтение регистров."""

    @abstractmethod
    def close(self) -> None: ...


class SPIDevice(ABC):
    @abstractmethod
    def xfer(self, data: bytes) -> bytes:
        """Полнодуплексный обмен при опущенном CS; длина ответа = длине data."""

    @abstractmethod
    def close(self) -> None: ...


class UARTPort(ABC):
    baud: int

    @abstractmethod
    def write(self, data: bytes) -> None: ...

    @abstractmethod
    def read(self, max_bytes: int = 4096, timeout: float = 0.0) -> bytes:
        """Что пришло (≤ max_bytes), ждать не дольше timeout секунд."""

    @abstractmethod
    def set_baud(self, baud: int) -> None: ...

    @abstractmethod
    def close(self) -> None: ...


# ---------------------------------------------------------------- HAL
class HAL(ABC):
    name: str

    @abstractmethod
    def open_input(self, gpio: GpioRef, *, edge: EdgeMode = "none", debounce_ms: int = 0,
                   on_edge: EdgeCallback | None = None) -> InputLine: ...

    @abstractmethod
    def open_output(self, gpio: GpioRef, *, initial: bool = False) -> OutputLine: ...

    def open_pwm(self, gpio: GpioRef, *, freq: float = 100.0, duty: float = 0.0) -> PwmLine:
        """По умолчанию — soft-PWM поверх выхода. Аппаратный PWM (pwmchip) — фаза 3."""
        from pioneer.hal.softpwm import SoftPwm
        return SoftPwm(self.open_output(gpio), freq, duty)

    @abstractmethod
    def read_adc(self, channel: int) -> float:
        """Напряжение на входе ADC, вольты (0…vref)."""

    @abstractmethod
    def open_i2c(self, bus: int) -> I2CBus: ...

    @abstractmethod
    def open_spi(self, bus: int, cs: int, *, mode: int = 0, speed_hz: int = 1_000_000) -> SPIDevice: ...

    @abstractmethod
    def open_uart(self, device: str, *, baud: int = 9600) -> UARTPort: ...

    @abstractmethod
    def close(self) -> None: ...


def try_realtime(priority: int = RT_PRIORITY) -> bool:
    """SCHED_FIFO для текущего потока. На Mac/без прав — молча False."""
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(priority))  # type: ignore[attr-defined]
        return True
    except (AttributeError, PermissionError, OSError):
        return False
