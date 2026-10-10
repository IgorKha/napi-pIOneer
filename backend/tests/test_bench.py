"""BenchHAL и встроенные модели."""
import json

import pytest

from conftest import wait_for
from pioneer.hal import BenchHAL
from pioneer.hal.bench import BenchError, LineBusy


def gpio(board, pin):
    return board.pin(pin).gpio


def test_default_pull_levels(board, bench):
    assert bench.level("J9-7") is False  # pull-down
    assert bench.level("J10-5") is True  # pull-up
    inp = bench.open_input(gpio(board, "J10-16"))
    assert inp.read() is True
    inp.close()


def test_line_busy(board, bench):
    bench.open_output(gpio(board, "J9-7"))
    with pytest.raises(LineBusy):
        bench.open_input(gpio(board, "J9-7"))


def test_level_model(board, bench):
    events = []
    bench.open_input(gpio(board, "J9-7"), edge="both", on_edge=events.append)
    lvl = bench.add("level", "ext", pin="J9-7", value=True)
    assert wait_for(lambda: [e.rising for e in events] == [True])
    bench.action("ext", "set", {"value": False})
    assert wait_for(lambda: [e.rising for e in events] == [True, False])
    bench.remove(lvl.id)
    assert bench.level("J9-7") is False


def test_wire_loopback_like_board_probe(board, bench):
    """Как проба на плате: J9-16 (out) перемычкой на J9-18 (in)."""
    bench.add("wire", a="J9-16", b="J9-18")
    events = []
    bench.open_input(gpio(board, "J9-18"), edge="both", on_edge=events.append)
    out = bench.open_output(gpio(board, "J9-16"))
    for v in (True, False, True):
        out.write(v)
    assert wait_for(lambda: len(events) == 3)
    assert [e.rising for e in events] == [True, False, True]
    assert [e.seqno for e in events] == [1, 2, 3]


def test_events_come_from_bench_thread(board, bench):
    import threading
    threads = []
    bench.open_input(gpio(board, "J9-7"), edge="both",
                     on_edge=lambda ev: threads.append(threading.current_thread().name))
    bench.add("level", pin="J9-7", value=True)
    assert wait_for(lambda: threads == ["pioneer-bench"])


def test_button_bounce_and_debounce(board, bench):
    """Дребезг виден на входе без debounce; ядерный debounce даёт одно событие.
    Оба входа на одной кнопке через перемычку (одну линию нельзя запросить дважды)."""
    raw, deb = [], []
    btn = bench.add("button", "btn", pin="J10-2", bounce_ms=8)
    assert btn.active is True  # pull-down -> нажатие = HIGH
    bench.add("wire", a="J10-2", b="J10-4")
    bench.open_input(gpio(board, "J10-2"), edge="both", on_edge=raw.append)
    bench.open_input(gpio(board, "J10-4"), edge="both", debounce_ms=20, on_edge=deb.append)
    bench.action("btn", "press")
    assert wait_for(lambda: len(deb) == 1, 0.5)
    assert len(raw) > 1, "кнопка должна дребезжать"
    assert raw[-1].rising is True and deb[0].rising is True


def test_button_auto_level_on_pull_up(bench):
    btn = bench.add("button", pin="J10-5", bounce_ms=0)  # pull-up -> нажатие = LOW
    bench.action(btn.id, "press")
    assert bench.level("J10-5") is False
    bench.action(btn.id, "release")
    assert bench.level("J10-5") is True


def test_led_follows_output_and_pwm(board, bench):
    led = bench.add("led", pin="J9-7")
    out = bench.open_output(gpio(board, "J9-7"))
    out.write(True)
    assert led.state()["on"] is True
    out.close()
    assert led.state()["on"] is False
    pwm = bench.open_pwm(gpio(board, "J9-7"), freq=200, duty=0.25)
    assert led.state() == {"on": True, "brightness": 0.25, "pwm_hz": 200}
    pwm.close()
    assert led.state()["on"] is False


def test_voltage_quantized(bench):
    bench.add("voltage", "pot", pin="J9-26", volts=0.9)
    assert bench.read_adc(0) == pytest.approx(0.9, abs=0.002)
    bench.action("pot", "set", {"volts": 3.3})  # больше vref — потолок 1023 LSB
    assert bench.read_adc(0) == pytest.approx(1.8 * 1023 / 1024)
    assert bench.read_adc(1) == 0.0


def test_i2c_regs_and_nack(bench):
    bench.add("i2c_regs", bus=1, addr=0x50)
    bus = bench.open_i2c(1)
    bus.write(0x50, bytes([0x10, 0xAA, 0xBB]))
    assert bus.write_read(0x50, bytes([0x10]), 2) == b"\xaa\xbb"
    with pytest.raises(OSError) as e:
        bus.read(0x3C, 1)
    assert e.value.errno == 6  # ENXIO, как rk3x на плате
    with pytest.raises(BenchError):
        bench.add("i2c_regs", bus=1, addr=0x50)


def test_uart_and_spi_loopback(bench):
    bench.add("uart_loopback", device="/dev/ttyS3")
    port = bench.open_uart("/dev/ttyS3", baud=115200)
    port.write(b"hello")
    assert port.read(16, timeout=0.1) == b"hello"
    assert port.read(16, timeout=0.01) == b""

    bench.add("spi_loopback", bus=1, cs=0)
    assert bench.open_spi(1, 0).xfer(b"\x01\x02") == b"\x01\x02"
    assert bench.open_spi(2, 0).xfer(b"\x01") == b"\xff"


def test_bad_model_config(bench):
    with pytest.raises(BenchError, match="не GPIO"):
        bench.add("level", pin="J9-1")
    with pytest.raises(BenchError):
        bench.add("level", pinn="J9-7")
    with pytest.raises(BenchError, match="нет модели"):
        bench.add("nope")


def test_persistence(board, tmp_path):
    store = tmp_path / "bench.json"
    b1 = BenchHAL(board, store=store)
    b1.add("level", "ext", pin="J9-7")
    b1.action("ext", "set", {"value": True})
    b1.close()
    assert json.loads(store.read_text())[0]["config"]["value"] is True
    b2 = BenchHAL(board, store=store)
    assert b2.level("J9-7") is True
    b2.close()
