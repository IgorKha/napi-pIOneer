import pytest

from pioneer.board import InterfaceError, parse_uenv_overlays

UENV_031 = """verbosity=7
fdtfile=rk3308-napi-c.dtb
overlays=rk3308-uart1 rk3308-uart2-m0 rk3308-uart3-m0 rk3308-i2c1-ds1338 rk3308-i2c3-m0 rk3308-usb20-host
user_overlays=my
"""


def test_all_header_pins_described(board):
    for h in board.headers:
        ids = {p for p in board.pins if p.startswith(h.id + "-")}
        assert ids == {f"{h.id}-{n}" for n in range(1, h.rows * h.cols + 1)}


def test_gpio_unique(board):
    gpios = [p.gpio for p in board.pins.values() if p.gpio]
    assert len(gpios) == len(set(gpios))


def test_parse_uenv():
    assert parse_uenv_overlays(UENV_031)[3] == "rk3308-i2c1-ds1338"


def test_functions_default_uenv(board):
    assert board.function("J9-7") == "gpio"
    assert board.function("J9-3") == "i2c1"  # rk3308-i2c1-ds1338 включает i2c1
    assert board.function("J9-8") == "uart0"  # консоль — всегда
    assert board.function("J9-11") == "i2c3_m0"
    assert board.function("J10-5") == "gpio"  # uart4 выключен
    assert board.function("J9-26") == "adc"
    assert board.function("J9-1") is None


def test_runtime_modes(board):
    assert board.runtime_modes("J9-7") == ["gpio_in", "gpio_out", "soft_pwm"]
    assert board.runtime_modes("J9-11") == []
    assert board.runtime_modes("J10-3") == ["adc"]


def test_conflicts_from_shared_pins(board):
    assert set(board.conflicts("spi2")) == {"uart1", "uart2_m0", "i2c0"}
    assert set(board.conflicts("pwm2")) == {"i2c3_m0"}
    assert set(board.conflicts("uart3_m0")) == {"i2c3_m1", "spi1"}


def test_plan_switch_i2c3_to_pwm(board):
    with pytest.raises(InterfaceError, match="конфликтует"):
        board.plan_interfaces(enable=["pwm2"])
    plan = board.plan_interfaces(enable=["pwm2"], disable=["i2c3_m0"])
    assert "rk3308-i2c3-m0" not in plan.overlays and "rk3308-pwm2" in plan.overlays
    assert plan.pins_to_interface == {"J9-11": "pwm2"}
    assert plan.pins_to_gpio == ["J9-13"]  # J9-11 уходит в PWM2, J9-13 освобождается
    assert plan.needs_reboot


def test_plan_locked_and_variants(board):
    with pytest.raises(InterfaceError, match="нельзя"):
        board.plan_interfaces(disable=["uart0"])
    plan = board.plan_interfaces(disable=["i2c1"])  # снимает вариант с RTC
    assert "rk3308-i2c1-ds1338" not in plan.overlays
    assert plan.pins_to_gpio == ["J9-3", "J9-5"]
