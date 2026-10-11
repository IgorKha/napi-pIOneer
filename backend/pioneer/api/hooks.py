"""Входящие webhooks (нода «Входящий webhook»): публичный маршрут, защита — токен ноды."""
from __future__ import annotations

import hmac
import json
import threading
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from pioneer.api.code import limited_body
from pioneer.engine.node import NodeError
from pioneer.engine.types import Origin, RequestContext
from pioneer.nodes.builtin.net import WebhookIn

router = APIRouter()
REPLY_TIMEOUT_S = 5.0
MAX_BODY = 256 * 1024


def _find(request: Request, path: str):
    for pid, rt in list(request.app.state.projects.engine.runtimes.items()):
        for node in rt.nodes.values():
            if isinstance(node, WebhookIn) and node.path == path and rt.running:
                return pid, node
    return None, None


@router.post("/hooks/{path}")
async def hook(path: str, request: Request) -> JSONResponse:
    pid, node = _find(request, path)
    if node is None:
        return JSONResponse({"ok": False, "error": f"нет запущенного webhook «{path}»"}, 404)
    given = request.headers.get("x-pioneer-token") or ""
    try:
        expected = node.expected_token()
    except NodeError as e:
        return JSONResponse({"ok": False, "error": str(e)}, 500)
    if not expected or not hmac.compare_digest(given.encode(), expected.encode()):
        return JSONResponse({"ok": False, "error": "неверный токен"}, 401)
    raw = await limited_body(request, MAX_BODY)
    try:
        body = json.loads(raw) if raw.strip() else None
    except ValueError:
        body = raw.decode("utf-8", "replace")

    done = threading.Event()
    ctx = RequestContext(transport="http", deadline=time.monotonic() + REPLY_TIMEOUT_S,
                         meta={"path": path, "remote": request.client.host if request.client else None})
    ctx.on_reply.append(lambda c: done.set())
    ctx.on_complete.append(lambda c: done.set())
    ctx.on_timeout.append(lambda c: done.set())
    engine = request.app.state.projects.engine
    engine.inject_call(pid, Origin("remote", "http"), lambda: node.deliver(body, ctx), ctx=ctx, node=node)

    import asyncio
    await asyncio.to_thread(done.wait, REPLY_TIMEOUT_S + 0.5)
    if ctx.rejected:
        return JSONResponse({"ok": False, "error": ctx.rejected}, 409)
    if ctx.replied:
        return JSONResponse(ctx.reply_body, ctx.reply_status)
    if ctx.done and not ctx.timed_out:
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "error": f"граф не ответил за {REPLY_TIMEOUT_S:g} с"}, 504)
