import asyncio
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from pioneer.api.code import upload_to
import tempfile
from pathlib import Path
from pioneer.codetree import CodeTree, TreeError, TREE_LIMIT

router = APIRouter(prefix="/modules")


@router.get("")
def catalog(request: Request):
    return {
        "modules": request.app.state.modules.list(),
        "bench": request.app.state.hal.name == "bench",
    }


@router.post("")
async def install(request: Request, confirmed: bool = False):
    if not confirmed:
        raise HTTPException(422, "подтвердите доверие к коду модуля")
    with tempfile.TemporaryDirectory(dir=request.app.state.cfg.data_dir) as tmp:
        data = await upload_to(request, Path(tmp) / "module.zip", TREE_LIMIT)
        try:
            return await asyncio.to_thread(request.app.state.modules.install, data, confirmed)
        except (OSError, ValueError, KeyError, TypeError):
            raise HTTPException(
                422, "проверьте module.yaml, ZIP, зависимости и доверие к коду"
            ) from None


class Git(BaseModel):
    url: str = Field(max_length=500)
    ref: str = Field(max_length=100)
    confirmed: bool = False


@router.post("/git")
async def git(body: Git, request: Request):
    try:
        return await asyncio.to_thread(
            request.app.state.modules.git, body.url, body.ref, body.confirmed
        )
    except (OSError, ValueError, KeyError, TypeError):
        raise HTTPException(422, "не удалось установить доверенный git-модуль") from None


@router.get("/example.zip")
def example():
    from pioneer.config import REPO_ROOT

    data = CodeTree(REPO_ROOT / "modules/examples/register").export()
    return Response(
        data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="register-demo.zip"'},
    )


@router.get("/{ident}/{version}/readme")
def readme(ident: str, version: int, request: Request):
    try:
        return {
            "markdown": CodeTree(request.app.state.modules.path(ident, version))
            .read("README.md")
            .decode()
        }
    except (ValueError, OSError):
        raise HTTPException(404, "нет README") from None


@router.get("/{ident}/{version}/widget")
def widget(ident: str, version: int, request: Request):
    try:
        m = request.app.state.modules.records[(ident, version)]
        if not m.get("widget"):
            raise ValueError()
        data = CodeTree(request.app.state.modules.path(ident, version)).read(m["widget"])
        return Response(
            data,
            media_type="text/javascript",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
    except (ValueError, OSError, KeyError):
        raise HTTPException(404, "нет виджета") from None


@router.delete("/{ident}/{version}")
def remove(ident: str, version: int, request: Request):
    manager = request.app.state.modules
    try:
        m = manager.records[(ident, version)]
    except KeyError:
        raise HTTPException(404, "нет модуля") from None
    required = set(m["types"])
    pm = request.app.state.projects
    for project in pm.list():
        _, graph = pm._read(project["id"])
        flat, _, _ = pm._flat(graph)
        if required.intersection(n.type for n in flat.nodes.values()):
            raise HTTPException(409, "модуль используется проектом " + project["name"])
    if any(
        model.kind == m["model_kind"]
        for model in getattr(request.app.state.hal, "models", {}).values()
    ):
        raise HTTPException(409, "сначала удалите симуляторы этого модуля")
    try:
        manager.remove(ident, version)
    except (OSError, ValueError):
        raise HTTPException(422, "не удалось удалить модуль") from None
    return {"ok": True}
