"""Проекты и типы нод: CRUD, пакеты операций (spec 3.7a), запуск, live, UI-контролы, паника."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from pioneer.projects import ProjectError, ProjectManager
from pioneer.engine.node import NodeError
from pioneer.images import image_type

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


@router.get("/project-templates")
def templates():
    from pioneer.templates import TEMPLATES
    return {"templates": TEMPLATES}


@router.get("/projects/{pid}")
def get_project(pid: str, request: Request) -> dict:
    pm = _pm(request)
    return {**_guard(pm.get, pid), "log": pm.log_tail(pid, 200)}


class UpdateProject(BaseModel):
    name: str | None = None
    description: str | None = None
    autostart: bool | None = None
    save_state: bool | None = None


@router.put("/projects/{pid}")
def update_project(pid: str, body: UpdateProject, request: Request) -> dict:
    return asdict(_guard(_pm(request).update_meta, pid, name=body.name, description=body.description,
                         autostart=body.autostart, save_state=body.save_state))


@router.get("/projects/{pid}/export")
def export_project(pid: str, request: Request) -> JSONResponse:
    pm = _pm(request)
    meta, graph = _guard(pm._read, pid)
    flat, _, _ = pm._flat(graph)
    body = {"format": "pioneer-project", "version": 1,
            "name": meta.name, "description": meta.description, "graph": flat.to_json(),
            "required_types": sorted({n.type for n in flat.nodes.values()})}
    return JSONResponse(body, headers={"Content-Disposition": f'attachment; filename="pioneer-{pid}.json"'})


@router.post("/projects/import")
async def import_project(request: Request) -> dict:
    # Bound before JSON decode (nginx permits larger uploads for CodeTree ZIPs).
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 1024 * 1024:
            raise HTTPException(413, "граф больше 1 МБ")
    import json
    try:
        body = json.loads(raw)
        if body.get("format") != "pioneer-project" or body.get("version") != 1:
            raise ValueError("формат")
        graph = body["graph"]
        if not isinstance(graph, dict) or len(graph.get("nodes", [])) > 500 or len(graph.get("edges", [])) > 2000:
            raise ValueError("слишком большой граф")
        ids = [n["id"] for n in graph.get("nodes", [])]
        if len(set(ids)) != len(ids):
            raise ValueError("повторяющиеся id")
        name, description = body["name"], body.get("description", "")
        if not isinstance(name, str) or not isinstance(description, str):
            raise ValueError("метаданные")
        created = _pm(request).create(name, description, graph)
    except ProjectError as e:
        raise HTTPException(e.code, str(e)) from None
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(422, "неверный файл проекта") from None
    return asdict(created)  # always new id, stopped; no autostart or checkpoint imported


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


@router.get("/projects/{pid}/image")
def project_image(pid: str, node: str, request: Request, output: str = "image") -> Response:
    pm = _pm(request)
    _guard(pm._read, pid)

    def latest():
        rt = pm.engine.runtimes.get(pid)
        if rt is None:
            return None
        key = f"{node}.{output}"
        # Выход группы в live — псевдоним выхода её внутренней ноды.
        key = next((src for src, aliases in rt.aliases.items() if key in aliases), key)
        sample = rt.sample(*key.rsplit(".", 1))
        return sample.value if sample else None

    raw = pm.engine.call(latest)
    try:
        ctype = image_type(raw)
    except NodeError:
        raise HTTPException(404, "нет изображения") from None
    return Response(raw, media_type=ctype, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/projects/{pid}/images/{name}")
def saved_image(pid: str, name: str, request: Request) -> FileResponse:
    pm = _pm(request)
    _guard(pm._read, pid)
    try:
        path = pm.engine.images.path(pid, name)
    except (NodeError, FileNotFoundError):
        raise HTTPException(404, "нет изображения") from None
    return FileResponse(path, media_type="image/png" if path.suffix == ".png" else "image/jpeg",
                        filename=name, headers={"X-Content-Type-Options": "nosniff"})


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


@router.get("/projects/{pid}/bundle.zip")
def bundle_export(pid:str, request:Request):
    import tempfile
    from pathlib import Path
    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask
    from pioneer.project_bundle import export_to
    from pioneer.codetree import TreeError
    with tempfile.NamedTemporaryFile(prefix='pioneer-bundle-',suffix='.zip',delete=False) as f:path=Path(f.name)
    try:export_to(request.app,pid,path)
    except (ProjectError,TreeError,KeyError,OSError) as e:
        path.unlink(missing_ok=True);raise HTTPException(422,str(e)) from None
    return FileResponse(path,media_type='application/zip',filename=f'pioneer-{pid}.zip',background=BackgroundTask(path.unlink,missing_ok=True))

@router.post("/projects/import.zip")
async def bundle_import(request:Request, confirmed:bool=False):
    import asyncio,tempfile,zipfile
    from pathlib import Path
    from pioneer.api.code import upload_to
    from pioneer.codetree import TREE_LIMIT,TreeError
    from pioneer.project_bundle import import_bundle
    if not confirmed:raise HTTPException(422,'подтвердите доверие импортируемому Python-коду')
    with tempfile.TemporaryDirectory(dir=request.app.state.cfg.data_dir) as tmp:
        data=await upload_to(request,Path(tmp)/'project.zip',TREE_LIMIT)
        try:return asdict(await asyncio.to_thread(import_bundle,request.app,data,confirmed))
        except (OSError,ValueError,KeyError,TypeError,ProjectError,zipfile.BadZipFile,TreeError):
            raise HTTPException(422,"пакет повреждён, конфликтует с установленным кодом или не подтверждено доверие") from None
