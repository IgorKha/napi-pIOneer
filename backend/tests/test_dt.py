"""Живой Device Tree: фейковое дерево в формате /proc/device-tree платы."""
import time

import pytest

from conftest import BOARD_FILE, DEFAULT_OVERLAYS, wait_for
from pioneer.board import load_board
from pioneer.dt import read_dt
from pioneer.hal import BenchHAL
from pioneer.hw import HW
from pioneer.resources import ResourceConflict, ResourceManager


def u32(*vals):
    return b"".join(v.to_bytes(4, "big") for v in vals)


def s(text):
    return text.encode() + b"\0"


def make_dt(root, nodes):
    for path, props in nodes.items():
        d = root / path.strip("/") if path != "/" else root
        d.mkdir(parents=True, exist_ok=True)
        for k, v in props.items():
            (d / k).write_bytes(v)
    return root


@pytest.fixture
def dt_dir(tmp_path):
    """Как на плате: uart1 (serial1) и i2c1 включены, uart4 выключен, LED на gpio-leds,
    плюс «чужой» оверлей, который держит J10-17 (GPIO2_B4) как rts-gpios."""
    return make_dt(tmp_path / "dt", {
        "/aliases": {"gpio0": s("/pinctrl/gpio@ff220000"), "gpio2": s("/pinctrl/gpio@ff240000"),
                     "gpio1": s("/pinctrl/gpio@ff230000"), "serial1": s("/serial@ff0b0000"),
                     "serial4": s("/serial@ff0e0000"), "i2c1": s("/i2c@ff050000")},
        "/__symbols__": {"pwm2": s("/pwm@ff180020")},
        "/pinctrl/gpio@ff220000": {"phandle": u32(0x83), "#gpio-cells": u32(2)},
        "/pinctrl/gpio@ff230000": {"phandle": u32(0x84), "#gpio-cells": u32(2)},
        "/pinctrl/gpio@ff240000": {"phandle": u32(0x50), "#gpio-cells": u32(2)},
        "/pinctrl/uart1/uart1-xfer": {"phandle": u32(0x20), "rockchip,pins": u32(1, 24, 1, 0x99, 1, 25, 1, 0x99)},
        "/pinctrl/uart4/uart4-xfer": {"phandle": u32(0x21), "rockchip,pins": u32(4, 8, 1, 0x99, 4, 9, 1, 0x99)},
        "/pinctrl/i2c1/i2c1-xfer": {"phandle": u32(0x22), "rockchip,pins": u32(0, 11, 1, 0x99, 0, 12, 1, 0x99)},
        "/serial@ff0b0000": {"status": s("okay"), "pinctrl-0": u32(0x20),
                             "rts-gpios": u32(0x50, 12, 0)},  # «пользовательский» RS-485 DE на J10-17
        "/serial@ff0e0000": {"status": s("disabled"), "pinctrl-0": u32(0x21)},
        "/i2c@ff050000": {"status": s("okay"), "pinctrl-0": u32(0x22)},
        "/gpio-leds/blue-led": {"gpios": u32(0x83, 5, 0)},
        "/disabled-parent": {"status": s("disabled")},
        "/disabled-parent/child": {"gpios": u32(0x50, 4, 0)},  # J9-7 — но предок выключен
    })


@pytest.fixture
def dboard(dt_dir):
    # uEnv: uart4 уже включён, но плата не перезагружена (pending)
    return load_board(BOARD_FILE, [*DEFAULT_OVERLAYS, "rk3308-uart4"], read_dt(dt_dir))


def test_read_dt(dt_dir):
    dt = read_dt(dt_dir)
    assert [u.label for u in dt.users(1, 24)] == ["serial1"]
    assert dt.users(4, 8) == []  # serial4 выключен
    assert [u.describe() for u in dt.users(2, 12)] == ["serial1 (rts-gpios)"]
    assert [u.describe() for u in dt.users(0, 5)] == ["blue-led (gpios)"]
    assert dt.users(2, 4) == []  # предок выключен
    assert dt.node_of("pwm2") == "/pwm@ff180020" and dt.node_of("serial1") == "/serial@ff0b0000"
    assert read_dt(dt_dir / "nope") is None


def test_functions_from_live_dt(dboard):
    assert dboard.function("J9-23") == "uart1"
    assert dboard.function("J9-3") == "i2c1"
    assert dboard.function("J10-17") == "kernel"
    assert dboard.kernel_users("J10-17") == ["serial1 (rts-gpios)"]
    # в uEnv включены uart2/uart3/i2c3, но в живом DT их нет — значит, сейчас пины свободны
    assert dboard.function("J9-19") == "gpio"
    assert dboard.function("J9-7") == "gpio"


def test_pending_reboot(dboard):
    assert dboard.interface_enabled("uart4") is False and dboard.uenv_enabled("uart4") is True
    assert dboard.interface_pending("uart4") is True
    assert dboard.interface_pending("uart1") is False
    assert dboard.interface_pending("uart2_m0") is True  # в uEnv есть, в DT нет


def test_resource_messages(dboard):
    hw = HW(dboard, BenchHAL(dboard), ResourceManager(dboard))
    with pytest.raises(ResourceConflict, match="J10-17 занят драйвером ядра: serial1 \\(rts-gpios\\)"):
        hw.acquire("p", "x", "J10-17", "gpio_out")
    with pytest.raises(ResourceConflict, match="uart4 включён в uEnv.txt, но ещё не применён — перезагрузите"):
        hw.uart("p", "x", "uart4")
    hw.uart("p", "x", "uart1")  # живой — открывается


def test_explicit_conflicts(board):
    assert "i2c3_m0" in board.conflicts("pwm2") and "pwm2" in board.conflicts("i2c3_m0")


# ---- встроенные LED
def test_bench_led_states(board, bench, hw):
    assert bench.led_state("rockpis:green:power")["on"] is True  # default-on
    led = hw.led("p", "Тест", "user")
    led.set(True)
    assert bench.led_state("rockpis:blue:user") == {"on": True, "trigger": "none", "triggers": bench.led_state(
        "rockpis:blue:user")["triggers"]}
    led.set_trigger("timer")
    assert bench.led_state("rockpis:blue:user")["trigger"] == "timer"
    with pytest.raises(ValueError):
        led.set_trigger("nope")
    with pytest.raises(ResourceConflict, match="led:user занят"):
        hw.led("q", "Другой", "user")
    hw.release("p")
    assert bench.led_state("rockpis:blue:user")["trigger"] == "heartbeat"  # системный триггер вернулся


def test_bench_heartbeat_blinks(bench):
    seen = set()
    end = time.monotonic() + 1.3
    while time.monotonic() < end:
        seen.add(bench.led_state("rockpis:blue:user")["on"])
        time.sleep(0.01)
    assert seen == {True, False}


def test_led_from_script(tmp_path, bench, hw):
    from pioneer.scripts import ScriptManager
    sm = ScriptManager(tmp_path, hw)
    meta = sm.create("led", 'from pioneer.sdk import BoardLed\nled = BoardLed("user")\nled.on()\nprint("ok")\n'
                            'from pioneer.sdk import run\nrun()\n')
    sm.start(meta.id)
    assert wait_for(lambda: bench.led_state("rockpis:blue:user")["trigger"] == "none", 5)
    assert bench.led_state("rockpis:blue:user")["on"] is True
    sm.stop(meta.id)
    assert bench.led_state("rockpis:blue:user")["trigger"] == "heartbeat"
