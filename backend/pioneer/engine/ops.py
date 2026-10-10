"""Изменение графа пакетами операций (spec 3.7a).

    apply_ops(graph, [{"op": "add_node", "type": "builtin.and", "position": [0, 0]}, ...], registry)
        -> (новый граф, обратный пакет для undo, id созданных нод)

Операции: add_node, remove_node, set_param, set_input_value, connect, disconnect, move,
rename, collapse (+ group/ungroup — с Node Groups в фазе 2). Каждая операция проверяется
структурно при применении; граф целиком (типы, циклы, пины) — validate() после пакета.
Пакет атомарен: ошибка в любой операции — исходный граф не меняется.
"""
from __future__ import annotations

import secrets
from typing import Any

from pioneer.board import Board
from pioneer.engine.graph import GEdge, GNode, Graph, check_param
from pioneer.engine.node import NodeRegistry
from pioneer.engine.types import can_connect, check_value

OPS = ("add_node", "remove_node", "set_param", "set_input_value", "connect", "disconnect", "move",
       "rename", "collapse", "group", "ungroup")
MAX_OPS = 500


class OpError(ValueError):
    def __init__(self, index: int, message: str, node: str | None = None):
        super().__init__(f"операция {index + 1}: {message}")
        self.index, self.node, self.text = index, node, message

    def to_json(self) -> dict:
        d = {"op_index": self.index, "message": self.text}
        if self.node:
            d["node"] = self.node
        return d


def _pos(v: Any) -> list[float]:
    if (not isinstance(v, (list, tuple)) or len(v) != 2
            or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)):
        raise ValueError("position: ожидается [x, y]")
    return [float(v[0]), float(v[1])]


def _sock(v: Any, what: str) -> tuple[str, str]:
    if not isinstance(v, (list, tuple)) or len(v) != 2 or not all(isinstance(x, str) for x in v):
        raise ValueError(f"{what}: ожидается [нода, сокет]")
    return v[0], v[1]


def new_node_id(graph: Graph) -> str:
    while (nid := "n" + secrets.token_hex(3)) in graph.nodes:
        pass
    return nid


def apply_ops(graph: Graph, ops: list[dict], registry: NodeRegistry, board: Board | None = None,
              groups=None) -> tuple[Graph, list[dict], list[str]]:
    """groups — GroupManager (операции group/ungroup); без него они недоступны."""
    if not isinstance(ops, list) or not ops:
        raise OpError(0, "пустой пакет операций")
    if len(ops) > MAX_OPS:
        raise OpError(0, f"больше {MAX_OPS} операций в пакете")
    g = graph.copy()
    inverse: list[list[dict]] = []
    created: list[str] = []
    for i, op in enumerate(ops):
        try:
            if not isinstance(op, dict) or op.get("op") not in OPS:
                raise ValueError(f"неизвестная операция {op.get('op') if isinstance(op, dict) else op!r}; "
                                 f"есть: {', '.join(OPS)}")
            fn = _APPLY[op["op"]]
            inv = fn(g, op, registry, board, created, groups) if fn in (_group, _ungroup) else \
                fn(g, op, registry, board, created)
        except OpError:
            raise
        except (ValueError, KeyError) as e:
            node = op.get("node") or op.get("id") if isinstance(op, dict) else None
            raise OpError(i, str(e.args[0]) if e.args else str(e), node if isinstance(node, str) else None) from None
        inverse.append(inv)
    return g, [o for inv in reversed(inverse) for o in inv], created


def _node(g: Graph, nid: Any) -> GNode:
    if not isinstance(nid, str) or nid not in g.nodes:
        raise ValueError(f"нет ноды {nid!r}")
    return g.nodes[nid]


def _add_node(g: Graph, op: dict, reg: NodeRegistry, board, created) -> list[dict]:
    nt = reg.resolve(op["type"])
    nid = op.get("id") or new_node_id(g)
    if not isinstance(nid, str) or not nid.replace("_", "").replace("-", "").isalnum() or len(nid) > 40 \
            or not nid.isascii():
        raise ValueError(f"плохой id ноды {nid!r}")
    if nid in g.nodes:
        raise ValueError(f"нода {nid} уже есть")
    params = {p.name: p.default for p in nt.params.values()}
    for k, v in (op.get("params") or {}).items():
        p = nt.params.get(k)
        if p is None:
            raise ValueError(f"у {nt.title} нет параметра {k!r}")
        params[k] = check_param(p, v, board)
    inputs = {}
    sockets = reg.dynamic[nt.key](params) if nt.key in reg.dynamic else nt  # Скрипт: входы — из кода
    for k, v in (op.get("inputs") or {}).items():
        i = sockets.inputs.get(k)
        if i is None:
            raise ValueError(f"у {nt.title} нет входа {k!r}")
        inputs[k] = check_value(v, i.type)
    label = op.get("label")
    if label is not None and not isinstance(label, str):
        raise ValueError("label: ожидается строка")
    g.nodes[nid] = GNode(id=nid, type=nt.key, position=_pos(op.get("position", [0, 0])), params=params,
                         inputs=inputs, collapsed=bool(op.get("collapsed", False)), label=label or None)
    created.append(nid)
    return [{"op": "remove_node", "id": nid}]


