"""FastAPI-приложение pioneer-core. Фаза 0: плата, ручное управление, live по WS, mock-пульт.

TODO фаза 0: auth (логин/сессия), скрипты в worker-процессе.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pioneer import __version__
from pioneer.board import Board, InterfaceError, load_board, parse_uenv_overlays
from pioneer.api.live import LiveHub
from pioneer.config import REPO_ROOT, Config, load_config
from pioneer.hal import MockHAL, create_hal
from pioneer.pins import PinIO
from pioneer.resources import ResourceConflict, ResourceManager

API = "/pioneer/api"
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"
MANUAL_OWNER = "manual"


def _read_overlays(cfg: Config) -> list[str]:
    try:
        return parse_uenv_overlays(cfg.uenv_file.read_text())
    except OSError:
        return list(cfg.mock_overlays)


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        board = load_board(cfg.board_file, _read_overlays(cfg))
        hal = create_hal(cfg.hal, adc_device=board.adc_device, adc_vref=board.adc_vref)
        app.state.board, app.state.hal = board, hal
        app.state.pins = PinIO(board, hal, ResourceManager(board))
        app.state.live = LiveHub(board, app.state.pins)
        app.state.live.start()
        yield
        await app.state.live.stop()
        app.state.pins.close_all(safe_value=False)
        hal.close()

    app = FastAPI(title="napi-pioneer", version=__version__, lifespan=lifespan,
                  docs_url=f"{API}/docs", openapi_url=f"{API}/openapi.json")
    app.include_router(router, prefix=API)

    @app.websocket("/pioneer/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.app.state.live.connect(websocket)

    if (FRONTEND_DIST / "index.html").exists():
        app.mount("/pioneer/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        def root() -> RedirectResponse:  # на плате / — штатная админка, сюда не доходит
            return RedirectResponse("/pioneer/")

        @app.get("/pioneer/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:  # SPA: любые не-API пути -> index.html
            if path.startswith("api/"):
                raise HTTPException(404)
            return FileResponse(FRONTEND_DIST / "index.html")
    return app


router = APIRouter()


def _ctx(request: Request) -> tuple[Board, PinIO]:
    return request.app.state.board, request.app.state.pins


@router.get("/health")
def health(request: Request) -> dict:
    return {"ok": True, "version": __version__, "hal": request.app.state.hal.name}


@router.get("/board")
def get_board(request: Request) -> dict:
    board, pins = _ctx(request)
    claims = pins.res.claims
    return {
        "id": board.id,
        "title": board.title,
        "hal": request.app.state.hal.name,
        "headers": [asdict(h) for h in board.headers],
        "adc": {"vref": board.adc_vref, "bits": board.adc_bits},
        "overlays": board.overlays,
        "interfaces": [
            {"id": i.id, "title": i.title, "device": i.device, "locked": i.locked, "note": i.note,
             "enabled": board.interface_enabled(i.id), "pins": i.pins,
             "conflicts": board.conflicts(i.id)}
            for i in board.interfaces.values()
        ],
        "pins": [
            {
                "id": p.id, "name": p.name, "kind": p.kind, "power": p.power, "special": p.special,
                "gpio": asdict(p.gpio) if p.gpio else None, "adc_channel": p.adc_channel,
                "default_pull": p.default_pull, "drive_ma": p.drive_ma, "alt": list(p.alt),
                # режимы: текущая функция (pinmux) + что доступно на лету + какие интерфейсы возможны
                "function": board.function(p.id),
                "runtime_modes": board.runtime_modes(p.id),
                "interface_options": [
                    {"id": i, "role": board.interfaces[i].pins[p.id]} for i in p.interfaces],
                "claim": ({"mode": c.mode, "owners": c.owners}
                          if (c := claims.get(p.id)) else None),
                "value": pins.value(p.id),
            }
            for p in board.pins.values()
        ],
    }


class ManualRequest(BaseModel):
    mode: Literal["gpio_in", "gpio_out", "adc", "release"]
    value: bool = False


@router.post("/board/pins/{pin_id}/manual")
def manual(pin_id: str, body: ManualRequest, request: Request) -> dict:
    """Ручное управление пином (отладка). Только свободные или уже ручные пины."""
    board, pins = _ctx(request)
    if pin_id not in board.pins:
        raise HTTPException(404, f"нет пина {pin_id}")
    request.app.state.live.mark(pin_id)
    if body.mode == "release":
        pins.release(MANUAL_OWNER, pin_id)
        return {"pin": pin_id, "claim": None}
    try:
        pins.acquire(MANUAL_OWNER, "Ручное управление", pin_id, body.mode, initial=body.value)
    except ResourceConflict as e:
        raise HTTPException(409, str(e)) from e
    if body.mode == "gpio_out":
        pins.write(pin_id, body.value)
    return {"pin": pin_id, "mode": body.mode, "value": pins.value(pin_id)}


class InterfacePlanRequest(BaseModel):
    enable: list[str] = []
    disable: list[str] = []


@router.post("/board/interfaces/plan")
def plan_interfaces(body: InterfacePlanRequest, request: Request) -> dict:
    """Предпросмотр смены интерфейсов (pinmux). Ничего не пишет — применение в фазе 3."""
    board, _ = _ctx(request)
    try:
        return asdict(board.plan_interfaces(body.enable, body.disable))
    except InterfaceError as e:
        raise HTTPException(422, str(e)) from e


# ---- mock-пульт: «внешний мир» для разработки без платы
class MockLevel(BaseModel):
    value: bool


class MockAdc(BaseModel):
    volts: float


def _mock(request: Request) -> MockHAL:
    hal = request.app.state.hal
    if not isinstance(hal, MockHAL):
        raise HTTPException(404, "доступно только с mock-HAL")
    return hal


@router.post("/mock/pins/{pin_id}")
def mock_level(pin_id: str, body: MockLevel, request: Request) -> dict:
    hal, (board, _) = _mock(request), _ctx(request)
    pin = board.pins.get(pin_id)
    if not pin or not pin.gpio:
        raise HTTPException(404, f"{pin_id} не GPIO")
    hal.set_level(pin.gpio, body.value)
    request.app.state.live.mark(pin_id)
    return {"pin": pin_id, "value": body.value}


@router.post("/mock/adc/{channel}")
def mock_adc(channel: int, body: MockAdc, request: Request) -> dict:
    _mock(request).set_adc(channel, body.volts)
    return {"channel": channel, "volts": body.volts}
