"""Настройки сервиса из UI: MQTT (idea 003)."""
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
