"""TM1637: кодирование, протокол на линиях и нода движка на стенде."""
import pytest

from conftest import wait_for
from pioneer.drivers.tm1637 import DisplayError, TM1637, encode_mmss, encode_number, encode_text
from pioneer.engine import Engine
from pioneer.nodes import registry
from test_engine import graph


def decode(trace):
    """Лог уровней [(line, value)] → список транзакций (байты между start и stop)."""
    clk = dio = True
    frames, cur, bits = [], None, []
    for line, v in trace:
        if line == "dio" and clk and dio and not v:
            cur, bits = [], []                      # start
        elif line == "dio" and clk and not dio and v and cur is not None:
            frames.append(cur)                      # stop
            cur = None
        elif line == "clk" and v and not clk and cur is not None:
            bits.append(dio)                        # фронт CLK — защёлкнуть бит
            if len(bits) == 9:                      # 8 бит + такт ACK
                cur.append(sum(b << i for i, b in enumerate(bits[:8])))
                bits = []
        clk, dio = (v, dio) if line == "clk" else (clk, v)
    return frames


def test_encode():
    assert encode_text("PASS") == ([0x73, 0x77, 0x6D, 0x6D], False)
    assert encode_text("12:34") == ([0x06, 0x5B, 0x4F, 0x66], True)
    assert encode_number(-7) == [0, 0, 0x40, 0x07] and encode_number(42.4)[2:] == [0x66, 0x5B]
    assert encode_mmss(125) == encode_text("0205")[0]
    with pytest.raises(DisplayError, match="не помещается"):
        encode_number(12345)
    with pytest.raises(DisplayError, match="не отображается"):
        encode_text("W")


def test_protocol_frames():
    trace = []
    TM1637(lambda line, v: trace.append((line, v))).show([0x3F, 0x06, 0x5B, 0x4F], colon=True, brightness=7)
    assert decode(trace) == [[0x40], [0xC0, 0x3F, 0x86, 0x5B, 0x4F], [0x8F]]


def test_node_on_bench(hw, bench):
    trace = []
    orig = hw.write
    hw.write = lambda pin, v: (trace.append(({"J10-17": "clk", "J10-18": "dio"}[pin], v)), orig(pin, v))
    e = Engine(hw, registry())
    try:
        g = graph(("n", "builtin.ui_number"),
                  ("d", "builtin.tm1637", {"clk": "J10-17", "dio": "J10-18", "format": "mmss", "brightness": 2}),
                  edges=[("n.value", "d.value")])
        e.start_project("p", "Дисплей", g)
        e.ui("p", "n", 75)
        assert wait_for(lambda: [0xC0, *encode_mmss(75)] in decode(trace), 1)
        assert [0x8A] in decode(trace)
        e.stop_project("p")
        assert decode(trace)[-1] == [0x80]  # при остановке погашен
        assert "J10-17" not in hw.res.claims
    finally:
        e.close()
