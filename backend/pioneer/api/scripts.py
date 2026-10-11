"""Скрипты: CRUD, запуск/остановка, лог."""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from pioneer.scripts import ScriptError, ScriptManager
from pioneer.codetree import TreeError

router = APIRouter(prefix="/scripts")


def _sm(request: Request) -> ScriptManager:
    return request.app.state.scripts


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (ScriptError, TreeError) as e:
        code = 404 if str(e).startswith("нет скрипта") else 409
        raise HTTPException(code, str(e)) from e


@router.get("")
def list_scripts(request: Request) -> list[dict]:
    return _sm(request).list()


class CreateScript(BaseModel):
    name: str
    code: str | None = None


@router.post("")
def create_script(body: CreateScript, request: Request) -> dict:
    return asdict(_sm(request).create(body.name, body.code))


@router.get("/{sid}")
def get_script(sid: str, request: Request) -> dict:
    sm = _sm(request)
    meta = _guard(sm.meta, sid)
    return {**asdict(meta), "code": sm.code(sid), "status": asdict(sm.status(sid)), "log": sm.log_tail(sid)}


class UpdateScript(BaseModel):
    name: str | None = None
    code: str | None = None
    autostart: bool | None = None
    entry: str | None = None
    restart_on_save: bool | None = None


@router.put("/{sid}")
def update_script(sid: str, body: UpdateScript, request: Request) -> dict:
    return asdict(_guard(_sm(request).update, sid, name=body.name, code=body.code, autostart=body.autostart, entry=body.entry,
                          restart_on_save=body.restart_on_save))


@router.delete("/{sid}")
def delete_script(sid: str, request: Request) -> dict:
    _guard(_sm(request).meta, sid)
    _sm(request).delete(sid)
    return {"deleted": sid}


@router.post("/{sid}/start")
def start_script(sid: str, request: Request, file: str | None = None) -> dict:
    return asdict(_guard(_sm(request).start, sid, file))


@router.post("/{sid}/stop")
def stop_script(sid: str, request: Request) -> dict:
    sm = _sm(request)
    _guard(sm.meta, sid)
    sm.stop(sid)
    return asdict(sm.status(sid))


@router.post("/{sid}/restart")
def restart_script(sid: str, request: Request) -> dict:
    return asdict(_guard(_sm(request).restart, sid))