def _remove_node(g: Graph, op: dict, reg, board, created) -> list[dict]:
    n = _node(g, op.get("id", op.get("node")))
    edges = [e for e in g.edges if n.id in (e.src[0], e.dst[0])]
    g.edges = [e for e in g.edges if e not in edges]
    del g.nodes[n.id]
    return [{"op": "add_node", **n.to_json()}] + [{"op": "connect", **e.to_json()} for e in edges]


def _set_param(g: Graph, op: dict, reg: NodeRegistry, board, created) -> list[dict]:
    n = _node(g, op.get("node"))
    nt = reg.for_node(n)
    p = nt.params.get(op.get("name"))
    if p is None:
        raise ValueError(f"у {nt.title} нет параметра {op.get('name')!r}")
    old = n.params.get(p.name, p.default)
    n.params[p.name] = check_param(p, op.get("value"), board)
    inverse = [{"op": "set_param", "node": n.id, "name": p.name, "value": old}]
    if reg.get(n.type).key in reg.dynamic:
        # сокеты Скрипта зависят от кода: провода к исчезнувшим сокетам снимаются (и вернутся при undo)
        new = reg.for_node(n)
        gone = [e for e in g.edges if (e.dst[0] == n.id and e.dst[1] not in new.inputs)
                or (e.src[0] == n.id and e.src[1] not in new.outputs)]
        g.edges = [e for e in g.edges if e not in gone]
        for k in [k for k in n.inputs if k not in new.inputs]:
            inverse.append({"op": "set_input_value", "node": n.id, "name": k, "value": n.inputs.pop(k)})
        inverse += [{"op": "connect", **e.to_json()} for e in gone]
    return inverse


def _set_input_value(g: Graph, op: dict, reg: NodeRegistry, board, created) -> list[dict]:
    n = _node(g, op.get("node"))
    nt = reg.for_node(n)
    i = nt.inputs.get(op.get("name"))
    if i is None:
        raise ValueError(f"у {nt.title} нет входа {op.get('name')!r}")
    old = n.inputs.get(i.name)
    if op.get("value") is None:
        n.inputs.pop(i.name, None)
    else:
        n.inputs[i.name] = check_value(op["value"], i.type)
    return [{"op": "set_input_value", "node": n.id, "name": i.name, "value": old}]


def _connect(g: Graph, op: dict, reg: NodeRegistry, board, created) -> list[dict]:
    src, dst = _sock(op.get("from"), "from"), _sock(op.get("to"), "to")
    a, b = reg.for_node(_node(g, src[0])), reg.for_node(_node(g, dst[0]))
    out, inp = a.outputs.get(src[1]), b.inputs.get(dst[1])
    if out is None:
        raise ValueError(f"у {a.title} нет выхода {src[1]!r}")
    if inp is None:
        raise ValueError(f"у {b.title} нет входа {dst[1]!r}")
    if not can_connect(out.type, inp.type):
        raise ValueError(f"нельзя соединить {out.type} ({a.title}.{src[1]}) → {inp.type} ({b.title}.{dst[1]})")
    old = g.edge_to(*dst)
    if old and old.src == src:
        return []
    inv: list[dict] = [{"op": "disconnect", "to": list(dst)}]
    if old:  # провод на занятый вход заменяет старый (как в Blender)
        g.edges.remove(old)
        inv.append({"op": "connect", **old.to_json()})
    g.edges.append(GEdge(src, dst))
    return inv


def _disconnect(g: Graph, op: dict, reg, board, created) -> list[dict]:
    dst = _sock(op.get("to"), "to")
    e = g.edge_to(*dst)
    if e is None or (op.get("from") is not None and e.src != _sock(op["from"], "from")):
        raise ValueError(f"нет провода к {dst[0]}.{dst[1]}")
    g.edges.remove(e)
    return [{"op": "connect", **e.to_json()}]


BOARD = "@board"  # Board-нода: одна на проект, не удаляется; живёт в graph.view


def _view_node(g: Graph) -> dict:
    return g.view.setdefault("board", {"position": [-420.0, 0.0], "collapsed": False})


def _move(g: Graph, op: dict, reg, board, created) -> list[dict]:
    if op.get("node") == BOARD:
        v = _view_node(g)
        old, v["position"] = v["position"], _pos(op.get("position"))
        return [{"op": "move", "node": BOARD, "position": old}]
    n = _node(g, op.get("node"))
    old = n.position
    n.position = _pos(op.get("position"))
    return [{"op": "move", "node": n.id, "position": old}]


