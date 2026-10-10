"""Проекты: хранение, пакеты операций с ревизиями, запуск/«Применить», автозапуск, API и WS."""
import pytest

from conftest import wait_for
from pioneer.nodes import registry
from pioneer.projects import ProjectError, ProjectManager, RevConflict
from test_api import anon, client  # noqa: F401 — фикстуры
from pioneer.api.app import API

REG = registry()


@pytest.fixture
def pm(tmp_path, hw):
    m = ProjectManager(tmp_path, hw, REG)
    yield m
    m.close()


BLINK = [
    {"op": "add_node", "id": "t", "type": "builtin.interval", "position": [0, 0], "inputs": {"period_ms": 20}},
    {"op": "add_node", "id": "tg", "type": "builtin.toggle", "position": [200, 0]},
    {"op": "add_node", "id": "o", "type": "builtin.digital_out", "position": [400, 0], "params": {"pin": "J9-7"}},
    {"op": "connect", "from": ["t", "tick"], "to": ["tg", "toggle"]},
    {"op": "connect", "from": ["tg", "out"], "to": ["o", "value"]},
]


def test_ops_revisions_and_undo(pm):
    meta = pm.create("Мигалка")
    r = pm.apply_ops(meta.id, BLINK, base_rev=0)
    assert r["rev"] == 1 and r["validation"]["can_start"]
    with pytest.raises(RevConflict):
        pm.apply_ops(meta.id, [{"op": "move", "node": "o", "position": [1, 1]}], base_rev=0)
    undo = pm.apply_ops(meta.id, r["inverse"], base_rev=1)
    assert undo["rev"] == 2 and pm.get(meta.id)["graph"]["nodes"] == []
    with pytest.raises(ProjectError, match="операция 1"):
        pm.apply_ops(meta.id, [{"op": "connect", "from": ["x", "y"], "to": ["z", "w"]}])
    assert pm.get(meta.id)["rev"] == 2  # отклонённый пакет ничего не меняет


def test_start_apply_and_dirty(pm, bench):
    meta = pm.create("Мигалка")
    pm.apply_ops(meta.id, BLINK)
    st = pm.start(meta.id)
    assert st["state"] == "running" and st["rev"] == 1 and not st["dirty"]
    levels = set()
    assert wait_for(lambda: levels.add(bench.level("J9-7")) or levels == {True, False}, 1)
    pm.apply_ops(meta.id, [{"op": "set_param", "node": "o", "name": "pin", "value": "J9-12"}])
    assert pm.status(meta.id)["dirty"]  # запущен старый граф
    assert pm.restart(meta.id)["rev"] == 2
    assert "J9-12" in pm.hw.res.claims and "J9-7" not in pm.hw.res.claims
    pm.stop(meta.id)
    assert pm.status(meta.id)["state"] == "stopped" and not pm.hw.res.claims


def test_incomplete_graph_does_not_start(pm):
    meta = pm.create("Пусто")
    pm.apply_ops(meta.id, [{"op": "add_node", "id": "o", "type": "builtin.digital_out"}])
    with pytest.raises(ProjectError, match="не задан параметр «Пин»"):
        pm.start(meta.id)
    assert pm.status(meta.id)["state"] == "error"


def test_conflict_with_script_owner_and_warning(pm, hw):
    hw.acquire("script:abc", "скрипт «Тест»", "J9-7", "gpio_out")
    meta = pm.create("Мигалка")
    r = pm.apply_ops(meta.id, BLINK)
    assert any("скрипт «Тест»" in w["message"] for w in r["validation"]["warnings"])
    with pytest.raises(ProjectError, match="J9-7 занят"):
        pm.start(meta.id)


def test_autostart_and_persistence(tmp_path, hw, bench):
    pm1 = ProjectManager(tmp_path, hw, REG)
    meta = pm1.create("Авто")
    pm1.apply_ops(meta.id, [{"op": "add_node", "id": "o", "type": "builtin.digital_out",
                             "params": {"pin": "J9-7"}, "inputs": {"value": True}}])
    pm1.update_meta(meta.id, autostart=True)
    pm1.close()
    pm2 = ProjectManager(tmp_path, hw, REG)  # как после перезагрузки
    try:
        pm2.start_autostart()
        assert pm2.status(meta.id)["state"] == "running" and bench.level("J9-7") is True
        msgs = pm2.drain()
        assert any(m["type"] == "project_status" for m in msgs)
        assert any(m["type"] == "project_log" and "запущен" in m["lines"][0]["text"] for m in msgs)
    finally:
        pm2.close()


def test_api_flow(client):
    types = client.get(f"{API}/node-types").json()
    keys = {t["type"] for t in types["types"]}
    assert "builtin.digital_in@1" in keys and types["socket_colors"]["Event"]
    pid = client.post(f"{API}/projects", json={"name": "Слайдер"}).json()["id"]
    r = client.post(f"{API}/projects/{pid}/ops", json={"rev": 0, "ops": [
        {"op": "add_node", "id": "s", "type": "builtin.ui_slider", "position": [0, 0]},
        {"op": "add_node", "id": "w", "type": "builtin.watch", "position": [200, 0]},
        {"op": "connect", "from": ["s", "value"], "to": ["w", "value"]}]})
    assert r.status_code == 200 and r.json()["rev"] == 1
    bad = client.post(f"{API}/projects/{pid}/ops", json={"ops": [{"op": "nope"}]})
    assert bad.status_code == 422 and bad.json()["detail"]["errors"][0]["op_index"] == 0
    v = client.post(f"{API}/projects/{pid}/ops/validate", json={"ops": [{"op": "remove_node", "id": "s"}]}).json()
    assert v["ok"]
    assert client.post(f"{API}/projects/{pid}/start").json()["state"] == "running"
    with client.websocket_connect("/pioneer/ws") as ws:
        ws.send_json({"type": "ui", "project": pid, "node": "s", "value": 0.5})
        msg = next(m for m in iter(ws.receive_json, None)
                   if m["type"] == "project_live" and "w" in m.get("views", {}))
        assert msg["views"]["w"]["value"] == 0.5 and msg["views"]["w"]["origin"] == "ui:s"
    live = client.get(f"{API}/projects/{pid}/live").json()
    assert live["values"]["s.value"]["v"] == 0.5
    assert client.post(f"{API}/projects/{pid}/nodes/w/ui", json={"value": 1}).status_code == 409
    p = client.get(f"{API}/projects/{pid}").json()
    assert p["status"]["state"] == "running" and p["validation"]["can_start"]
    assert client.post(f"{API}/panic").json() == {"stopped": True}
    assert client.get(f"{API}/projects/{pid}").json()["status"]["state"] == "stopped"
    assert client.delete(f"{API}/projects/{pid}").status_code == 200
    assert client.get(f"{API}/projects/{pid}").status_code == 404
