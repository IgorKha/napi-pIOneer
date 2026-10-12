"""Настройки сервиса из UI: MQTT (idea 003), таймауты worker-процессов (pioneer.limits)."""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from pioneer.mqtt import MqttSettings

router = APIRouter(prefix="/settings")


@router.get("/mqtt")
def get_mqtt(request: Request) -> dict:
    return {"settings": asdict(request.app.state.settings.mqtt()), "status": request.app.state.mqtt.status()}


class MqttIn(BaseModel):
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 1883
    username: str = ""
    password: str = ""
    tls: bool = False
    device_id: str = ""
    base: str = "pioneer"


@router.put("/mqtt")
def put_mqtt(body: MqttIn, request: Request) -> dict:
    s = MqttSettings(**body.model_dump())
    try:
        request.app.state.settings.save_mqtt(s)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    request.app.state.mqtt.apply(request.app.state.settings.mqtt())
    return get_mqtt(request)


# ---- таймауты и лимиты worker-процессов
def _limits_view(request: Request) -> dict:
    from pioneer import limits
    store = request.app.state.limits
    return {"values": asdict(store.get()), "defaults": store.base,
            "fields": [{"name": k, "min": lo, "max": hi, "title": t, "doc": d}
                       for k, (lo, hi, t, d) in limits.SPEC.items()]}


@router.get("/limits")
def get_limits(request: Request) -> dict:
    return _limits_view(request)


@router.put("/limits")
def put_limits(body: dict[str, float | bool | None], request: Request) -> dict:
    """Частичное обновление; null — вернуть умолчание. Действует без перезапуска сервиса:
    таймауты вызовов — сразу, запуск процесса блоков — со следующего старта проекта."""
    try:
        request.app.state.limits.save(body)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return _limits_view(request)
