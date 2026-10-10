"""Библиотека групп нод: список, содержимое, правка изнутри, переименование, удаление."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from pioneer.engine.graph import validate
from pioneer.groups import GroupError, GroupManager, apply_group_ops

router = APIRouter(prefix="/groups")


def _gm(request: Request) -> GroupManager:
    return request.app.state.groups


def _guard(fn, *a):
    try:
        return fn(*a)
    except GroupError as e:
        raise HTTPException(e.code, str(e)) from e


@router.get("")
def list_groups(request: Request) -> list[dict]:
    return _gm(request).list()


@router.get("/{gid}")
def get_group(gid: str, request: Request) -> dict:
    gm = _gm(request)
    d = _guard(gm.get, gid)
    reg = gm.registry
    dyn = {nid: reg.for_node(n).schema() for nid, n in d.graph.nodes.items()
           if n.type in reg.types and reg.get(n.type).key in reg.dynamic}
    return {**d.to_json(), "type": d.key, "node_types": dyn,
            "validation": validate(d.graph, reg, request.app.state.board).to_json()}


class Ops(BaseModel):
    ops: list[dict[str, Any]]
    rev: int | None = None


@router.post("/{gid}/ops")
def group_ops(gid: str, body: Ops, request: Request) -> dict:
    return _guard(apply_group_ops, _gm(request), gid, body.ops, body.rev, request.app.state.board)


class Rename(BaseModel):
    name: str


@router.put("/{gid}")
def rename_group(gid: str, body: Rename, request: Request) -> dict:
    return _guard(_gm(request).rename, gid, body.name).to_json()


@router.delete("/{gid}")
def delete_group(gid: str, request: Request) -> dict:
    gm = _gm(request)
    d = _guard(gm.get, gid)
    used = [p["name"] for p in request.app.state.projects.list()
            if any(n["type"] == d.key for n in request.app.state.projects.get(p["id"])["graph"]["nodes"])]
    if used:
        raise HTTPException(409, f"группа используется в проектах: {', '.join(used)}")
    gm.delete(gid)
    return {"deleted": gid}
