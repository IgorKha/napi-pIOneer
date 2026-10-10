"""FastAPI-приложение pioneer-core: сборка роутеров, жизненный цикл HAL/HW, live по WS."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from pioneer import __version__
from pioneer.api import auth, bench, blocks, board, help, hooks, projects, remote, scripts, secrets
from pioneer.api.live import LiveHub
from pioneer.auth import Auth
from pioneer.board import load_board, parse_uenv_overlays
from pioneer.config import REPO_ROOT, Config, load_config
from pioneer.dt import read_dt
from pioneer.hal import BenchHAL, create_hal
from pioneer.hw import HW
from pioneer.resources import ResourceManager
from pioneer.nodes import registry
from pioneer.projects import ProjectManager
from pioneer.scripts import ScriptManager
from pioneer.secrets_store import SecretStore
from pioneer.userblocks import BlockManager

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
        dt = await asyncio.to_thread(read_dt, cfg.dt_dir)  # ~1.5 с на плате
        b = load_board(cfg.board_file, _read_overlays(cfg), dt)
        hal = create_hal(cfg.hal, b, bench_store=cfg.data_dir / "bench.json")
        app.state.cfg, app.state.board, app.state.hal = cfg, b, hal
        app.state.auth = Auth(cfg.data_dir)
        app.state.hw = HW(b, hal, ResourceManager(b))
        app.state.live = live = LiveHub(b, app.state.hw)
        if isinstance(hal, BenchHAL):
            hal.on_change(live.mark_bench)
        app.state.scripts = sm = ScriptManager(cfg.data_dir, app.state.hw)
        sm.on_pins, sm.on_leds = live.mark, live.mark_leds
        live.add_source(sm.drain)
        reg = registry().copy()  # встроенные + свои блоки
        app.state.blocks = BlockManager(cfg.data_dir, reg)
        app.state.projects = pm = ProjectManager(cfg.data_dir, app.state.hw, reg)
        app.state.secrets = pm.engine.secrets = SecretStore(cfg.data_dir)
        pm.on_pins, pm.on_leds = live.mark, live.mark_leds
        live.add_source(pm.drain)
        live.on_message("ui", lambda m: pm.ui(m["project"], m["node"], m.get("value")))
        live.start()
        sm.start_autostart()
        await asyncio.to_thread(pm.start_autostart)
        yield
        await asyncio.to_thread(pm.stop_all)
        pm.close()
        sm.stop_all()
        await live.stop()
        app.state.hw.close_all(safe_value=False)
        hal.close()

    app = FastAPI(title="napi-pioneer", version=__version__, lifespan=lifespan,
                  docs_url=f"{API}/docs", openapi_url=f"{API}/openapi.json")
    public = APIRouter()
    public.add_api_route("/health", health)
    public.include_router(auth.router)
    public.include_router(hooks.router)  # своя защита — токен ноды
    public.include_router(remote.router)  # своя защита — ключ remote_api_key
    protected = APIRouter(dependencies=[Depends(auth.require_session)])
    protected.include_router(board.router)
    protected.include_router(bench.router)
    protected.include_router(scripts.router)
    protected.include_router(projects.router)
    protected.include_router(help.router)
    protected.include_router(blocks.router)
    protected.include_router(secrets.router)
    app.include_router(public, prefix=API)
    app.include_router(protected, prefix=API)

    @app.websocket("/pioneer/ws")
    async def ws(websocket: WebSocket) -> None:
        if not auth.ws_allowed(websocket):
            await websocket.close(code=4401)
            return
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
            # index.html не кэшируем: ассеты с хэшами, а новая сборка должна подхватываться сразу
            return FileResponse(FRONTEND_DIST / "index.html", headers={"Cache-Control": "no-cache"})
    return app


def health(request: Request) -> dict:
    return {"ok": True, "version": __version__, "hal": request.app.state.hal.name}
