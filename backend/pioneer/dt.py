"""Живой Device Tree: какие пины держат узлы ядра (docs/ideas/002, п.1).

Источник правды о занятости пинов на работающей плате — не uEnv.txt, а применённый DT:
он учитывает user_overlays, ядерные драйверы (gpio-leds, rts-gpios, …) и ручные правки.

Для узлов со status = okay (и всеми включёнными предками):
- pinctrl-0 → группы pinctrl → rockchip,pins (банк, пин, mux, конфиг) — пин отдан функции;
- свойства *-gpios / gpios → <&gpioN пин флаги> — пин держит драйвер как GPIO.
pinmux-pins у vendor-ядра бесполезен, поэтому разбираем DT сами.
"""
from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DT = Path("/proc/device-tree")
_WANTED = {"status", "phandle", "pinctrl-0", "rockchip,pins", "#gpio-cells"}


@dataclass(frozen=True)
class DtUser:
    node: str  # путь узла, /serial@ff0b0000
    label: str  # алиас (serial1) или имя узла
    via: str  # "pinctrl" | имя свойства *-gpios
    mux: int | None = None  # функция пина для pinctrl (0 = GPIO)

    def describe(self) -> str:
        return self.label if self.via == "pinctrl" else f"{self.label} ({self.via})"


@dataclass
class DeviceTree:
    claims: dict[tuple[int, int], list[DtUser]] = field(default_factory=lambda: defaultdict(list))
    aliases: dict[str, str] = field(default_factory=dict)  # serial1 -> /serial@ff0b0000
    symbols: dict[str, str] = field(default_factory=dict)  # метки DTS: pwm2 -> /pwm@ff180020

    def users(self, bank: int, pin: int) -> list[DtUser]:
        return self.claims.get((bank, pin), [])

    def node_of(self, ref: str) -> str:
        """Алиас (serial1), метка (pwm2) или путь (/pwm@ff180020) -> путь узла."""
        return self.aliases.get(ref) or self.symbols.get(ref) or ref


def _u32s(data: bytes) -> list[int]:
    return [int.from_bytes(data[i:i + 4], "big") for i in range(0, len(data) - 3, 4)]


def _str(data: bytes) -> str:
    return data.split(b"\0", 1)[0].decode(errors="replace")


def read_dt(base: Path = DEFAULT_DT) -> DeviceTree | None:
    """None, если DT нет (Mac/тесты) — тогда занятость берётся из uEnv.txt."""
    if not base.exists():
        return None
    base = base.resolve()
    props: dict[str, dict[str, bytes]] = {}
    for root, dirs, files in os.walk(base):
        rel = "/" + os.path.relpath(root, base) if root != str(base) else "/"
        whole = rel in ("/aliases", "/__symbols__")
        node = {}
        for f in files:
            # читаем только нужное: полный обход sysfs-DT на A35 стоил ~4 с
            if whole or f in _WANTED or f.endswith("gpios"):
                try:
                    node[f] = (Path(root) / f).read_bytes()
                except OSError:
                    pass
        props[rel] = node

    dt = DeviceTree()
    dt.aliases = {k: _str(v) for k, v in props.get("/aliases", {}).items() if k != "name"}
    dt.symbols = {k: _str(v) for k, v in props.get("/__symbols__", {}).items() if k != "name"}
    path_alias = {v: k for k, v in dt.aliases.items()}
    phandles = {_u32s(p["phandle"])[0]: path for path, p in props.items() if "phandle" in p}
    gpio_bank = {}  # phandle gpio-контроллера -> номер банка
    for alias, path in dt.aliases.items():
        if alias.startswith("gpio") and alias[4:].isdigit() and "phandle" in props.get(path, {}):
            gpio_bank[_u32s(props[path]["phandle"])[0]] = int(alias[4:])

    def enabled(path: str) -> bool:
        while True:
            st = props.get(path, {}).get("status")
            if st is not None and _str(st) not in ("okay", "ok"):
                return False
            if path == "/":
                return True
            path = path.rsplit("/", 1)[0] or "/"

    for path, p in props.items():
        if path in ("/", "/aliases", "/__symbols__", "/__fixups__", "/__local_fixups__") or not enabled(path):
            continue
        label = path_alias.get(path, path.rsplit("/", 1)[-1])
        if "pinctrl-0" in p:
            for ph in _u32s(p["pinctrl-0"]):
                group = props.get(phandles.get(ph, ""), {})
                quads = _u32s(group.get("rockchip,pins", b""))
                for i in range(0, len(quads) - 3, 4):
                    bank, pin, mux = quads[i], quads[i + 1], quads[i + 2]
                    if mux == 0 and label.startswith("gpio") and phandles.get(ph, "").startswith("/pinctrl/pioneer-gpio-bias/"):
                        continue  # default electrical bias does not reserve a GPIO consumer
                    dt.claims[(bank, pin)].append(DtUser(path, label, "pinctrl", mux))
        for prop, raw in p.items():
            if not (prop == "gpios" or prop.endswith("-gpios")):
                continue
            cells = _u32s(raw)
            i = 0
            while i < len(cells):
                bank = gpio_bank.get(cells[i])
                if bank is None:  # не GPIO-контроллер Rockchip — дальше не разобрать
                    break
                ctrl = phandles[cells[i]]
                ncells = _u32s(props[ctrl].get("#gpio-cells", b"\0\0\0\x02"))[0]
                dt.claims[(bank, cells[i + 1])].append(DtUser(path, label, prop))
                i += 1 + ncells
    return dt
