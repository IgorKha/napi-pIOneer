"""Описание платы (boards/*.yaml) и модель режимов пинов.

Режимы пина двух видов:
- рантайм (`gpio_in`, `gpio_out`, `soft_pwm`, `adc`) — переключаются на лету
  через HAL/ResourceManager, когда пин в функции `gpio`;
- интерфейс (UART/I2C/SPI/HW PWM) — pinmux через DT-оверлей: пин отдан
  интерфейсу, если тот включён в uEnv.txt; смена — правка `overlays=` + reboot.

Интерфейс — группа пинов, включается/выключается целиком. Два интерфейса
конфликтуют, если делят хотя бы один пин.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

RuntimeMode = Literal["gpio_in", "gpio_out", "soft_pwm", "adc"]


@dataclass(frozen=True)
class GpioRef:
    chip: int
    line: int

    @property
    def path(self) -> str:
        return f"/dev/gpiochip{self.chip}"


@dataclass
class Interface:
    id: str
    title: str
    overlays: tuple[str, ...]
    pins: dict[str, str]  # pin id -> роль (TX, SDA, ...)
    device: str | None = None
    locked: bool = False
    note: str | None = None

    def matches(self, overlay: str) -> bool:
        return any(overlay == ov or overlay.startswith(ov + "-") for ov in self.overlays)


@dataclass
class Pin:
    id: str
    name: str | None = None
    gpio: GpioRef | None = None
    adc_channel: int | None = None
    power: str | None = None
    special: str | None = None
    default_pull: Literal["up", "down", "none"] | None = None
    drive_ma: int | None = None
    alt: tuple[str, ...] = ()
    interfaces: list[str] = field(default_factory=list)  # id интерфейсов, куда входит пин

    @property
    def kind(self) -> str:
        if self.power:
            return "power"
        if self.special:
            return "special"
        if self.adc_channel is not None:
            return "adc"
        return "gpio"


@dataclass
class Header:
    id: str
    rows: int
    cols: int
    pin1: str


@dataclass
class InterfacePlan:
    """Результат планирования смены интерфейсов (без записи в uEnv)."""
    overlays: list[str]  # новое значение `overlays=`
    enable: list[str]
    disable: list[str]
    pins_to_interface: dict[str, str]  # пин -> интерфейс, который его заберёт
    pins_to_gpio: list[str]  # пины, которые освободятся
    needs_reboot: bool = True


class InterfaceError(ValueError):
    pass


@dataclass
class Board:
    id: str
    title: str
    headers: list[Header]
    pins: dict[str, Pin]
    interfaces: dict[str, Interface]
    adc_device: str
    adc_vref: float
    adc_bits: int
    overlays: list[str] = field(default_factory=list)  # из uEnv.txt на момент загрузки

    def pin(self, pin_id: str) -> Pin:
        try:
            return self.pins[pin_id]
        except KeyError:
            raise KeyError(f"нет пина {pin_id!r} на плате {self.id}") from None

    # --- интерфейсы
    def interface_enabled(self, iface_id: str) -> bool:
        iface = self.interfaces[iface_id]
        return iface.locked or any(iface.matches(o) for o in self.overlays)

    @property
    def active_interfaces(self) -> list[str]:
        return [i for i in self.interfaces if self.interface_enabled(i)]

    def conflicts(self, iface_id: str) -> list[str]:
        pins = self.interfaces[iface_id].pins.keys()
        return [o for o, other in self.interfaces.items()
                if o != iface_id and pins & other.pins.keys()]

    # --- режимы пина
    def function(self, pin_id: str) -> str | None:
        """Текущая функция: id включённого интерфейса, 'gpio'/'adc' или None (питание и т.п.)."""
        pin = self.pin(pin_id)
        for iface in pin.interfaces:
            if self.interface_enabled(iface):
                return iface
        if pin.gpio:
            return "gpio"
        if pin.adc_channel is not None:
            return "adc"
        return None

    def runtime_modes(self, pin_id: str) -> list[RuntimeMode]:
        """Режимы, доступные без перезагрузки в текущей функции пина."""
        match self.function(pin_id):
            case "gpio":
                return ["gpio_in", "gpio_out", "soft_pwm"]
            case "adc":
                return ["adc"]
            case _:
                return []

    def plan_interfaces(self, enable: list[str] = (), disable: list[str] = ()) -> InterfacePlan:
        """Что будет, если включить/выключить интерфейсы. Конфликтующие включённые
        интерфейсы не выключаются молча — это ошибка, их надо явно перечислить в disable."""
        enable, disable = list(enable), list(disable)
        for i in enable + disable:
            if i not in self.interfaces:
                raise InterfaceError(f"нет интерфейса {i!r}")
        for i in disable:
            if self.interfaces[i].locked:
                raise InterfaceError(f"{self.interfaces[i].title} нельзя выключить")
        if set(enable) & set(disable):
            raise InterfaceError("интерфейс одновременно включается и выключается")

        after = (set(self.active_interfaces) - set(disable)) | set(enable)
        for i in enable:
            clash = [c for c in self.conflicts(i) if c in after]
            if clash:
                titles = ", ".join(self.interfaces[c].title for c in clash)
                raise InterfaceError(
                    f"{self.interfaces[i].title} конфликтует с {titles} (общие пины) — выключите их")

        overlays = [o for o in self.overlays
                    if not any(self.interfaces[i].matches(o) for i in disable)]
        for i in enable:
            if not self.interface_enabled(i):
                overlays.append(self.interfaces[i].overlays[0])

        before = set(self.active_interfaces)
        to_iface = {p: i for i in enable if i not in before for p in self.interfaces[i].pins}
        freed = sorted({p for i in disable if i in before for p in self.interfaces[i].pins}
                       - to_iface.keys())
        return InterfacePlan(overlays=overlays, enable=enable, disable=disable,
                             pins_to_interface=to_iface, pins_to_gpio=freed,
                             needs_reboot=overlays != self.overlays)


def load_board(path: Path, overlays: list[str] | None = None) -> Board:
    raw = yaml.safe_load(path.read_text())
    pins: dict[str, Pin] = {}
    for p in raw["pins"]:
        pin = Pin(
            id=p["id"],
            name=p.get("name"),
            gpio=GpioRef(**p["gpio"]) if "gpio" in p else None,
            adc_channel=p.get("adc_channel"),
            power=p.get("power"),
            special=p.get("special"),
            default_pull=p.get("default_pull"),
            drive_ma=p.get("drive_ma"),
            alt=tuple(p.get("alt", ())),
        )
        if pin.id in pins:
            raise ValueError(f"пин {pin.id} описан дважды")
        pins[pin.id] = pin

    interfaces: dict[str, Interface] = {}
    for iid, i in raw.get("interfaces", {}).items():
        iface = Interface(id=iid, title=i["title"], overlays=tuple(i["overlays"]),
                          pins=dict(i["pins"]), device=i.get("device"),
                          locked=i.get("locked", False), note=i.get("note"))
        for pid in iface.pins:
            if pid not in pins or not pins[pid].gpio:
                raise ValueError(f"интерфейс {iid}: {pid} не GPIO-пин")
            pins[pid].interfaces.append(iid)
        interfaces[iid] = iface

    return Board(
        id=raw["id"],
        title=raw["title"],
        headers=[Header(id=k, **v) for k, v in raw["headers"].items()],
        pins=pins,
        interfaces=interfaces,
        adc_device=raw["adc"]["device"],
        adc_vref=raw["adc"]["vref"],
        adc_bits=raw["adc"]["bits"],
        overlays=list(overlays or []),
    )


def parse_uenv_overlays(text: str) -> list[str]:
    """Значение `overlays=` из uEnv.txt (user_overlays — свои, на пины не маппятся)."""
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key == "overlays":
            return value.split()
    return []
