"""Node Groups (spec 3.6 a): кусок графа → своя нода, библиотека групп, развёртка при запуске.

data/groups/<id>.json = {id, name, rev, graph, inputs: [{name, title, type, targets: [[нода, сокет]…]}],
outputs: [{name, title, type, source: [нода, сокет]}]}. Тип в палитре — `group.<id>@1`.

Интерфейс группы получается из выделения: провода, входящие в выделение снаружи, —
её входы, выходящие — выходы. Движок про группы не знает: перед запуском граф проекта
разворачивается (flatten) — ноды группы получают id `<нода-группы>/<внутренний id>`, а живые
значения внутренних выходов, выведенных наружу, дублируются под ключом выхода группы.
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pioneer.engine.graph import GEdge, GNode, Graph
from pioneer.engine.node import In, Node, NodeRegistry, Out, node

PREFIX = "group."
MAX_DEPTH = 5


class GroupError(ValueError):
    def __init__(self, message: str, code: int = 409):
        super().__init__(message)
        self.code = code


class GroupNode(Node):
    """Заглушка: группы разворачиваются до запуска и в движок не попадают."""

    def setup(self):
        raise RuntimeError("группа должна быть развёрнута перед запуском")


@dataclass
class GroupDef:
    id: str
    name: str
    graph: Graph
    inputs: list[dict] = field(default_factory=list)
    outputs: list[dict] = field(default_factory=list)
    rev: int = 1
    updated: float = field(default_factory=time.time)

    @property
    def key(self) -> str:
        return f"{PREFIX}{self.id}@1"

    def to_json(self) -> dict:
        return {"id": self.id, "name": self.name, "rev": self.rev, "updated": self.updated,
                "graph": self.graph.to_json(), "inputs": self.inputs, "outputs": self.outputs}

    @classmethod
    def from_json(cls, raw: dict) -> GroupDef:
        return cls(id=raw["id"], name=raw["name"], graph=Graph.from_json(raw["graph"]), inputs=raw.get("inputs", []),
                   outputs=raw.get("outputs", []), rev=raw.get("rev", 1), updated=raw.get("updated", time.time()))


def _uniq(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9_]", "_", name.lower()) or "s"
    if not re.match(r"^[a-z]", base):
        base = "s_" + base
    n, i = base, 2
    while n in taken:
        n, i = f"{base}_{i}", i + 1
    taken.add(n)
    return n


class GroupManager:
    def __init__(self, data_dir: Path, registry: NodeRegistry):
        self.dir = data_dir / "groups"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.registry = registry
        self.defs: dict[str, GroupDef] = {}
        self._lock = threading.RLock()
        for f in sorted(self.dir.glob("*.json")):
            try:
                self._register(GroupDef.from_json(json.loads(f.read_text())))
            except (ValueError, KeyError, TypeError) as e:
                import logging
                logging.getLogger(__name__).error("группа %s не загружена: %s", f.name, e)

    # ---- библиотека
    def get(self, gid: str) -> GroupDef:
        if gid not in self.defs:
            raise GroupError(f"нет группы {gid}", 404)
        return self.defs[gid]

    def of_type(self, type_key: str) -> GroupDef | None:
        if not type_key.startswith(PREFIX):
            return None
        return self.defs.get(type_key[len(PREFIX):].split("@")[0])

    def list(self) -> list[dict]:
        return sorted(({"id": d.id, "name": d.name, "rev": d.rev, "type": d.key, "nodes": len(d.graph.nodes)}
                       for d in self.defs.values()), key=lambda d: d["name"].lower())

    def save(self, d: GroupDef) -> None:
        with self._lock:
            d.updated = time.time()
            tmp = self.dir / f"{d.id}.tmp"
            tmp.write_text(json.dumps(d.to_json(), ensure_ascii=False, indent=1))
            tmp.replace(self.dir / f"{d.id}.json")
            self._register(d)

    def rename(self, gid: str, name: str) -> GroupDef:
        d = self.get(gid)
        d.name = name.strip() or d.name
        d.rev += 1
        self.save(d)
        return d

    def delete(self, gid: str) -> None:
        with self._lock:
            d = self.get(gid)
            self.registry.remove(d.key)
            self.defs.pop(gid, None)
            (self.dir / f"{gid}.json").unlink(missing_ok=True)

    def _register(self, d: GroupDef) -> None:
        self.registry.remove(d.key)
        attrs: dict[str, Any] = {}
        for i in d.inputs:
            attrs[i["name"]] = In(i["type"], i.get("default"), title=i.get("title"))
        for o in d.outputs:
            attrs[o["name"]] = Out(o["type"], title=o.get("title"))
        cls = type(f"Group_{d.id}", (GroupNode,), attrs)
        doc = (f"Группа нод «{d.name}» ({len(d.graph.nodes)} нод). Tab или «Открыть группу» — редактировать "
               f"содержимое; изменения действуют во всех проектах с этой группой (после «Применить»).")
        node(PREFIX + d.id, title=d.name, category="Группы", color="#4b5d6b", doc=doc, registry=self.registry)(cls)
        self.defs[d.id] = d

    # ---- создание из выделения (операция group)
    def create_from(self, graph: Graph, ids: list[str], name: str) -> tuple[GroupDef, dict]:
        """Новая группа из нод ids графа. → (группа, план замены: внешние провода к интерфейсу)."""
        sel = set(ids)
        if len(sel) < 1 or not sel <= graph.nodes.keys():
            raise GroupError("выделите ноды, которые есть в графе")
        inner = Graph()
        for nid in ids:
            inner.nodes[nid] = GNode(**{**graph.nodes[nid].__dict__})
        inner.edges = [e for e in graph.edges if e.src[0] in sel and e.dst[0] in sel]
        taken_in: set[str] = set()
        taken_out: set[str] = set()
        inputs, outputs = [], []
        incoming = [e for e in graph.edges if e.src[0] not in sel and e.dst[0] in sel]
        outgoing = [e for e in graph.edges if e.src[0] in sel and e.dst[0] not in sel]
        outer_in: list[tuple[tuple[str, str], str]] = []  # (внешний источник, имя входа группы)
        for e in incoming:
            nt = self.registry.for_node(graph.nodes[e.dst[0]])
            s = nt.inputs[e.dst[1]]
            name_ = _uniq(e.dst[1], taken_in)
            inputs.append({"name": name_, "title": s.title or e.dst[1], "type": s.type, "default": s.default,
                           "targets": [list(e.dst)]})
            outer_in.append((e.src, name_))
        out_by_src: dict[tuple[str, str], str] = {}
        outer_out: list[tuple[str, tuple[str, str]]] = []  # (имя выхода, внешний приёмник)
        for e in outgoing:
            if e.src not in out_by_src:
                nt = self.registry.for_node(graph.nodes[e.src[0]])
                s = nt.outputs[e.src[1]]
                out_by_src[e.src] = n = _uniq(e.src[1], taken_out)
                outputs.append({"name": n, "title": s.title or e.src[1], "type": s.type, "source": list(e.src)})
            outer_out.append((out_by_src[e.src], e.dst))
        d = GroupDef(id=secrets.token_hex(4), name=name.strip() or "Группа", graph=inner, inputs=inputs, outputs=outputs)
        self.save(d)
        return d, {"in": outer_in, "out": outer_out}

    # ---- развёртка
    def flatten(self, graph: Graph) -> tuple[Graph, dict[str, list[str]], dict[str, str]]:
        """→ (плоский граф, алиасы live-ключей «внутр.нода.сокет» → «группа.выход»,
        хозяин: плоская нода → нода верхнего уровня)."""
        flat, aliases, owner, _, _ = self._flatten(graph, 0)
        return flat, aliases, owner

    def _flatten(self, graph: Graph, depth: int):
        if depth > MAX_DEPTH:
            raise GroupError(f"группы вложены глубже {MAX_DEPTH} уровней (или группа содержит саму себя)")
        flat = Graph(view=graph.view)
        aliases: dict[str, list[str]] = {}
        owner: dict[str, str] = {}
        in_map: dict[tuple[str, str], list[tuple[str, str]]] = {}
        out_map: dict[tuple[str, str], tuple[str, str]] = {}
        for n in graph.nodes.values():
            d = self.of_type(n.type)
            if d is None:
                flat.nodes[n.id] = n
                owner[n.id] = n.id
                continue
            inner, inner_alias, _, inner_in, inner_out = self._flatten(d.graph, depth + 1)
            p = n.id + "/"
            for inid, inode in inner.nodes.items():
                flat.nodes[p + inid] = GNode(**{**inode.__dict__, "id": p + inid, "inputs": dict(inode.inputs)})
                owner[p + inid] = n.id
            flat.edges += [GEdge((p + e.src[0], e.src[1]), (p + e.dst[0], e.dst[1])) for e in inner.edges]
            for k, vs in inner_alias.items():
                aliases.setdefault(p + k, []).extend(p + v for v in vs)
            for i in d.inputs:
                targets = []
                for t in i["targets"]:  # цель может быть входом вложенной группы
                    targets += [(p + a, b) for a, b in inner_in.get(tuple(t), [tuple(t)])]
                in_map[(n.id, i["name"])] = targets
                if i["name"] in n.inputs:  # inline-значение входа группы → во внутренние входы
                    for t in targets:
                        flat.nodes[t[0]].inputs[t[1]] = n.inputs[i["name"]]
            for o in d.outputs:
                a, b = inner_out.get(tuple(o["source"]), tuple(o["source"]))
                src = (p + a, b)
                out_map[(n.id, o["name"])] = src
                aliases.setdefault(f"{src[0]}.{src[1]}", []).append(f"{n.id}.{o['name']}")
        for e in graph.edges:
            src = out_map.get(e.src, e.src)
            for dst in in_map.get(e.dst, [e.dst]):
                flat.edges.append(GEdge(src, dst))
        return flat, aliases, owner, in_map, out_map

    def used_revs(self, graph: Graph, acc: dict[str, int] | None = None) -> dict[str, int]:
        """Ревизии групп, используемых графом (рекурсивно) — для «граф изменён»."""
        acc = {} if acc is None else acc
        for n in graph.nodes.values():
            d = self.of_type(n.type)
            if d is not None and d.id not in acc:
                acc[d.id] = d.rev
                self.used_revs(d.graph, acc)
        return acc


def apply_group_ops(gm: GroupManager, gid: str, ops: list[dict], base_rev: int | None, board) -> dict:
    """Правка содержимого группы (Tab внутрь): те же операции, что у проекта. Ноды, на которые
    ссылается интерфейс группы, удалить нельзя (сначала — разгруппировать)."""
    from pioneer.engine.graph import validate
    from pioneer.engine.ops import OpError, apply_ops
    with gm._lock:
        d = gm.get(gid)
        if base_rev is not None and base_rev != d.rev:
            raise GroupError(f"группа изменена в другой вкладке (ревизия {d.rev}, у вас {base_rev})")
        try:
            new, inverse, created = apply_ops(d.graph, ops, gm.registry, board, None)
        except OpError as e:
            raise GroupError(str(e), 422) from None
        for i in d.inputs:
            for t in i["targets"]:
                n = new.nodes.get(t[0])
                if n is None or t[1] not in gm.registry.for_node(n).inputs:
                    raise GroupError(f"«{i['title']}» — вход группы ведёт в {t[0]}.{t[1]}: эту ноду/сокет не удалить "
                                     f"(разгруппируйте, измените и сгруппируйте заново)", 422)
        for o in d.outputs:
            n = new.nodes.get(o["source"][0])
            if n is None or o["source"][1] not in gm.registry.for_node(n).outputs:
                raise GroupError(f"«{o['title']}» — выход группы из {o['source'][0]}.{o['source'][1]}: "
                                 f"эту ноду/сокет не удалить", 422)
        rep = validate(new, gm.registry, board)
        if not rep.ok:
            raise GroupError(f"пакет отклонён: {rep.text()}", 422)
        d.graph, d.rev = new, d.rev + 1
        gm.save(d)
        return {"rev": d.rev, "inverse": inverse, "created": created, "validation": rep.to_json()}
