import time

import pytest

from pioneer.hal import MockHAL
from pioneer.hal.softpwm import SoftPwm
from pioneer.pins import PinIO
from pioneer.resources import ResourceConflict, ResourceManager


@pytest.fixture
def hal():
    return MockHAL()


@pytest.fixture
def pins(board, hal):
    io = PinIO(board, hal, ResourceManager(board))
    yield io
    io.close_all()


def test_loopback_edges(board, hal, pins):
    """Как проба на плате: J9-16 (out) перемычкой на J9-18 (in)."""
    hal.wire(board.pin("J9-16").gpio, board.pin("J9-18").gpio)
    events = []
    pins.acquire("p1", "Тест", "J9-18", "gpio_in", on_edge=lambda pid, ev: events.append(ev))
    pins.acquire("p1", "Тест", "J9-16", "gpio_out")
    for v in (True, False, True):
        pins.write("J9-16", v)
    assert [e.rising for e in events] == [True, False, True]
    assert [e.seqno for e in events] == [1, 2, 3]
    assert pins.value("J9-18") is True


def test_debounce_filters_short_pulses(board, hal, pins):
    g = board.pin("J9-7").gpio
    events = []
    pins.acquire("p1", "Тест", "J9-7", "gpio_in", debounce_ms=20,
                 on_edge=lambda pid, ev: events.append(ev))
    for _ in range(5):  # дребезг
        hal.set_level(g, True)
        hal.set_level(g, False)
    hal.set_level(g, True)
    time.sleep(0.06)
    assert [e.rising for e in events] == [True]


def test_shared_input_exclusive_output(pins):
    a, b = [], []
    pins.acquire("p1", "Полив", "J9-7", "gpio_in", on_edge=lambda *x: a.append(x))
    pins.acquire("p2", "Свет", "J9-7", "gpio_in", on_edge=lambda *x: b.append(x))
    with pytest.raises(ResourceConflict, match='"Полив"'):
        pins.acquire("p3", "Чужой", "J9-7", "gpio_out")

    pins.acquire("p1", "Полив", "J9-12", "gpio_out")
    with pytest.raises(ResourceConflict, match="занят"):
        pins.acquire("p2", "Свет", "J9-12", "gpio_out")


def test_mode_switch_by_single_owner(board, hal, pins):
    pins.acquire("m", "Ручное", "J9-7", "gpio_out", initial=True)
    assert hal.levels[board.pin("J9-7").gpio] is True
    pins.acquire("m", "Ручное", "J9-7", "gpio_in")  # out -> in: старый выход в safe state
    assert pins.res.claims["J9-7"].mode == "gpio_in"
    assert hal.levels[board.pin("J9-7").gpio] is False


def test_interface_pin_rejected_with_hint(pins):
    with pytest.raises(ResourceConflict, match="I2C3 m0.*перезагрузите"):
        pins.acquire("p1", "Тест", "J9-11", "gpio_out")
    with pytest.raises(ResourceConflict, match="не поддерживает"):
        pins.acquire("p1", "Тест", "J9-26", "gpio_out")


def test_claim_many_atomic(pins):
    with pytest.raises(ResourceConflict):
        pins.res.claim_many("p1", "Тест", {"J9-7": "gpio_out", "J9-8": "gpio_out"})
    assert pins.res.claims == {}


def test_release_puts_output_to_safe_state(board, hal, pins):
    pins.acquire("p1", "Тест", "J9-7", "gpio_out", initial=True)
    pins.release("p1")
    assert hal.levels[board.pin("J9-7").gpio] is False
    assert pins.res.claims == {}


def test_adc(board, hal, pins):
    hal.set_adc(0, 0.9)
    pins.acquire("p1", "Тест", "J9-26", "adc")
    assert pins.value("J9-26") == pytest.approx(0.9)
    hal.set_adc(0, 5.0)  # больше vref — ограничивается
    assert pins.value("J9-26") == pytest.approx(1.8)


def test_softpwm_toggles(board, hal):
    line = hal.open_output(board.pin("J9-7").gpio)
    changes = []
    orig = line.write
    line.write = lambda v: (changes.append(v), orig(v))
    pwm = SoftPwm(line, freq=200, duty=0.5)
    time.sleep(0.2)
    pwm.stop()
    rising = sum(1 for a, b in zip(changes, changes[1:]) if not a and b)
    assert 25 <= rising <= 45  # ~40 периодов за 0.2 с, CI бывает медленным
    assert changes[-1] is False
    with pytest.raises(ValueError):
        pwm.set(freq=1000)
