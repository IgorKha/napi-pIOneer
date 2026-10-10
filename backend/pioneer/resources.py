"""ResourceManager — кто какой ресурс держит (spec 3.5).

Ресурсы:
- пин: "J9-7" в рантайм-режиме (gpio_in / gpio_out / soft_pwm / adc);
- устройство на I2C: "i2c:1@0x3c" — шина общая, адрес эксклюзивный;
- SPI-устройство: "spi:1.0" (шина.CS) — эксклюзивно;
- UART: "uart:ttyS3" — эксклюзивно;
- встроенный LED: "led:user" — эксклюзивно (через /sys/class/leds).

Шины доступны, только если их интерфейс включён в uEnv.txt; их пины при этом уже
отданы интерфейсу (Board.function) и как GPIO недоступны.

gpio_out / soft_pwm — эксклюзивно; gpio_in / adc — разделяемые (рефкаунт владельцев).
Пакет захватов атомарен: либо все, либо ни одного.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Literal

from pioneer.board import Board, Interface, RuntimeMode

BusMode = Literal["i2c", "spi", "uart", "led"]
Mode = RuntimeMode | BusMode
SHARED: frozenset[str] = frozenset({"gpio_in", "adc"})

_I2C = re.compile(r"^i2c:(\d+)@0x([0-9a-f]{1,2})$")
_SPI = re.compile(r"^spi:(\d+)\.(\d+)$")
_UART = re.compile(r"^uart:(tty\w+)$")
_LED = re.compile(r"^led:(\w+)$")


def i2c_id(bus: int, addr: int) -> str:
    return f"i2c:{bus}@0x{addr:02x}"


def spi_id(bus: int, cs: int) -> str:
    return f"spi:{bus}.{cs}"


def uart_id(device: str) -> str:
    return f"uart:{device.removeprefix('/dev/')}"


def led_id(led: str) -> str:
    return f"led:{led}"


@dataclass
class Claim:
    resource: str
    mode: Mode
    owners: dict[str, str] = field(default_factory=dict)  # owner id -> человекочитаемое имя


class ResourceConflict(Exception):
    pass


class ResourceManager:
    def __init__(self, board: Board):
        self.board = board
        self.claims: dict[str, Claim] = {}
        self._lock = threading.Lock()

    # ---- проверки
    def check(self, owner: str, resource: str, mode: Mode) -> None:
        if ":" in resource:
            self._check_bus(resource, mode)
        else:
            self._check_pin(resource, mode)
        claim = self.claims.get(resource)
        if claim is None or owner in claim.owners and len(claim.owners) == 1:
            return
        if claim.mode == mode and mode in SHARED:
            return
        holders = ", ".join(f'"{n}"' for o, n in claim.owners.items() if o != owner)
        raise ResourceConflict(f"{resource} занят ({claim.mode}): {holders}")

    def _check_pin(self, pin_id: str, mode: Mode) -> None:
        board = self.board
        pin = board.pin(pin_id)
        if mode in board.runtime_modes(pin_id):
            return
        func = board.function(pin_id)
        if func == "kernel":
            raise ResourceConflict(
                f"{pin_id} занят драйвером ядра: {', '.join(board.kernel_users(pin_id))}")
        if func in board.interfaces:
            iface = board.interfaces[func]
            raise ResourceConflict(
                f"{pin_id} отдан интерфейсу {iface.title} ({iface.pins[pin_id]}); чтобы использовать как "
                f"{mode}, выключите {iface.title} в uEnv.txt и перезагрузите плату")
        raise ResourceConflict(f"{pin_id} ({pin.name or pin.kind}) не поддерживает режим {mode}")

    def _check_bus(self, resource: str, mode: Mode) -> None:
        if m := _I2C.match(resource):
            self._need(mode, "i2c", resource)
            bus, addr = int(m[1]), int(m[2], 16)
            if not 0x03 <= addr <= 0x77:
                raise ResourceConflict(f"{resource}: адрес I2C 0x03…0x77")
            self._enabled_iface(lambda i: i.device == f"/dev/i2c-{bus}", f"I2C-шина {bus}")
        elif m := _SPI.match(resource):
            self._need(mode, "spi", resource)
            bus, cs = int(m[1]), int(m[2])
            iface = self._enabled_iface(lambda i: i.id == f"spi{bus}", f"SPI{bus}")
            cs_roles = {r for r in iface.pins.values() if r.startswith("CSN")}
            if f"CSN{cs}" not in cs_roles:
                raise ResourceConflict(f"{resource}: на гребёнке выведены только {', '.join(sorted(cs_roles))}")
        elif m := _UART.match(resource):
            self._need(mode, "uart", resource)
            iface = self._enabled_iface(lambda i: i.device == f"/dev/{m[1]}", m[1])
            if iface.locked:
                raise ResourceConflict(f"{m[1]} — {iface.title}, занят системой")
        elif m := _LED.match(resource):
            self._need(mode, "led", resource)
            if m[1] not in self.board.leds:
                raise ResourceConflict(f"нет встроенного LED {m[1]!r}")
        else:
            raise ResourceConflict(f"неизвестный ресурс {resource!r}")

    @staticmethod
    def _need(mode: Mode, expected: str, resource: str) -> None:
        if mode != expected:
            raise ResourceConflict(f"{resource}: режим должен быть {expected}, а не {mode}")

    def _enabled_iface(self, match, what: str) -> Interface:
        candidates = [i for i in self.board.interfaces.values() if match(i)]
        for i in candidates:
            if self.board.interface_enabled(i.id):
                return i
        if not candidates:
            raise ResourceConflict(f"{what}: нет на гребёнке Napi-C")
        names = " или ".join(i.title for i in candidates)
        if any(self.board.interface_pending(i.id) for i in candidates):
            raise ResourceConflict(f"{what} включена в uEnv.txt, но ещё не применена — перезагрузите плату")
        raise ResourceConflict(f"{what} выключена: включите {names} в uEnv.txt и перезагрузите плату")

    # ---- захват
    def claim_many(self, owner: str, owner_name: str, wants: dict[str, Mode]) -> None:
        with self._lock:
            errors = []
            for res, mode in wants.items():
                try:
                    self.check(owner, res, mode)
                except ResourceConflict as e:
                    errors.append(str(e))
                except KeyError as e:  # нет такого пина
                    errors.append(e.args[0])
            if errors:
                raise ResourceConflict("; ".join(errors))
            for res, mode in wants.items():
                claim = self.claims.get(res)
                if claim is None or claim.mode != mode:  # смена режима единственным владельцем
                    claim = self.claims[res] = Claim(res, mode)
                claim.owners[owner] = owner_name

    def claim(self, owner: str, owner_name: str, resource: str, mode: Mode) -> None:
        self.claim_many(owner, owner_name, {resource: mode})

    def release(self, owner: str, resource: str | None = None) -> list[str]:
        """Освободить ресурс (или все ресурсы владельца). Возвращает ставшие свободными."""
        freed = []
        with self._lock:
            for rid, claim in list(self.claims.items()):
                if (resource is None or rid == resource) and owner in claim.owners:
                    del claim.owners[owner]
                    if not claim.owners:
                        del self.claims[rid]
                        freed.append(rid)
        return freed

    def owned_by(self, owner: str) -> list[str]:
        return [rid for rid, c in self.claims.items() if owner in c.owners]
