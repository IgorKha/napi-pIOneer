"""Napi-C pin identities; importing this module never acquires hardware."""

from dataclasses import dataclass
from types import SimpleNamespace
from pioneer.board import load_board
from pioneer.config import REPO_ROOT


@dataclass(frozen=True)
class Pin:
    id: str


pin = SimpleNamespace()
for _p in load_board(REPO_ROOT / "boards/napi-c.yaml").pins.values():
    if _p.gpio or _p.adc_channel is not None:
        _value = Pin(_p.id)
        setattr(pin, _p.id.replace("-", "_"), _value)
        if _p.name:
            setattr(pin, _p.name, _value)


class CPU:
    @property
    def temperature(self):
        raise NotImplementedError("Температура CPU Napi-C пока недоступна через CircuitPython")


cpu = CPU()
