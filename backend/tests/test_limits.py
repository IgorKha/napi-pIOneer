"""Таймауты worker-процессов: настройки, разделение бюджетов, предзагрузка блоков."""
import json
import time

import pytest

import pioneer.userblocks as workers
from pioneer import limits
from pioneer.config import load_config
from pioneer.engine.node import NodeError
from pioneer.limits import Limits, LimitsStore
from pioneer.nodes import registry
from pioneer.projects import ProjectError, ProjectManager
from pioneer.userblocks import BlockManager
from test_api import anon, client  # noqa: F401
from test_circuitpython import circuit  # noqa: F401
from pioneer.api.app import API


def use(monkeypatch, **values):
    monkeypatch.setattr(limits, "get", lambda: Limits(**values))


def test_store_precedence_validation_and_reset(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"mqtt": {"host": "broker"}}))
    store = LimitsStore(f, {"block_call_s": 2})
    assert store.get().block_call_s == 2.0 and store.get().block_load_s == Limits().block_load_s
    store.save({"block_load_s": 45, "block_preload": False})
    data = json.loads(f.read_text())
    assert data["mqtt"] == {"host": "broker"}  # чужой раздел цел
    assert data["limits"] == {"block_load_s": 45.0, "block_preload": False}
    assert LimitsStore(f, {"block_call_s": 2}).get().block_load_s == 45.0
    with pytest.raises(ValueError, match="0.1…60"):
        store.save({"block_call_s": 0})
    with pytest.raises(ValueError, match="неизвестный"):
        store.save({"nope": 1})
    store.save({"block_load_s": None})  # вернуть умолчание
    assert store.get().block_load_s == Limits().block_load_s
    f.write_text("{broken")
    assert LimitsStore(f).get() == Limits()  # испорченный файл не мешает запуску


