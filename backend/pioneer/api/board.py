"""Плата: описание, режимы пинов, ручное управление, предпросмотр смены интерфейсов."""
from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from pioneer.api import deps
from pioneer.board import InterfaceError
from pioneer.resources import ResourceConflict

router = APIRouter()
MANUAL_OWNER = "manual"


@router.get("/board")
def get_board(request: Request) -> dict:
    board, hw = deps.board(request), deps.hw(request)
    claims = hw.res.claims
    return {
        "id": board.id,
        "title": board.title,
        "hal": deps.hal(request).name,
        "headers": [asdict(h) for h in board.headers],
        "adc": {"vref": board.adc_vref, "bits": board.adc_bits},
        "overlays": board.overlays,
        "live_dt": board.dt is not None,  # занятость из живого Device Tree (на плате)
        "interfaces": [
            {"id": i.id, "controller": i.controller, "kind": i.kind, "title": i.title, "device": i.device, "locked": i.locked, "note": i.note,
             "enabled": board.interface_enabled(i.id),  # сейчас
             "enabled_in_uenv": board.uenv_enabled(i.id),  # после перезагрузки
             "pending": board.interface_pending(i.id),
             "pins": i.pins, "conflicts": board.conflicts(i.id),
             # кто вне core держит порт (getty, mbusd, …) — только для включённых UART
             "holders": (deps.hal(request).device_holders(i.device)
                         if i.device and i.device.startswith("/dev/tty") and board.interface_enabled(i.id)
                         and not i.locked else [])}
            for i in board.interfaces.values()
        ],
        "leds": [led_info(request, led_id) for led_id in board.leds],
        "pins": [
            {
                "id": p.id, "name": p.name, "kind": p.kind, "power": p.power, "special": p.special,
                "gpio": asdict(p.gpio) if p.gpio else None, "adc_channel": p.adc_channel,
                "default_pull": p.default_pull, "drive_ma": p.drive_ma, "alt": list(p.alt),
                # режимы: текущая функция (pinmux) + что доступно на лету + какие интерфейсы возможны
                "function": board.function(p.id),
                "kernel_users": board.kernel_users(p.id),
                "runtime_modes": board.runtime_modes(p.id),
                "interface_options": [
                    {"id": i, "role": board.interfaces[i].pins[p.id]} for i in p.interfaces],
                "claim": ({"mode": c.mode, "owners": c.owners}
                          if (c := claims.get(p.id)) else None),
                "value": hw.value(p.id),
            }
            for p in board.pins.values()
        ],
        # ресурсы шин (i2c1@0x3c, uart3, spi2.cs0, led:user)
        "bus_claims": {rid: {"mode": c.mode, "owners": c.owners}
                       for rid, c in claims.items() if rid not in board.pins},
    }


class ManualRequest(BaseModel):
    mode: Literal["gpio_in", "gpio_out", "adc", "release"]
    value: bool = False


@router.post("/board/pins/{pin_id}/manual")
def manual(pin_id: str, body: ManualRequest, request: Request) -> dict:
    """Ручное управление пином (отладка). Только свободные или уже ручные пины."""
    board, hw = deps.board(request), deps.hw(request)
    if pin_id not in board.pins:
        raise HTTPException(404, f"нет пина {pin_id}")
    request.app.state.live.mark(pin_id)
    if body.mode == "release":
        hw.release(MANUAL_OWNER, pin_id)
        return {"pin": pin_id, "claim": None}
    try:
        hw.acquire(MANUAL_OWNER, "Ручное управление", pin_id, body.mode, initial=body.value)
    except ResourceConflict as e:
        raise HTTPException(409, str(e)) from e
    if body.mode == "gpio_out":
        hw.write(pin_id, body.value)
    return {"pin": pin_id, "mode": body.mode, "value": hw.value(pin_id)}


def led_info(request: Request, led_id: str) -> dict:
    led = deps.board(request).leds[led_id]
    claim = deps.hw(request).res.claims.get(f"led:{led_id}")
    return {"id": led.id, "title": led.title, "color": led.color, "default_trigger": led.default_trigger,
            **deps.hal(request).led_state(led.sysfs),
            "claim": {"mode": claim.mode, "owners": claim.owners} if claim else None}


class LedRequest(BaseModel):
    action: Literal["on", "off", "trigger", "release"]
    trigger: str | None = None


@router.post("/board/leds/{led_id}")
def led_manual(led_id: str, body: LedRequest, request: Request) -> dict:
    """Ручное управление встроенным LED. release — вернуть системный триггер."""
    hw = deps.hw(request)
    if led_id not in deps.board(request).leds:
        raise HTTPException(404, f"нет LED {led_id}")
    if body.action == "release":
        hw.release(MANUAL_OWNER, f"led:{led_id}")
    else:
        try:
            led = hw.led(MANUAL_OWNER, "Ручное управление", led_id)
            if body.action == "trigger":
                led.set_trigger(body.trigger or "none")
            else:
                led.set(body.action == "on")
        except ResourceConflict as e:
            raise HTTPException(409, str(e)) from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
    request.app.state.live.mark_leds()
    return led_info(request, led_id)


class InterfacePlanRequest(BaseModel):
    enable: list[str] = []
    disable: list[str] = []


@router.post("/board/interfaces/plan")
def plan_interfaces(body: InterfacePlanRequest, request: Request) -> dict:
    """Предпросмотр смены интерфейсов (pinmux). Ничего не пишет — применение в фазе 3."""
    try:
        return asdict(deps.board(request).plan_interfaces(body.enable, body.disable))
    except InterfaceError as e:
        raise HTTPException(422, str(e)) from e
