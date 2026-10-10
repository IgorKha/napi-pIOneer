"""Python-блоки: схема из отдельного процесса, регистрация, исполнение в worker проекта."""
import time

import pytest

from conftest import wait_for
from pioneer.nodes import registry
from pioneer.projects import ProjectManager
from pioneer.userblocks import TEMPLATE, BlockManager
from test_api import anon, client  # noqa: F401
from pioneer.api.app import API


@pytest.fixture
def env(tmp_path, hw):
    reg = registry().copy()
    bm = BlockManager(tmp_path, reg)
    pm = ProjectManager(tmp_path, hw, reg)
    yield bm, pm, reg
    pm.close()


def test_template_registers(env):
    bm, _, reg = env
    b = bm.create("Гистерезис")
    assert b["error"] is None and b["types"] == ["user.hysteresis"]
    nt = reg.get("user.hysteresis@1")
    assert [i.name for i in nt.inputs.values()] == ["value", "low", "high"]
    assert nt.params["invert"].kind == "bool" and nt.outputs["out"].type == "Bool"
    assert "Включает выход" in nt.doc and nt.category == "Мои блоки"
    assert "user.hysteresis@1" not in registry().types  # встроенный реестр не тронут


def test_error_keeps_previous_version(env):
    bm, _, reg = env
    b = bm.create("Гистерезис")
    r = bm.save(b["id"], TEMPLATE.replace("def setup(self):", "def setup(self)"))
    assert "синтаксис, строка" in r["error"] and "user.hysteresis@1" in reg.types
    assert "def setup(self)\n" in bm.get(b["id"])["code"]  # код не потерян
    r = bm.save(b["id"], TEMPLATE.replace("In(Number, 0", "In(complex, 0"))
    assert "тип сокета" in r["error"]


def test_hysteresis_block_in_graph(env):
    """Фаза 2, «Готово, когда»: свой Гистерезис используется в графе без рестарта сервиса."""
    bm, pm, _ = env
    bm.create("Гистерезис")
    p = pm.create("С блоком")
    pm.apply_ops(p.id, [
        {"op": "add_node", "id": "n", "type": "builtin.ui_number"},
        {"op": "add_node", "id": "h", "type": "user.hysteresis", "inputs": {"low": 30, "high": 35}},
        {"op": "add_node", "id": "w", "type": "builtin.watch"},
        {"op": "connect", "from": ["n", "value"], "to": ["h", "value"]},
        {"op": "connect", "from": ["h", "out"], "to": ["w", "value"]},
    ])
    pm.start(p.id)
    rt = pm.engine.runtimes[p.id]
    for v, expect in ((40, True), (32, True), (20, False)):
        pm.ui(p.id, "n", v)
        assert rt.sample("h", "out").value is expect
    assert str(rt.sample("h", "out").origin) == "ui:n"


TIMER_BLOCK = '''from pioneer.block import node, Node, Out, Param, Event, Number

@node(id="ticker", title="Тикер")
class Ticker(Node):
    period = Param(float, 0.03, title="Период")
    tick = Out(Event)
    n = Out(Number)

    def setup(self):
        self.count = 0
        self.every(self.period, self.fire)

    def fire(self):
        self.count += 1
        self.n.set(self.count)
        self.tick.emit(self.count)
        if self.count == 2:
            self.log("второй тик")
'''


def test_block_timers_and_log(env):
    bm, pm, _ = env
    bm.create("Тикер", TIMER_BLOCK)
    p = pm.create("Тики")
    pm.apply_ops(p.id, [{"op": "add_node", "id": "t", "type": "user.ticker"},
                        {"op": "add_node", "id": "c", "type": "builtin.counter"},
                        {"op": "connect", "from": ["t", "tick"], "to": ["c", "up"]}])
    pm.start(p.id)
    rt = pm.engine.runtimes[p.id]
    assert wait_for(lambda: (rt.sample("c", "count").value or 0) >= 3, 2)
    assert rt.sample("t", "n").origin.kind == "block"
    assert wait_for(lambda: any("второй тик" in line["text"] for line in pm.log_tail(p.id)), 1)


def test_hung_block_isolated_and_dirty_after_save(env):
    bm, pm, _ = env
    slow = TEMPLATE.replace("    def on_change(self, changed):\n", "    def on_change(self, changed):\n        import time\n        time.sleep(3)\n")
    b = bm.create("Тормоз", slow)
    assert b["error"] is None, b["error"]
    p = pm.create("Зависание")
    pm.apply_ops(p.id, [{"op": "add_node", "id": "n", "type": "builtin.ui_number"},
                        {"op": "add_node", "id": "h", "type": "user.hysteresis"},
                        {"op": "add_node", "id": "ok", "type": "builtin.not"},
                        {"op": "connect", "from": ["n", "value"], "to": ["h", "value"]}])
    pm.start(p.id)
    t0 = time.monotonic()
    pm.ui(p.id, "n", 1)
    assert time.monotonic() - t0 < 2  # таймаут 1 с, а не 3
    rt = pm.engine.runtimes[p.id]
    assert "не ответил" in rt.node_state("h")["error"] and rt.sample("ok", "out").value is True
    assert not pm.status(p.id)["dirty"]
    time.sleep(0.01)
    bm.save(b["id"], TEMPLATE)
    assert pm.status(p.id)["dirty"]  # новая версия блока — через «Применить»
    pm.restart(p.id)
    pm.ui(p.id, "n", 40)
    assert pm.engine.runtimes[p.id].sample("h", "out").value is True


def test_blocks_api(client):
    r = client.post(f"{API}/blocks", json={"name": "Мой"}).json()
    assert r["types"] == ["user.hysteresis"]
    assert client.post(f"{API}/blocks/validate", json={"name": "", "code": "x = ("}).json()["ok"] is False
    types = {t["type"] for t in client.get(f"{API}/node-types").json()["types"]}
    assert "user.hysteresis@1" in types
    assert client.delete(f"{API}/blocks/{r['id']}").status_code == 200
    types = {t["type"] for t in client.get(f"{API}/node-types").json()["types"]}
    assert "user.hysteresis@1" not in types
