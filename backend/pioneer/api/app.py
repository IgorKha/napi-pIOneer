"""FastAPI-приложение pioneer-core: сборка роутеров, жизненный цикл HAL/HW, live по WS."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from pioneer import __version__
from pioneer.api import bench, board
from pioneer.api.live import LiveHub
from pioneer.board import load_board, parse_uenv_overlays
from pioneer.config import REPO_ROOT, Config, load_config
from pioneer.hal import BenchHAL, create_hal
from pioneer.hw import HW
from pioneer.resources import ResourceManager

API = "/pioneer/api"
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"


def _read_overlays(cfg: Config) -> list[str]:
    try:
        return parse_uenv_overlays(cfg.uenv_file.read_text())
    except OSError:
        return list(cfg.default_overlays)


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        b = load_board(cfg.board_file, _read_overlays(cfg))
        hal = create_hal(cfg.hal, b, bench_store=cfg.data_dir / "bench.json")
        app.state.cfg, app.state.board, app.state.hal = cfg, b, hal
        app.state.hw = HW(b, hal, ResourceManager(b))
        app.state.live = live = LiveHub(b, app.state.hw)
        if isinstance(hal, BenchHAL):
            hal.on_change(live.mark_bench)
        live.start()
        yield
        await live.stop()
        app.state.hw.close_all(safe_value=False)
        hal.close()

    app = FastAPI(title="napi-pioneer", version=__version__, lifespan=lifespan,
                  docs_url=f"{API}/docs", openapi_url=f"{API}/openapi.json")
    api = APIRouter()
    api.add_api_route("/health", health)
    api.include_router(board.router)
    api.include_router(bench.router)
    app.include_router(api, prefix=API)

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


def health(request: Request) -> dict:
    return {"ok": True, "version": __version__, "hal": request.app.state.hal.name}