def _rename(g: Graph, op: dict, reg, board, created) -> list[dict]:
    n = _node(g, op.get("node"))
    label = op.get("label")
    if label is not None and not isinstance(label, str):
        raise ValueError("label: ожидается строка")
    old, n.label = n.label, (label or "").strip()[:80] or None
    return [{"op": "rename", "node": n.id, "label": old}]


def _collapse(g: Graph, op: dict, reg, board, created) -> list[dict]:
    if op.get("node") == BOARD:
        v = _view_node(g)
        old, v["collapsed"] = v["collapsed"], bool(op.get("collapsed", True))
        return [{"op": "collapse", "node": BOARD, "collapsed": old}]
    n = _node(g, op.get("node"))
    old, n.collapsed = n.collapsed, bool(op.get("collapsed", True))
    return [{"op": "collapse", "node": n.id, "collapsed": old}]


def _group(g: Graph, op: dict, reg, board, created, groups) -> list[dict]:
    """{"op": "group", "nodes": [...], "name": "…"} — выделение → нода-группа (spec 3.6 a)."""
    if groups is None:
        raise ValueError("группы здесь недоступны")
    ids = op.get("nodes")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        raise ValueError("nodes: список id нод")
    ids = list(dict.fromkeys(ids))
    for nid in ids:
        _node(g, nid)
    sel = set(ids)
    touched = [e for e in g.edges if e.src[0] in sel or e.dst[0] in sel]
    originals = [g.nodes[i] for i in ids]
    d, plan = groups.create_from(g, ids, op.get("name") or "Группа")
    gid = op.get("id") or new_node_id(g)
    cx = sum(n.position[0] for n in originals) / len(originals)
    cy = sum(n.position[1] for n in originals) / len(originals)
    g.edges = [e for e in g.edges if e not in touched]
    for i in ids:
        del g.nodes[i]
    g.nodes[gid] = GNode(id=gid, type=d.key, position=[round(cx), round(cy)])
    for src, name in plan["in"]:
        g.edges.append(GEdge(src, (gid, name)))
    for name, dst in plan["out"]:
        g.edges.append(GEdge((gid, name), dst))
    created.append(gid)
    return ([{"op": "remove_node", "id": gid}] + [{"op": "add_node", **n.to_json()} for n in originals]
            + [{"op": "connect", **e.to_json()} for e in touched])


def _ungroup(g: Graph, op: dict, reg, board, created, groups) -> list[dict]:
    """{"op": "ungroup", "node": id} — содержимое группы вставляется в граф."""
    if groups is None:
        raise ValueError("группы здесь недоступны")
    n = _node(g, op.get("node"))
    d = groups.of_type(n.type)
    if d is None:
        raise ValueError(f"{n.id} — не группа")
    touched = [e for e in g.edges if n.id in (e.src[0], e.dst[0])]
    inner = d.graph
    xs = [m.position[0] for m in inner.nodes.values()] or [0]
    ys = [m.position[1] for m in inner.nodes.values()] or [0]
    dx, dy = n.position[0] - sum(xs) / len(xs), n.position[1] - sum(ys) / len(ys)
    ids: dict[str, str] = {}
    for iid, m in inner.nodes.items():
        new = f"{n.id}_{iid}"
        while new in g.nodes:
            new += "_"
        ids[iid] = new
        g.nodes[new] = GNode(id=new, type=m.type, position=[round(m.position[0] + dx), round(m.position[1] + dy)],
                             params=dict(m.params), inputs=dict(m.inputs), collapsed=m.collapsed, label=m.label)
        created.append(new)
    g.edges = [e for e in g.edges if e not in touched]
    g.edges += [GEdge((ids[e.src[0]], e.src[1]), (ids[e.dst[0]], e.dst[1])) for e in inner.edges]
    ins = {i["name"]: i for i in d.inputs}
    outs = {o["name"]: o for o in d.outputs}
    for name, value in n.inputs.items():
        for t in ins.get(name, {}).get("targets", []):
            g.nodes[ids[t[0]]].inputs[t[1]] = value
    for e in touched:
        if e.dst[0] == n.id and e.dst[1] in ins:
            for t in ins[e.dst[1]]["targets"]:
                g.edges.append(GEdge(e.src, (ids[t[0]], t[1])))
        elif e.src[0] == n.id and e.src[1] in outs:
            s = outs[e.src[1]]["source"]
            g.edges.append(GEdge((ids[s[0]], s[1]), e.dst))
    del g.nodes[n.id]
    return ([{"op": "remove_node", "id": i} for i in ids.values()] + [{"op": "add_node", **n.to_json()}]
            + [{"op": "connect", **e.to_json()} for e in touched])


_APPLY = {"add_node": _add_node, "remove_node": _remove_node, "set_param": _set_param,
          "set_input_value": _set_input_value, "connect": _connect, "disconnect": _disconnect,
          "move": _move, "rename": _rename, "collapse": _collapse, "group": _group, "ungroup": _ungroup}
