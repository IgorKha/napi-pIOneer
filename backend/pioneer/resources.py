"""ResourceManager — кто какой пин держит и в каком рантайм-режиме (spec 3.5).

- gpio_out / soft_pwm — эксклюзивно;
- gpio_in / adc — разделяемые (одна линия на всех читателей, рефкаунт владельцев);
- пин, отданный интерфейсу (uEnv), в рантайм-режимах недоступен;
- пакет захватов атомарен: либо все, либо ни одного.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from pioneer.board import Board, RuntimeMode

SHARED: frozenset[RuntimeMode] = frozenset({"gpio_in", "adc"})


@dataclass
class PinClaim:
    pin: str
    mode: RuntimeMode
    owners: dict[str, str] = field(default_factory=dict)  # owner id -> человекочитаемое имя


class ResourceConflict(Exception):
    pass


class ResourceManager:
    def __init__(self, board: Board):
        self.board = board
        self.claims: dict[str, PinClaim] = {}
        self._lock = threading.Lock()

    def check(self, owner: str, pin_id: str, mode: RuntimeMode) -> None:
        board = self.board
        pin = board.pin(pin_id)
        if mode not in board.runtime_modes(pin_id):
            func = board.function(pin_id)
            if func in board.interfaces:
                iface = board.interfaces[func]
                role = iface.pins[pin_id]
                raise ResourceConflict(
                    f"{pin_id} отдан интерфейсу {iface.title} ({role}); чтобы использовать как "
                    f"{mode}, выключите {iface.title} в uEnv.txt и перезагрузите плату")
            raise ResourceConflict(f"{pin_id} ({pin.name or pin.kind}) не поддерживает режим {mode}")
        claim = self.claims.get(pin_id)
        if claim is None or owner in claim.owners and len(claim.owners) == 1:
            return
        if claim.mode == mode and mode in SHARED:
            return
        holders = ", ".join(f'"{n}"' for o, n in claim.owners.items() if o != owner)
        raise ResourceConflict(f"{pin_id} занят ({claim.mode}): {holders}")

    def claim_many(self, owner: str, owner_name: str, wants: dict[str, RuntimeMode]) -> None:
        with self._lock:
            errors = []
            for pin_id, mode in wants.items():
                try:
                    self.check(owner, pin_id, mode)
                except (ResourceConflict, KeyError) as e:
                    errors.append(str(e).strip("'\""))
            if errors:
                raise ResourceConflict("; ".join(errors))
            for pin_id, mode in wants.items():
                claim = self.claims.get(pin_id)
                if claim is None or claim.mode != mode:  # смена режима единственным владельцем
                    claim = self.claims[pin_id] = PinClaim(pin_id, mode)
                claim.owners[owner] = owner_name

    def claim(self, owner: str, owner_name: str, pin_id: str, mode: RuntimeMode) -> None:
        self.claim_many(owner, owner_name, {pin_id: mode})

    def release(self, owner: str, pin_id: str | None = None) -> list[str]:
        """Освободить пин (или все пины владельца). Возвращает пины, ставшие свободными."""
        freed = []
        with self._lock:
            for pid, claim in list(self.claims.items()):
                if (pin_id is None or pid == pin_id) and owner in claim.owners:
                    del claim.owners[owner]
                    if not claim.owners:
                        del self.claims[pid]
                        freed.append(pid)
        return freed
