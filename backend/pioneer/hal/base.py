"""Интерфейс HAL — единственного владельца железа в процессе core.

Колбэки on_edge вызываются из служебного потока HAL; мост в asyncio — на стороне
вызывающего (loop.call_soon_threadsafe).
"""
from __future__ import annotations

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


@dataclass(frozen=True, slots=True)
class EdgeEvent:
    rising: bool
    timestamp_ns: int  # CLOCK_MONOTONIC
    seqno: int  # сквозной номер по линии; дырка = потерянные события


EdgeCallback = Callable[[EdgeEvent], None]


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


class HAL(ABC):
    name: str

    @abstractmethod
    def open_input(self, gpio: GpioRef, *, edge: EdgeMode = "none", debounce_ms: int = 0,
                   on_edge: EdgeCallback | None = None) -> InputLine: ...

    @abstractmethod
    def open_output(self, gpio: GpioRef, *, initial: bool = False) -> OutputLine: ...

    @abstractmethod
    def read_adc(self, channel: int) -> float:
        """Напряжение на входе ADC, вольты (0…vref)."""

    @abstractmethod
    def close(self) -> None: ...


def try_realtime(priority: int = RT_PRIORITY) -> bool:
    """SCHED_FIFO для текущего потока. На Mac/без прав — молча False."""
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(priority))  # type: ignore[attr-defined]
        return True
    except (AttributeError, PermissionError, OSError):
        return False
