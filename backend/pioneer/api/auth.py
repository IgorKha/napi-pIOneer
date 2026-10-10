"""Вход/выход и защита API: сессия в cookie + CSRF-заголовок на изменяющих запросах."""
from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket
from pydantic import BaseModel

from pioneer.auth import Auth

COOKIE = "pioneer_session"
CSRF_HEADER = "x-pioneer"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
FAIL_DELAY_S = 1.0  # замедляет перебор пароля

router = APIRouter(prefix="/auth")


def _auth(request: Request) -> Auth:
    return request.app.state.auth


def _csrf(request: Request) -> None:
    # Изменяющий запрос должен нести свой заголовок: из чужого сайта без CORS его не выставить.
    if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(403, "нет заголовка X-Pioneer (защита от CSRF)")


def require_session(request: Request) -> None:
    """Зависимость для всех защищённых роутеров."""
    _csrf(request)
    if not _auth(request).valid(request.cookies.get(COOKIE)):
        raise HTTPException(401, "нужен вход")


def ws_allowed(ws: WebSocket) -> bool:
    """WS: та же сессия + Origin с того же хоста (защита от cross-site WebSocket)."""
    origin = ws.headers.get("origin")
    if origin and urlparse(origin).netloc != ws.headers.get("host"):
        return False
    return ws.app.state.auth.valid(ws.cookies.get(COOKIE))


class Login(BaseModel):
    password: str


@router.get("/me")
def me(request: Request) -> dict:
    a = _auth(request)
    return {"authenticated": a.valid(request.cookies.get(COOKIE)), "password_set": a.password_set}


@router.post("/login")
async def login(body: Login, request: Request, response: Response) -> dict:
    _csrf(request)
    a = _auth(request)
    if not a.password_set:
        raise HTTPException(503, "пароль не задан: выполните на плате python -m pioneer set-password")
    client = request.client.host if request.client else "?"
    if a.throttled(client):
        raise HTTPException(429, "слишком много неудачных попыток, подождите 5 минут")
    ok = await asyncio.to_thread(a.check_password, body.password)
    if not ok:
        a.record_fail(client)
        await asyncio.sleep(FAIL_DELAY_S)
        raise HTTPException(401, "неверный пароль")
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(COOKIE, a.create_session(), httponly=True, samesite="strict", secure=secure,
                        path="/pioneer", max_age=30 * 24 * 3600)
    return {"authenticated": True}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    _csrf(request)
    _auth(request).drop_session(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/pioneer")
    return {"authenticated": False}
