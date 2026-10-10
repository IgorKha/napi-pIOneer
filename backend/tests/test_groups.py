"""Node Groups: группировка выделения, развёртка при запуске, live, разгруппировка, правка внутри."""
import pytest

from pioneer.groups import GroupError, GroupManager, apply_group_ops
from pioneer.nodes import registry
from pioneer.projects import ProjectError, ProjectManager


@pytest.fixture
def env(tmp_path, hw, bench):
    reg = registry().copy()
    gm = GroupManager(tmp_path, reg)
    pm = ProjectManager(tmp_path, hw, reg)
    pm.groups = gm
    yield gm, pm, reg
    pm.close()


GRAPH = [
    {"op": "add_node", "id": "n", "type": "builtin.ui_number", "position": [0, 0]},
    {"op": "add_node", "id": "m", "type": "builtin.math", "position": [200, 0], "params": {"op": "mul"}, "inputs": {"b": 3}},
    {"op": "add_node", "id": "c", "type": "builtin.clamp", "position": [400, 0], "inputs": {"max": 50}},
    {"op": "add_node", "id": "w", "type": "builtin.watch", "position": [600, 0]},
    {"op": "add_node", "id": "o", "type": "builtin.digital_out", "position": [600, 100], "params": {"pin": "J9-7"}},
    {"op": "add_node", "id": "cmp", "type": "builtin.compare", "position": [400, 100], "inputs": {"b": 20}},
    {"op": "connect", "from": ["n", "value"], "to": ["m", "a"]},
    {"op": "connect", "from": ["m", "result"], "to": ["c", "value"]},
    {"op": "connect", "from": ["c", "result"], "to": ["w", "value"]},
    {"op": "connect", "from": ["m", "result"], "to": ["cmp", "a"]},
    {"op": "connect", "from": ["cmp", "out"], "to": ["o", "value"]},
]


def test_group_run_ungroup(env, bench):
    gm, pm, reg = env
    p = pm.create("Группы")
    pm.apply_ops(p.id, GRAPH)
    r = pm.apply_ops(p.id, [{"op": "group", "id": "g", "nodes": ["m", "c", "cmp"], "name": "Обработка"}])
    graph = pm.get(p.id)["graph"]
    assert {n["id"] for n in graph["nodes"]} == {"n", "w", "o", "g"}
    d = gm.of_type(graph["nodes"][-1]["type"]) or next(iter(gm.defs.values()))
    assert [i["name"] for i in d.inputs] == ["a"] and [o["name"] for o in d.outputs] == ["result", "out"]
    assert reg.get(d.key).title == "Обработка" and r["validation"]["can_start"]

    pm.start(p.id)
    pm.ui(p.id, "n", 10)
    rt = pm.engine.runtimes[p.id]
    assert rt.sample("g/c", "result").value == 30 and bench.level("J9-7") is True
    live = pm.live_snapshot(p.id)
    assert live["values"]["g.result"]["v"] == 30 and live["values"]["g.out"]["v"] is True  # выходы группы — на её ноде
    pm.stop(p.id)

    # правка изнутри: ревизия группы растёт, запущенный проект — «граф изменён»
    pm.start(p.id)
    apply_group_ops(gm, d.id, [{"op": "set_input_value", "node": "m", "name": "b", "value": 10}], d.rev, pm.hw.board)
    assert pm.status(p.id)["dirty"]
    pm.restart(p.id)
    pm.ui(p.id, "n", 2)
    assert pm.engine.runtimes[p.id].sample("g/c", "result").value == 20
    pm.stop(p.id)
    with pytest.raises(GroupError, match="вход группы"):
        apply_group_ops(gm, d.id, [{"op": "remove_node", "id": "m"}], None, pm.hw.board)

    # ungroup и undo
    u = pm.apply_ops(p.id, [{"op": "ungroup", "node": "g"}])
    ids = {n["id"] for n in pm.get(p.id)["graph"]["nodes"]}
    assert {"g_m", "g_c", "g_cmp"} <= ids and "g" not in ids
    assert pm.get(p.id)["validation"]["can_start"]
    pm.apply_ops(p.id, u["inverse"])
    assert "g" in {n["id"] for n in pm.get(p.id)["graph"]["nodes"]}
    # undo группировки возвращает исходный граф
    pm.apply_ops(p.id, r["inverse"])
    assert {n["id"] for n in pm.get(p.id)["graph"]["nodes"]} == {"n", "w", "o", "m", "c", "cmp"}


def test_pin_conflict_inside_group_reported(env):
    gm, pm, _ = env
    p = pm.create("Конфликт")
    pm.apply_ops(p.id, [
        {"op": "add_node", "id": "a", "type": "builtin.digital_out", "params": {"pin": "J9-7"}},
        {"op": "add_node", "id": "b", "type": "builtin.digital_out", "params": {"pin": "J9-7"}},
    ])
    pm.apply_ops(p.id, [{"op": "group", "id": "g", "nodes": ["a"], "name": "Выход"}])
    v = pm.get(p.id)["validation"]
    assert not v["can_start"] and "несколькими нодами" in v["incomplete"][0]["message"]
    with pytest.raises(ProjectError):
        pm.start(p.id)


def test_nested_groups(env):
    gm, pm, _ = env
    p = pm.create("Вложенные")
    pm.apply_ops(p.id, GRAPH)
    pm.apply_ops(p.id, [{"op": "group", "id": "g1", "nodes": ["m", "c"], "name": "Внутренняя"}])
    pm.apply_ops(p.id, [{"op": "group", "id": "g2", "nodes": ["g1", "cmp"], "name": "Внешняя"}])
    pm.start(p.id)
    pm.ui(p.id, "n", 10)
    rt = pm.engine.runtimes[p.id]
    assert rt.sample("g2/g1/c", "result").value == 30 and rt.sample("g2/cmp", "out").value is True
    live = pm.live_snapshot(p.id)["values"]
    assert live["g2.out"]["v"] is True and live["g2.result"]["v"] == 30 and live["g2/g1.result"]["v"] == 30
