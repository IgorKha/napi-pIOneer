"""Скрипты в worker-процессах поверх стенда."""
import time

import pytest

from conftest import wait_for
from pioneer.scripts import TEMPLATE, ScriptError, ScriptManager


@pytest.fixture
def sm(tmp_path, hw):
    m = ScriptManager(tmp_path, hw)
    yield m
    m.stop_all()


def log_text(sm, sid):
    return "\n".join(line["text"] for line in sm.log_tail(sid))


def start_and_wait(sm, code, *, ready=None, timeout=5.0):
    meta = sm.create("тест", code)
    sm.start(meta.id)
    if ready:
        assert wait_for(ready, timeout), log_text(sm, meta.id)
    return meta.id


def test_phase0_button_toggles_led(sm, bench, hw):
    """Критерий фазы 0: кнопка на одном пине переключает LED на другом — скриптом."""
    bench.add("button", "btn", pin="J10-2", bounce_ms=5)
    led = bench.add("led", "led", pin="J9-7")
    sid = start_and_wait(sm, TEMPLATE, ready=lambda: "J10-2" in hw.res.claims)
    bench.action("btn", "press")
    assert wait_for(lambda: led.state()["on"] is True, 3), log_text(sm, sid)
    bench.action("btn", "release")
    time.sleep(0.08)
    bench.action("btn", "press")
    assert wait_for(lambda: led.state()["on"] is False, 3), log_text(sm, sid)
    assert "LED вкл" in log_text(sm, sid)

    sm.stop(sid)
    assert sm.status(sid).state == "stopped"
    assert hw.res.claims == {}  # всё отпущено


def test_crash_on_start_reports_error(sm):
    sid = start_and_wait(sm, "x = 1 +\n")
    assert wait_for(lambda: sm.status(sid).state == "error", 5)
    assert "SyntaxError" in sm.status(sid).error


def test_resource_conflict_message(sm):
    sid = start_and_wait(sm, 'from pioneer.sdk import Output\nOutput("J9-11")\n')
    assert wait_for(lambda: sm.status(sid).state == "error", 5)
    assert "I2C3 m0" in sm.status(sid).error


def test_handler_error_does_not_kill_script(sm, bench, hw):
    bench.add("level", "ext", pin="J9-7")
    code = '''
from pioneer.sdk import Input, log
inp = Input("J9-7")
@inp.on_rise
def boom(ev):
    log("before")
    1 / 0
'''
    sid = start_and_wait(sm, code, ready=lambda: "J9-7" in hw.res.claims)
    bench.action("ext", "set", {"value": True})
    assert wait_for(lambda: "ZeroDivisionError" in log_text(sm, sid), 3)
    time.sleep(0.1)
    assert sm.status(sid).state == "running"


def test_stop_unresponsive_script_with_sigkill(sm, hw):
    code = '''
import signal, time
from pioneer.sdk import Output
signal.signal(signal.SIGTERM, signal.SIG_IGN)
led = Output("J9-7", initial=True)
while True:
    time.sleep(0.1)
'''
    sid = start_and_wait(sm, code, ready=lambda: "J9-7" in hw.res.claims)
    assert hw.value("J9-7") is True
    sm.stop(sid)
    assert wait_for(lambda: sm.status(sid).state == "stopped", 4)
    assert "SIGKILL" in log_text(sm, sid)
    assert hw.res.claims == {}


def test_timer_and_log(sm):
    code = '''
from pioneer.sdk import every, log
n = 0
@every(0.05)
def tick():
    global n
    n += 1
    log("tick", n)
'''
    sid = start_and_wait(sm, code)
    assert wait_for(lambda: "tick 3" in log_text(sm, sid), 3)


def test_script_exits_normally(sm):
    sid = start_and_wait(sm, 'print("hello")\n')
    assert wait_for(lambda: sm.status(sid).state == "exited", 5)
    assert "hello" in log_text(sm, sid)


def test_i2c_from_script(sm, bench):
    bench.add("i2c_regs", "eeprom", bus=1, addr=0x50)
    code = '''
from pioneer.sdk import I2C, log
dev = I2C("i2c1", 0x50)
dev.write_reg(0x10, b"\\x2a")
log("read", dev.read_reg(0x10)[0])
try:
    I2C("i2c1", 0x51).read(1)
except Exception as e:
    log("nack", type(e).__name__)
'''
    sid = start_and_wait(sm, code)
    assert wait_for(lambda: sm.status(sid).state == "exited", 5), log_text(sm, sid)
    text = log_text(sm, sid)
    assert "read 42" in text and "nack PioneerError" in text


def test_crud_and_autostart(sm, tmp_path, hw):
    meta = sm.create("Свет")
    assert sm.code(meta.id) == TEMPLATE
    sm.update(meta.id, name="Свет в коридоре", autostart=True, code='print("auto")\n')
    assert [s["name"] for s in sm.list()] == ["Свет в коридоре"]

    sm2 = ScriptManager(tmp_path, hw)  # как после перезагрузки
    sm2.start_autostart()
    assert wait_for(lambda: "auto" in log_text(sm2, meta.id), 5)
    sm2.stop_all()

    sm.delete(meta.id)
    assert sm.list() == []
    with pytest.raises(ScriptError):
        sm.meta(meta.id)
    with pytest.raises(ScriptError):
        sm.meta("../../etc")


def test_double_start_rejected(sm, hw):
    sid = start_and_wait(sm, TEMPLATE, ready=lambda: "J9-7" in hw.res.claims)
    with pytest.raises(ScriptError, match="уже запущен"):
        sm.start(sid)


def test_two_scripts_conflict_on_output(sm, hw):
    code = 'from pioneer.sdk import Output, run\nOutput("J9-7")\nrun()\n'
    a = start_and_wait(sm, code, ready=lambda: "J9-7" in hw.res.claims)
    b = start_and_wait(sm, code)
    assert wait_for(lambda: sm.status(b).state == "error", 5)
    assert "J9-7 занят" in sm.status(b).error and "скрипт «тест»" in sm.status(b).error
    assert sm.status(a).state == "running"


def test_slow_handler_warns(sm):
    code = '''
from pioneer.sdk import every, sleep
@every(0.02)
def busy():
    sleep(0.15)
'''
    sid = start_and_wait(sm, code)
    assert wait_for(lambda: "обработчик busy() выполнялся" in log_text(sm, sid), 3)
    sm.stop(sid)
    assert log_text(sm, sid).count("блокировал цикл") == 1  # не чаще раза в 10 с
