"""Справочник SDK (docs/ideas/004): генерируется из кода, а не пишется руками.

Источники:
- pioneer.sdk — `__all__`, сигнатуры, Google-style докстринги;
- boards/*.yaml — какие пины/интерфейсы поддерживают каждый класс;
- реестр нод — схемы и `doc` нод графа;
- docs/sdk/*.md — руководства, рецепты, железо, каталог ошибок.

Отдаётся UI (`GET /help`: страница «Справка», подсказки Monaco), пишется в JSON для
статического сайта (`tools/gen_sdk_reference.py`). Офлайн: ничего не тянется из сети.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any

from pioneer import sdk
from pioneer.board import Board
from pioneer.config import REPO_ROOT
from pioneer.engine.node import NodeRegistry

DOCS_DIR = REPO_ROOT / "docs" / "sdk"
SECTIONS = ("Args", "Returns", "Raises", "Example", "Hardware")
# Порядок и заголовки руководств (файлы docs/sdk/<id>.md)
GUIDES = [("start", "Начало"), ("projects", "Проекты и пульт"), ("recipes", "Рецепты"),
          ("workspace", "Файлы и библиотеки"), ("blocks", "Свои блоки"), ("ai", "AI-помощник"),
          ("network", "MQTT и HTTP"), ("sensors", "Датчики I2C"), ("system", "Диагностика и сохранность"), ("hardware", "Железо"),
          ("drivers", "Драйверы ядра"), ("adafruit", "Adafruit / CircuitPython"), ("modules", "Модули периферии"), ("errors", "Ошибки")]

# Режим/шина, нужные классу SDK, — по ним из платы выбираются поддерживающие пины
SUPPORT = {"Output": ("pin", "gpio_out"), "Input": ("pin", "gpio_in"), "Pwm": ("pin", "soft_pwm"),
           "Adc": ("pin", "adc"), "HardwarePwm": ("bus", "pwm"), "SHT30": ("bus", "i2c"), "BH1750": ("bus", "i2c"), "I2C": ("bus", "i2c"), "SPI": ("bus", "spi"), "UART": ("bus", "uart"),
           "BoardLed": ("led", None)}
# Значения-заготовки для сниппетов автодополнения
PLACEHOLDER = {"pin": '"J9-7"', "addr": "0x3C", "seconds": "1.0", "led": '"user"', "fn": "fn", "name": '"heartbeat"',
               "data": 'b""', "n": "1", "reg": "0x00", "value": "True"}
IFACE_PLACEHOLDER = {"I2C": '"i2c1"', "SPI": '"spi2"', "UART": '"uart3"'}


# ---------------------------------------------------------------- докстринги
def parse_docstring(doc: str | None) -> dict[str, Any]:
    """Google-style: краткое описание, текст, секции Args/Returns/Raises/Example/Hardware."""
    doc = inspect.cleandoc(doc or "")
    out: dict[str, Any] = {"summary": "", "text": "", "args": {}}
    cur, buf = "text", []
    body: dict[str, list[str]] = {"text": []}
    for line in doc.splitlines():
        m = re.fullmatch(r"(\w+):", line.strip())
        if m and m[1] in SECTIONS and not line.startswith(" "):
            cur = m[1].lower()
            body[cur] = []
            continue
        body.setdefault(cur, []).append(line)
    text = "\n".join(body.pop("text")).strip()
    summary, _, rest = text.partition("\n\n")
    out["summary"], out["text"] = summary.replace("\n", " "), rest.strip()
    if "args" in body:
        name = None
        for line in body.pop("args"):
            m = re.match(r"^\s{0,4}(\*{0,2}\w+):\s*(.*)$", line)
            if m and (len(line) - len(line.lstrip())) <= 4:
                name = m[1].lstrip("*")
                out["args"][name] = m[2].strip()
            elif name and line.strip():
                out["args"][name] += " " + line.strip()
    for sec, lines in body.items():
        out[sec] = inspect.cleandoc("\n".join(lines)).strip()
    return out


def _signature(fn) -> tuple[str, list[dict]]:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return "()", []
    params = []
    for p in sig.parameters.values():
        if p.name in ("self", "cls"):
            continue
        d = {"name": p.name, "kind": p.kind.name.lower()}
        if p.annotation is not inspect.Parameter.empty:
            d["type"] = p.annotation if isinstance(p.annotation, str) else getattr(p.annotation, "__name__", str(p.annotation))
        if p.default is not inspect.Parameter.empty:
            d["default"] = repr(p.default)
        params.append(d)
    def fmt(p: dict) -> str:
        star = {"var_positional": "*", "var_keyword": "**"}.get(p["kind"], "")
        t = f": {p['type']}" if "type" in p else ""
        d = f" = {p['default']}" if "default" in p else ""
        return f"{star}{p['name']}{t}{d}"
    ret = sig.return_annotation
    ret = "" if ret is inspect.Signature.empty or ret in ("None", None) else f" -> {ret}"
    return f"({', '.join(fmt(p) for p in params)}){ret}", params


def _snippet(name: str, params: list[dict], owner: str | None = None) -> str:
    """Сниппет Monaco: обязательные параметры — плейсхолдерами."""
    parts, i = [], 1
    for p in params:
        if "default" in p or p["kind"] in ("var_positional", "var_keyword"):
            continue
        ph = (IFACE_PLACEHOLDER.get(owner or name) if p["name"] == "interface" else None) or \
            PLACEHOLDER.get(p["name"], p["name"])
        parts.append(f"${{{i}:{ph}}}")
        i += 1
    return f"{name}({', '.join(parts)})"


def _member(owner: str, name: str, obj) -> dict:
    if isinstance(obj, property):
        doc = parse_docstring(obj.fget.__doc__)
        return {"name": name, "kind": "property", "signature": name, "params": [],
                "writable": obj.fset is not None, **doc}
    sig, params = _signature(obj)
    return {"name": name, "kind": "method", "signature": f"{name}{sig}", "params": params,
            "snippet": _snippet(name, params, owner), **parse_docstring(obj.__doc__)}


def _support(name: str, board: Board) -> list[dict]:
    what, mode = SUPPORT.get(name, (None, None))
    out: list[dict] = []
    if what == "pin":
        for p in board.pins.values():
            static = ({"gpio_in", "gpio_out", "soft_pwm"} if p.gpio else {"adc"} if p.adc_channel is not None else set())
            if mode in static:
                func = board.function(p.id)
                out.append({"id": p.id, "name": p.name, "available": mode in board.runtime_modes(p.id),
                            "note": None if mode in board.runtime_modes(p.id) else
                            (board.interfaces[func].title if func in board.interfaces else func)})
    elif what == "bus":
        seen = set()
        for i in board.interfaces.values():
            if i.kind != mode or i.controller in seen:
                continue
            seen.add(i.controller)
            variants = [v for v in board.interfaces.values() if v.controller == i.controller]
            enabled = next((v for v in variants if board.interface_enabled(v.id)), None)
            out.append({"id": i.controller, "name": " / ".join(v.describe_pins() for v in variants),
                        "available": bool(enabled) and not i.locked,
                        "note": "системная консоль" if i.locked else None if enabled else
                        "выключен: " + " или ".join(f"оверлей {v.overlays[0]}" for v in variants)})
    elif what == "led":
        out = [{"id": lid, "name": led.title, "available": True, "note": None} for lid, led in board.leds.items()]
    return out


def sdk_symbols(board: Board | None = None) -> list[dict]:
    out = []
    for name in sdk.__all__:
        obj = getattr(sdk, name)
        if inspect.isclass(obj):
            is_exc = issubclass(obj, BaseException)
            sig, params = _signature(obj) if is_exc or obj.__init__ is object.__init__ else _signature(obj.__init__)
            members = [] if is_exc else [
                _member(name, m, v) for m, v in vars(obj).items()
                if not m.startswith("_") and (inspect.isfunction(v) or isinstance(v, property))]
            entry = {"name": name, "kind": "exception" if is_exc else "class", "signature": f"{name}{sig}",
                     "params": [] if is_exc else params, "members": members,
                     "snippet": None if is_exc else _snippet(name, params), **parse_docstring(obj.__doc__)}
        else:
            sig, params = _signature(obj)
            entry = {"name": name, "kind": "function", "signature": f"{name}{sig}", "params": params,
                     "members": [], "snippet": _snippet(name, params), **parse_docstring(obj.__doc__)}
        if board is not None:
            entry["support"] = _support(name, board)
        out.append(entry)
    return out


# ---------------------------------------------------------------- руководства
def load_guides(docs_dir: Path = DOCS_DIR) -> list[dict]:
    out = []
    for gid, title in GUIDES:
        f = docs_dir / f"{gid}.md"
        if f.exists():
            out.append({"id": gid, "title": title, "markdown": f.read_text()})
    return out


def error_patterns(docs_dir: Path = DOCS_DIR) -> list[dict]:
    """Каталог ошибок: `<!-- match: regex -->` под заголовком статьи → якорь статьи."""
    f = docs_dir / "errors.md"
    if not f.exists():
        return []
    out, anchor = [], None
    for line in f.read_text().splitlines():
        if m := re.match(r"^##\s+(.+)$", line):
            anchor = slug(m[1])
        elif (m := re.match(r"^<!--\s*match:\s*(.+?)\s*-->$", line.strip())) and anchor:
            out.append({"pattern": m[1], "anchor": anchor})
    return out


def slug(text: str) -> str:
    return re.sub(r"[^\w]+", "-", text.lower()).strip("-")


def executable_blocks(markdown: str) -> list[str]:
    """Блоки ```python run — исполняются в тестах на стенде (примеры не должны врать)."""
    return [m[1] for m in re.finditer(r"```python run\n(.*?)```", markdown, re.S)]


def block_examples(markdown: str) -> list[str]:
    """Блоки ```python block — проверяются в тестах (схема извлекается без ошибок)."""
    return [m[1] for m in re.finditer(r"```python block\n(.*?)```", markdown, re.S)]


def build_reference(board: Board | None, registry: NodeRegistry | None) -> dict:
    from pioneer import __version__
    return {
        "version": __version__,
        "sdk": sdk_symbols(board),
        "guides": load_guides(),
        "errors": error_patterns(),
        "nodes": registry.schemas() if registry else [],
    }
