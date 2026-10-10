from __future__ import annotations

from pathlib import Path
from typing import Literal

from pioneer.board import Board
from pioneer.hal.base import HAL, EdgeEvent, I2CBus, InputLine, OutputLine, PwmLine, SPIDevice, UARTPort
from pioneer.hal.bench import BenchHAL

__all__ = ["HAL", "BenchHAL", "EdgeEvent", "I2CBus", "InputLine", "OutputLine", "PwmLine",
           "SPIDevice", "UARTPort", "create_hal"]

HalKind = Literal["auto", "bench", "linux"]


def create_hal(kind: HalKind, board: Board, *, bench_store: Path | None = None) -> HAL:
    """auto: linux, если есть gpiod (плата), иначе стенд. bench на плате — режим «всухую»."""
    if kind == "auto":
        try:
            import gpiod  # noqa: F401
            kind = "linux"
        except ImportError:
            kind = "bench"
    if kind == "linux":
        from pioneer.hal.linux import LinuxHAL
        return LinuxHAL(board.adc_device)
    return BenchHAL(board, store=bench_store)
