from __future__ import annotations

from typing import Literal

from pioneer.hal.base import HAL, EdgeEvent, InputLine, OutputLine
from pioneer.hal.mock import MockHAL

__all__ = ["HAL", "EdgeEvent", "InputLine", "OutputLine", "MockHAL", "create_hal"]


def create_hal(kind: Literal["auto", "mock", "gpiod"], *, adc_device: str, adc_vref: float) -> HAL:
    if kind == "auto":
        try:
            import gpiod  # noqa: F401
            kind = "gpiod"
        except ImportError:
            kind = "mock"
    if kind == "gpiod":
        from pioneer.hal.gpiod_hal import GpiodHAL
        return GpiodHAL(adc_device)
    return MockHAL(adc_vref)