def test_config_toml_limits_section(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[core]\nport = 8100\n\n[limits]\nblock_load_s = 60\n')
    cfg = load_config(cfg_file)
    assert cfg.port == 8100 and cfg.limits == {"block_load_s": 60}
    assert LimitsStore(tmp_path / "s.json", cfg.limits).get().block_load_s == 60.0


SLOW_SETUP = '''import time
from pioneer.block import node, Node, Out, Bool

@node(id="slow", title="Медленный")
class Slow(Node):
    ok = Out(Bool)

    def setup(self):
        time.sleep(SETUP)

    def start(self):
        self.ok.set(True)

    def on_change(self, changed):
        time.sleep(HANDLER)
'''


def _project(tmp_path, hw, code, slug="slow"):
    reg = registry().copy()
    bm = BlockManager(tmp_path, reg)
    b = bm.create("Медленный")
    r = bm.save(b["id"], code)
    assert r["error"] is None, r
    pm = ProjectManager(tmp_path, hw, reg)
    p = pm.create("Проект")
    pm.apply_ops(p.id, [{"op": "add_node", "id": "s", "type": f"user.{slug}"}])
    return pm, p.id


def test_slow_setup_fits_load_budget_not_handler_budget(tmp_path, hw, monkeypatch):
    """LCD-подобный setup() дольше обработчика: раньше «блок не ответил за 1 с»."""
    use(monkeypatch, block_call_s=0.3, block_load_s=5)
    pm, pid = _project(tmp_path, hw, SLOW_SETUP.replace("SETUP", "0.8").replace("HANDLER", "0"))
    try:
        pm.start(pid)
        assert pm.engine.runtimes[pid].sample("s", "ok").value is True
    finally:
        pm.close()


def test_load_budget_and_message_point_to_settings(tmp_path, hw, monkeypatch):
    use(monkeypatch, block_load_s=0.3)
    pm, pid = _project(tmp_path, hw, SLOW_SETUP.replace("SETUP", "1.5").replace("HANDLER", "0"))
    try:
        with pytest.raises(ProjectError, match="загрузка блока: нет ответа за 0.3 с.*Таймауты"):
            pm.start(pid)
    finally:
        pm.close()


SLOW_IMPORT = '''import time
time.sleep(1.2)  # тяжёлые импорты драйверов на A35
from pioneer.block import node, Node, Out, Bool

@node(id="heavy", title="Тяжёлый импорт")
class Heavy(Node):
    ok = Out(Bool)

    def start(self):
        self.ok.set(True)
'''


@pytest.mark.parametrize("preload, starts", [(True, True), (False, False)])
def test_preload_moves_imports_out_of_load_budget(tmp_path, hw, monkeypatch, preload, starts):
    use(monkeypatch, block_load_s=0.6, block_worker_start_s=20, block_preload=preload)
    pm, pid = _project(tmp_path, hw, SLOW_IMPORT, slug="heavy")
    try:
        if starts:
            pm.start(pid)
            assert pm.engine.runtimes[pid].sample("s", "ok").value is True
        else:
            with pytest.raises(ProjectError, match="загрузка блока"):
                pm.start(pid)
    finally:
        pm.close()


def test_worker_starts_outside_engine_thread(tmp_path, hw, monkeypatch):
    """Холодный старт процесса блоков не занимает поток движка: другие проекты живут."""
    use(monkeypatch, block_worker_start_s=20, block_load_s=5)
    pm, pid = _project(tmp_path, hw, SLOW_IMPORT, slug="heavy")
    seen = []
    original = workers.BlockHostClient.__init__

    def spy(self, *a, **kw):
        seen.append(pm.engine.in_engine_thread)
        original(self, *a, **kw)

    monkeypatch.setattr(workers.BlockHostClient, "__init__", spy)
    try:
        pm.start(pid)
        assert seen == [False]
    finally:
        pm.close()


def test_handler_timeout_still_stops_worker(tmp_path, hw, monkeypatch):
    use(monkeypatch, block_call_s=0.2)
    host = workers.BlockHostClient(lambda *a: None, lambda *a: None)
    path = tmp_path / "b.py"
    path.write_text(SLOW_SETUP.replace("SETUP", "0").replace("HANDLER", "1"))
    try:
        host.call({"op": "load", "file": str(path), "block": "slow", "node": "s", "params": {}})
        with pytest.raises(NodeError, match="обработчик блока: нет ответа 0.2 с без работы с железом"):
            host.call({"op": "call", "method": "on_change", "node": "s", "inputs": {}, "changed": []})
        assert host.dead
    finally:
        host.close()


def test_limits_api(client):  # noqa: F811
    r = client.get(API + "/settings/limits")
    assert r.status_code == 200 and r.json()["values"]["block_load_s"] == Limits().block_load_s
    assert {f["name"] for f in r.json()["fields"]} >= {"block_load_s", "block_call_s", "script_rpc_s"}
    r = client.put(API + "/settings/limits", json={"block_load_s": 40})
    assert r.status_code == 200 and r.json()["values"]["block_load_s"] == 40
    assert limits.get().block_load_s == 40  # действует сразу
    assert client.put(API + "/settings/limits", json={"block_load_s": 0}).status_code == 422
    r = client.put(API + "/settings/limits", json={"block_load_s": None})
    assert r.json()["values"]["block_load_s"] == Limits().block_load_s


BUSY_HANDLER = '''import time
from pioneer.block import node, Node, Out, Bool

@node(id="busy", title="Занят железом")
class Busy(Node):
    done = Out(Bool)

    def on_change(self, changed):
        t = time.monotonic()
        while time.monotonic() - t < DURATION:
            self._host.hardware(self.id, "noop", {})  # как запись в LCD по I2C
            time.sleep(0.01)
        self.done.set(True)
'''


def _busy_host(tmp_path, duration):
    path = tmp_path / "busy.py"
    path.write_text(BUSY_HANDLER.replace("DURATION", str(duration)))
    host = workers.BlockHostClient(lambda *a: None, lambda *a: None, lambda node, op, args: None)
    host.call({"op": "load", "file": str(path), "block": "busy", "node": "b", "params": {}})
    return host


def test_handler_working_with_hardware_is_extended(tmp_path, monkeypatch):
    """Вывод на LCD: обработчик дольше block_call_s, но всё время работает с железом."""
    use(monkeypatch, block_call_s=0.3, block_call_max_s=10)
    host = _busy_host(tmp_path, 1.2)
    try:
        actions = host.call({"op": "call", "method": "on_change", "node": "b", "inputs": {}, "changed": []})
        assert ["set", "done", True] in actions and not host.dead
    finally:
        host.close()


def test_handler_cap_applies_even_with_activity(tmp_path, monkeypatch):
    use(monkeypatch, block_call_s=0.3, block_call_max_s=1)
    host = _busy_host(tmp_path, 5)
    try:
        t0 = time.monotonic()
        with pytest.raises(NodeError, match="не уложился в максимум 1 с"):
            host.call({"op": "call", "method": "on_change", "node": "b", "inputs": {}, "changed": []})
        assert time.monotonic() - t0 < 2.5
    finally:
        host.close()


def test_direct_i2c_claims_once_then_bypasses_core(circuit, hw, monkeypatch):
    """На железе адрес захватывается в core один раз, транзакции идут в /dev/i2c-N напрямую."""
    import board
    import pioneer.compat.hardware as core_side
    from pioneer import direct_io

    use(monkeypatch, direct_i2c=True)
    monkeypatch.setattr(hw.hal, "name", "linux")
    monkeypatch.setattr(core_side, "Path", lambda p: type("P", (), {"exists": lambda self: True})())
    transfers = []

    class FakeBus:
        def transfer(self, addr, data=b"", n=0):
            transfers.append((addr, bytes(data), n))
            return bytes(n)

    monkeypatch.setattr(direct_io, "i2c", lambda bus: FakeBus())
    calls = []
    original = circuit.call
    monkeypatch.setattr(circuit, "call", lambda action, **a: (calls.append(action), original(action, **a))[1])
    i2c = board.I2C()
    assert i2c.try_lock()
    for value in range(50):
        i2c.writeto(0x27, bytes([value]))
    buf = bytearray(2)
    i2c.writeto_then_readfrom(0x27, b"\x01", buf)
    i2c.unlock()
    assert calls.count("i2c_claim") == 1 and "i2c" not in calls
    assert len(transfers) == 51 and transfers[-1] == (0x27, b"\x01", 2)
    assert "i2c1@0x27" in hw.res.claims  # захват адреса остаётся в ResourceManager


def test_direct_i2c_switch_off_uses_core(circuit, hw, monkeypatch, bench):
    import board

    use(monkeypatch, direct_i2c=False)
    monkeypatch.setattr(hw.hal, "name", "linux")
    bench.add("i2c_regs", bus=1, addr=0x76)
    i2c = board.I2C()
    assert i2c.try_lock()
    i2c.writeto(0x76, bytes([4, 9]))
    buf = bytearray(1)
    i2c.writeto_then_readfrom(0x76, bytes([4]), buf)
    i2c.unlock()
    assert buf == b"\x09"
