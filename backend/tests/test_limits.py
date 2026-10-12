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
        with pytest.raises(NodeError, match="обработчик блока: нет ответа за 0.2 с"):
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
