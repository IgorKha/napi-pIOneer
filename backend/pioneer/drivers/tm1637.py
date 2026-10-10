"""TM1637 — 4-разрядный 7-сегментный дисплей (модуль HW-069), bit-bang по двум выходам.

Протокол — двухпроводной, похож на I2C, но младший бит первым и без адреса:
start (DIO↓ при CLK=1) · байт · ACK (9-й такт) · stop (DIO↑ при CLK=1).
ACK не читаем: на 9-м такте держим DIO в 0 сами, чтобы не спорить с чипом (линии —
обычные выходы, переключение направления на vendor-ядре стоит 1–1.7 мс, docs/test-rig.md).

Кадр: 0x40 (запись с автоинкрементом) · 0xC0 + 4 байта сегментов · 0x88|яркость (вкл).
Бит 0x80 второго разряда — двоеточие.
"""
from __future__ import annotations

from collections.abc import Callable

DIGITS = (0x3F, 0x06, 0x5B, 0x4F, 0x66, 0x6D, 0x7D, 0x07, 0x7F, 0x6F)
CHARS = {
    **{str(i): d for i, d in enumerate(DIGITS)},
    " ": 0x00, "-": 0x40, "_": 0x08, "°": 0x63, "=": 0x48,
    "A": 0x77, "B": 0x7C, "C": 0x39, "D": 0x5E, "E": 0x79, "F": 0x71, "G": 0x3D, "H": 0x76, "I": 0x06,
    "J": 0x1E, "L": 0x38, "N": 0x54, "O": 0x3F, "P": 0x73, "Q": 0x67, "R": 0x50, "S": 0x6D, "T": 0x78,
    "U": 0x3E, "Y": 0x6E, "c": 0x58, "h": 0x74, "o": 0x5C, "u": 0x1C, "r": 0x50, "n": 0x54, "t": 0x78,
}
COLON = 0x80
WIDTH = 4


class DisplayError(ValueError):
    pass


def encode_text(text: str) -> tuple[list[int], bool]:
    """Строка → 4 байта сегментов (выравнивание вправо) и двоеточие ("12:34" — с двоеточием).

    Raises:
        DisplayError: символ не отображается или строка длиннее 4 знаков.
    """
    colon = False
    if len(text) == 5 and text[2] == ":":
        text, colon = text[:2] + text[3:], True
    if len(text) > WIDTH:
        raise DisplayError(f"«{text}» длиннее {WIDTH} знаков")
    segs = []
    for ch in text.rjust(WIDTH):
        seg = CHARS.get(ch, CHARS.get(ch.upper()))
        if seg is None:
            raise DisplayError(f"символ {ch!r} не отображается на 7-сегментном индикаторе")
        segs.append(seg)
    return segs, colon


def encode_number(value: float) -> list[int]:
    """Целое −999…9999 (дробь округляется), вправо."""
    n = round(value)
    if not -999 <= n <= 9999:
        raise DisplayError(f"{n} не помещается в 4 разряда")
    return encode_text(str(n))[0]


def encode_mmss(seconds: float) -> list[int]:
    """Секунды → ММ:СС (до 99:59, дальше — ЧЧ:ММ)."""
    s = max(0, int(seconds))
    a, b = (s // 60, s % 60) if s < 6000 else (s // 3600 % 100, s // 60 % 60)
    return encode_text(f"{a:02d}{b:02d}")[0]


class TM1637:
    """Драйвер поверх функции «выставить уровень» set_level(line, value), line: "clk" | "dio"."""

    def __init__(self, set_level: Callable[[str, bool], None]):
        self._set = set_level

    def _start(self) -> None:
        self._set("clk", True)
        self._set("dio", True)
        self._set("dio", False)

    def _stop(self) -> None:
        self._set("clk", False)
        self._set("dio", False)
        self._set("clk", True)
        self._set("dio", True)

    def _byte(self, b: int) -> None:
        for i in range(8):
            self._set("clk", False)
            self._set("dio", bool(b >> i & 1))
            self._set("clk", True)
        self._set("clk", False)  # ACK
        self._set("dio", False)
        self._set("clk", True)
        self._set("clk", False)

    def show(self, segments: list[int], colon: bool = False, brightness: int = 4) -> None:
        segs = list(segments[:WIDTH])
        if colon:
            segs[1] |= COLON
        self._start()
        self._byte(0x40)
        self._stop()
        self._start()
        self._byte(0xC0)
        for s in segs:
            self._byte(s)
        self._stop()
        self._start()
        self._byte(0x88 | max(0, min(7, brightness)))
        self._stop()

    def off(self) -> None:
        self._start()
        self._byte(0x80)
        self._stop()
