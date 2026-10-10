"""Удалённые команды и состояния проекта (idea 003), HTTP-транспорт.

POST /remote/cmd/<имя> {"id", "args", "ttl_ms"} → {"id", "ok", "result" | "error"}
GET  /remote/state/<имя>, GET /remote/describe. Ключ — секрет `remote_api_key`
(заголовок X-Pioneer-Key). Вызвать можно только команды, объявленные запущенными проектами.
"""
from __future__ import annotations

import asyncio
import collections
import hmac
import threading
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from pioneer.engine.node import NodeError
from pioneer.engine.types import Origin, RequestContext
from pioneer.nodes.builtin.net import Command, State, check_args

router = APIRouter(prefix="/remote")
KEY_SECRET = "remote_api_key"
TIMEOUT_S = 5.0
_recent: collections.OrderedDict = collections.OrderedDict()  # (команда, id) -> ответ (дедупликация)
_RECENT_MAX = 200


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
    cmds = [{"name": n.name, "args": n.spec, "project": rt.name} for _, rt, n in _nodes(request, Command)]
    states = [{"name": n.name, **getattr(n, "current", {}), "project": rt.name} for _, rt, n in _nodes(request, State)]
    return JSONResponse({"commands": cmds, "states": states})


@router.get("/state/{name}")
def state(name: str, request: Request) -> JSONResponse:
    if (err := _auth(request)) is not None:
        return err
    for _, _, n in _nodes(request, State):
        if n.name == name:
            return JSONResponse({"name": name, **getattr(n, "current", {"value": None})})
    return JSONResponse({"ok": False, "error": f"нет состояния «{name}»"}, 404)


@router.post("/cmd/{name}")
async def command(name: str, request: Request) -> JSONResponse:
    if (err := _auth(request)) is not None:
        return err
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = {}
    rid = str(body.get("id", ""))[:64] or None
    if rid and (name, rid) in _recent:
        return JSONResponse(_recent[(name, rid)])
    found = next(((pid, rt, n) for pid, rt, n in _nodes(request, Command) if n.name == name), None)
    if found is None:
        return JSONResponse({"id": rid, "ok": False, "error": f"нет команды «{name}»"}, 404)
    pid, rt, node = found
    try:
        args = check_args(node.spec, body.get("args") or {})
    except ValueError as e:
        return JSONResponse({"id": rid, "ok": False, "error": str(e)}, 422)
    ttl = body.get("ttl_ms")
    timeout = TIMEOUT_S if not isinstance(ttl, (int, float)) else max(0.0, min(TIMEOUT_S, ttl / 1000))
    if timeout <= 0:
        return JSONResponse({"id": rid, "ok": False, "error": "expired"}, 408)

    done = threading.Event()
    ctx = RequestContext(transport="http", deadline=time.monotonic() + timeout,
                         meta={"command": name, "id": rid})
    for hooks in (ctx.on_reply, ctx.on_complete, ctx.on_timeout):
        hooks.append(lambda c: done.set())
    t0 = time.monotonic()
    request.app.state.projects.engine.inject_call(pid, Origin("remote", "http"), lambda: node.deliver(args, ctx),
                                                  ctx=ctx, node=node)
    await asyncio.to_thread(done.wait, timeout + 0.5)
    if ctx.replied:
        resp, code = {"id": rid, "ok": ctx.reply_status < 400, "result": ctx.reply_body}, 200
    elif ctx.done and not ctx.timed_out:
        resp, code = {"id": rid, "ok": True}, 200
    else:
        resp, code = {"id": rid, "ok": False, "error": "timeout"}, 504
    ms = (time.monotonic() - t0) * 1000
    who = request.client.host if request.client else "?"
    rt.engine.loop.call_soon_threadsafe(
        rt.log, f"команда {name} от {who}: {'ok' if resp['ok'] else resp.get('error', 'ошибка')}, {ms:.0f} мс")
    if rid:
        _recent[(name, rid)] = resp
        while len(_recent) > _RECENT_MAX:
            _recent.popitem(last=False)
    return JSONResponse(resp, code)
