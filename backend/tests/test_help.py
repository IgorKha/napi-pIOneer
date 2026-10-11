"""Справка: генератор из кода, исполняемые примеры, каталог ошибок (docs/ideas/004)."""
import re

import pytest

from conftest import wait_for
from pioneer import sdk
from pioneer.help import DOCS_DIR, build_reference, error_patterns, executable_blocks, load_guides, parse_docstring
from pioneer.nodes import registry
from pioneer.scripts import ScriptManager
from test_api import anon, client  # noqa: F401
from pioneer.api.app import API


def test_parse_docstring():
    d = parse_docstring(sdk.I2C.__doc__)
    assert d["summary"].startswith("Устройство на шине I2C")
    assert set(d["args"]) == {"interface", "addr"} and "0x03…0x77" in d["args"]["addr"]
    assert "NACK" in d["raises"] and 'I2C("i2c1", 0x76)' in d["example"] and d["hardware"]


def test_reference(board):
    ref = build_reference(board, registry())
    syms = {s["name"]: s for s in ref["sdk"]}
    assert list(syms) == sdk.__all__
    assert syms["I2C"]["signature"].startswith("I2C(interface: str, addr: int)")
    assert syms["I2C"]["snippet"] == 'I2C(${1:"i2c1"}, ${2:0x3C})'
    assert {m["name"] for m in syms["I2C"]["members"]} >= {"read_reg", "write_reg"}
    uart = {s["id"]: s for s in syms["UART"]["support"]}
    assert uart["uart3"]["available"] and not uart["uart4"]["available"] and "rk3308-uart4" in uart["uart4"]["note"]
    assert not uart["uart0"]["available"]
    out_pins = {s["id"]: s for s in syms["Output"]["support"]}
    assert out_pins["J9-7"]["available"] and not out_pins["J9-11"]["available"]  # J9-11 отдан I2C3
    assert {s["id"] for s in syms["Adc"]["support"]} == {"J9-26", "J10-3"}
    assert [g["id"] for g in ref["guides"]] == ["start", "projects", "recipes", "workspace", "blocks", "ai", "network", "sensors", "system", "hardware", "drivers", "modules", "errors"]
    assert len(ref["nodes"]) >= 30


def test_every_symbol_has_summary(board):
    for s in build_reference(board, None)["sdk"]:
        assert s["summary"], s["name"]
        for m in s["members"]:
            assert m["summary"], f"{s['name']}.{m['name']}"


@pytest.mark.parametrize("message", [
    'J9-7 занят (gpio_out): "проект «Полив»"',
    "J10-17 занят драйвером ядра: serial1 (rts-gpios)",
    "J9-11 отдан интерфейсу I2C3 m0 (SDA); чтобы использовать как gpio_out, выключите…",
    "uart4 выключен: нужен оверлей `rk3308-uart4` (J10-5 TX, J10-7 RX) в uEnv.txt и перезагрузка платы",
    "/dev/ttyS2 уже открыт: agetty (serial-getty@ttyS2.service)",
    "i2c-1: нет ACK от 0x51 (на стенде нет устройства)",
    "[Errno 6] No such device or address",
    "предупреждение: обработчик busy() выполнялся 0.15 с и блокировал цикл",
])
def test_error_catalog_matches(message):
    pats = error_patterns()
    assert any(re.search(p["pattern"], message) for p in pats), message


def _bench_setup(code: str, bench) -> None:
    for line in code.splitlines():
        m = re.match(r"#\s*стенд:\s*(\w+)\s*(.*)$", line)
        if not m:
            continue
        cfg = {}
        for kv in m[2].split():
            k, v = kv.split("=", 1)
            cfg[k] = int(v, 0) if re.fullmatch(r"0x[0-9a-fA-F]+|\d+", v) else \
                float(v) if re.fullmatch(r"\d+\.\d+", v) else v
        bench.add(m[1], **cfg)


EXAMPLES = [(g["id"], i, code) for g in load_guides(DOCS_DIR) for i, code in enumerate(executable_blocks(g["markdown"]))]


@pytest.mark.parametrize("guide,idx,code", EXAMPLES, ids=[f"{g}-{i}" for g, i, _ in EXAMPLES])
def test_guide_examples_run_on_bench(guide, idx, code, tmp_path, hw, bench):
    """Каждый ```python run из docs/sdk исполняется на стенде без ошибок."""
    _bench_setup(code, bench)
    sm = ScriptManager(tmp_path, hw)
    sid = sm.create(f"{guide}-{idx}", code).id
    sm.start(sid)
    try:
        wait_for(lambda: sm.status(sid).state != "running", 0.6)
        log = "\n".join(line["text"] for line in sm.log_tail(sid))
        assert sm.status(sid).state in ("running", "exited"), log
        assert "Traceback" not in log and "ошибка" not in log and "не отвечает" not in log, log
    finally:
        sm.stop(sid)
    assert len(EXAMPLES) >= 10


def test_help_api(client):
    r = client.get(f"{API}/help").json()
    assert r["sdk"][0]["name"] == "Input" and r["errors"]


def test_block_guide_examples_valid(tmp_path):
    from pioneer.help import block_examples
    from pioneer.userblocks import extract_schema
    blocks = [b for g in load_guides(DOCS_DIR) for b in block_examples(g["markdown"])]
    assert len(blocks) >= 2
    for i, code in enumerate(blocks):
        f = tmp_path / f"b{i}.py"
        f.write_text(code)
        r = extract_schema(f)
        assert r["ok"], r


def test_spec_custom_block_matches_public_sdk(tmp_path):
    import re
    from pioneer.userblocks import extract_schema
    source=(DOCS_DIR.parent/'spec.md').read_text()
    examples=[c for c in re.findall(r'```python\n(.*?)```',source,re.S) if 'from pioneer.block import' in c]
    assert examples
    for i,code in enumerate(examples):
        path=tmp_path/f'spec{i}.py';path.write_text(code)
        result=extract_schema(path)
        assert result['ok'],result
