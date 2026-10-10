"""Удалённые команды и состояния проекта (idea 003), HTTP-транспорт.

POST /remote/cmd/<имя> {"id", "args", "ttl_ms"} → {"id", "ok", "result" | "error"}
GET  /remote/state/<имя>, GET /remote/describe. Ключ — секрет `remote_api_key`
(заголовок X-Pioneer-Key). Вызвать можно только команды, объявленные запущенными проектами.
"""
from __future__ import annotations

import asyncio
import hmac

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from pioneer.engine.node import NodeError

router = APIRouter(prefix="/remote")
KEY_SECRET = "remote_api_key"


def _auth(request: Request) -> JSONResponse | None:
    try:
        expected = request.app.state.secrets.resolve("{{secret:%s}}" % KEY_SECRET)
    except NodeError:
        return JSONResponse({"ok": False, "error": f"удалённый доступ выключен: задайте секрет «{KEY_SECRET}»"}, 403)
    given = request.headers.get("x-pioneer-key", "")
    if not hmac.compare_digest(given.encode(), expected.encode()):
        return JSONResponse({"ok": False, "error": "неверный ключ"}, 401)
    return None


def _nodes(request: Request, cls):
    for pid, rt in list(request.app.state.projects.engine.runtimes.items()):
        if rt.running:
            for node in rt.nodes.values():
                if isinstance(node, cls):
                    yield pid, rt, node


@router.get("/describe")
def describe(request: Request) -> JSONResponse:
    if (err := _auth(request)) is not None:
        return err
    return JSONResponse(request.app.state.remote.describe())


@router.get("/state/{name}")
def state(name: str, request: Request) -> JSONResponse:
    if (err := _auth(request)) is not None:
        return err
    st = request.app.state.remote.state(name)
    return JSONResponse(st) if st else JSONResponse({"ok": False, "error": f"нет состояния «{name}»"}, 404)


@router.post("/cmd/{name}")
async def command(name: str, request: Request) -> JSONResponse:
    if (err := _auth(request)) is not None:
        return err
    try:
        body = await request.json()
    except ValueError:
        body = None
    who = request.client.host if request.client else "?"
    resp, code = await asyncio.to_thread(request.app.state.remote.execute_command, name, body, "http", who)
    return JSONResponse(resp, code)
