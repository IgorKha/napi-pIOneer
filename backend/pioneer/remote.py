"""Удалённые команды и состояния (idea 003): общая часть для HTTP и MQTT.

execute_command — найти объявленную команду среди запущенных проектов, проверить
аргументы, начать волну с контекстом запроса и дождаться ответа «Ответ» / завершения /
таймаута. Блокирующий вызов — из потока транспорта, не из потока движка.
"""
from __future__ import annotations

import collections
import threading
import time
from collections.abc import Callable

from pioneer.engine.types import Origin, RequestContext
from pioneer.nodes.builtin.net import Command, State, check_args

TIMEOUT_S = 5.0
_RECENT_MAX = 200


class Remote:
    def __init__(self, projects):
        self.projects = projects
        self._recent: collections.OrderedDict = collections.OrderedDict()  # (команда, id) -> (ответ, код)
        self._lock = threading.Lock()
        self.state_listeners: list[Callable[[str, dict], None]] = []  # MQTT: публикация состояний

    def nodes(self, cls):
        for pid, rt in list(self.projects.engine.runtimes.items()):
            if rt.running:
                for node in rt.nodes.values():
                    if isinstance(node, cls):
                        yield pid, rt, node

    def describe(self) -> dict:
        return {"commands": [{"name": n.name, "args": n.spec, "project": rt.name} for _, rt, n in self.nodes(Command)],
                "states": [{"name": n.name, **getattr(n, "current", {}), "project": rt.name}
                           for _, rt, n in self.nodes(State)]}

    def state(self, name: str) -> dict | None:
        for _, _, n in self.nodes(State):
            if n.name == name:
                return {"name": name, **getattr(n, "current", {"value": None})}
        return None

    def execute_command(self, name: str, body: dict, transport: str, who: str = "?") -> tuple[dict, int]:
        """→ (ответ {"id", "ok", "result"|"error"}, HTTP-код)."""
        body = body if isinstance(body, dict) else {}
        rid = str(body.get("id", ""))[:64] or None
        with self._lock:
            if rid and (name, rid) in self._recent:
                return self._recent[(name, rid)]
        found = next(((pid, rt, n) for pid, rt, n in self.nodes(Command) if n.name == name), None)
        if found is None:
            return {"id": rid, "ok": False, "error": f"нет команды «{name}»"}, 404
        pid, rt, node = found
        try:
            args = check_args(node.spec, body.get("args") or {})
        except ValueError as e:
            return {"id": rid, "ok": False, "error": str(e)}, 422
        ttl = body.get("ttl_ms")
        timeout = TIMEOUT_S if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) else min(TIMEOUT_S, ttl / 1000)
        if timeout <= 0:
            return {"id": rid, "ok": False, "error": "expired"}, 408

        done = threading.Event()
        ctx = RequestContext(transport=transport, deadline=time.monotonic() + timeout, meta={"command": name, "id": rid})
        for hooks in (ctx.on_reply, ctx.on_complete, ctx.on_timeout):
            hooks.append(lambda c: done.set())
        t0 = time.monotonic()
        self.projects.engine.inject_call(pid, Origin("remote", transport), lambda: node.deliver(args, ctx),
                                         ctx=ctx, node=node)
        done.wait(timeout + 0.5)
        if ctx.replied:
            result = ({"id": rid, "ok": ctx.reply_status < 400, "result": ctx.reply_body}, 200)
        elif ctx.done and not ctx.timed_out:
            result = ({"id": rid, "ok": True}, 200)
        else:
            result = ({"id": rid, "ok": False, "error": "timeout"}, 504)
        ms = (time.monotonic() - t0) * 1000
        status = "ok" if result[0]["ok"] else result[0].get("error", "ошибка")
        rt.engine.loop.call_soon_threadsafe(rt.log, f"команда {name} ({transport}, {who}): {status}, {ms:.0f} мс")
        if rid:
            with self._lock:
                self._recent[(name, rid)] = result
                while len(self._recent) > _RECENT_MAX:
                    self._recent.popitem(last=False)
        return result

    def state_changed(self, name: str, current: dict) -> None:
        for cb in list(self.state_listeners):
            try:
                cb(name, current)
            except Exception:  # публикация не должна ломать граф
                pass
