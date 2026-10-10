"""Проекты и типы нод: CRUD, пакеты операций (spec 3.7a), запуск, live, UI-контролы, паника."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from pioneer.projects import ProjectError, ProjectManager

router = APIRouter()


def _pm(request: Request) -> ProjectManager:
    return request.app.state.projects


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ProjectError as e:
        if e.detail is not None:
            raise HTTPException(e.code, {"message": str(e), **(e.detail if isinstance(e.detail, dict) else {})}) from e
        raise HTTPException(e.code, str(e)) from e


@router.get("/node-types")
def node_types(request: Request) -> dict:
    from pioneer.engine.types import SOCKET_COLORS
    return {"types": _pm(request).registry.schemas(), "socket_colors": SOCKET_COLORS}


@router.get("/projects")
def list_projects(request: Request) -> list[dict]:
    return _pm(request).list()


class CreateProject(BaseModel):
    name: str
    description: str = ""
    graph: dict | None = None


@router.post("/projects")
def create_project(body: CreateProject, request: Request) -> dict:
    return asdict(_guard(_pm(request).create, body.name, body.description, body.graph))


@router.get("/projects/{pid}")
def get_project(pid: str, request: Request) -> dict:
    pm = _pm(request)
    return {**_guard(pm.get, pid), "log": pm.log_tail(pid, 200)}


class UpdateProject(BaseModel):
    name: str | None = None
    description: str | None = None
    autostart: bool | None = None


@router.put("/projects/{pid}")
def update_project(pid: str, body: UpdateProject, request: Request) -> dict:
    return asdict(_guard(_pm(request).update_meta, pid, name=body.name, description=body.description,
                         autostart=body.autostart))


@router.delete("/projects/{pid}")
def delete_project(pid: str, request: Request) -> dict:
    _guard(_pm(request).delete, pid)
    return {"deleted": pid}


@router.post("/projects/{pid}/duplicate")
def duplicate_project(pid: str, request: Request) -> dict:
    return asdict(_guard(_pm(request).duplicate, pid))


class Ops(BaseModel):
    ops: list[dict[str, Any]]
    rev: int | None = None  # ревизия, на которой построен пакет; иная — 409


@router.post("/projects/{pid}/ops")
def apply_ops(pid: str, body: Ops, request: Request) -> dict:
    return _guard(_pm(request).apply_ops, pid, body.ops, body.rev)


@router.post("/projects/{pid}/ops/validate")
def validate_ops(pid: str, body: Ops, request: Request) -> dict:
    return _guard(_pm(request).validate_ops, pid, body.ops)


@router.post("/projects/{pid}/start")
def start_project(pid: str, request: Request) -> dict:
    return _guard(_pm(request).start, pid)


@router.post("/projects/{pid}/stop")
def stop_project(pid: str, request: Request) -> dict:
    _guard(_pm(request)._read, pid)
    return _pm(request).stop(pid)


@router.post("/projects/{pid}/apply")
def apply_project(pid: str, request: Request) -> dict:
    """Горячий перезапуск с текущей ревизией графа (spec 4.2)."""
    return _guard(_pm(request).restart, pid)


@router.get("/projects/{pid}/live")
def project_live(pid: str, request: Request) -> dict:
    _guard(_pm(request)._read, pid)
    return _pm(request).live_snapshot(pid)


@router.get("/projects/{pid}/log")
def project_log(pid: str, request: Request) -> list[dict]:
    return _pm(request).log_tail(pid)


class UiValue(BaseModel):
    value: Any = None


@router.post("/projects/{pid}/nodes/{nid}/ui")
def ui_value(pid: str, nid: str, body: UiValue, request: Request) -> dict:
    _guard(_pm(request).ui, pid, nid, body.value)
    return {"ok": True}


@router.post("/panic")
def panic(request: Request) -> JSONResponse:
    """Остановить все проекты и скрипты: выходы — в безопасное состояние (spec 6)."""
    _pm(request).stop_all()
    request.app.state.scripts.stop_all()
    return JSONResponse({"stopped": True})
