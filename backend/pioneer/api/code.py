"""Общее файловое API для скриптов, блоков и библиотек (idea 005)."""

from __future__ import annotations

import time
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel

from pioneer.api.auth import require_session
from pioneer.codetree import CodeTree, FILE_LIMIT, TREE_LIMIT, TreeError, project_tree


def access(request: Request) -> None:
    if request.headers.get("x-pioneer-key"):
        # Отдельный ключ: доступ к коду не наследуется от ключа команд железа.
        import hmac
        from pioneer.engine.node import NodeError

        try:
            expected = request.app.state.secrets.resolve("{{secret:code_api_key}}")
        except NodeError:
            raise HTTPException(403, "задайте секрет code_api_key") from None
        if not hmac.compare_digest(expected.encode(), request.headers["x-pioneer-key"].encode()):
            raise HTTPException(401, "неверный ключ")
    else:
        require_session(request)


router = APIRouter(prefix="/code", dependencies=[Depends(access)])


def tree(request: Request, kind: str, ident: str) -> CodeTree:
    try:
        t = project_tree(request.app.state.cfg.data_dir, kind, ident)
        if not t.path("pioneer.json", metadata=True).is_file():
            raise HTTPException(404, "нет проекта с кодом")
        return t
    except TreeError as e:
        raise HTTPException(400, str(e)) from e


def guard(fn, *args):
    try:
        return fn(*args)
    except (TreeError, OSError, UnicodeError) as e:
        raise HTTPException(400, str(e)) from e


def changed(request: Request, kind: str, ident: str, t: CodeTree) -> dict:
    if kind == "scripts":
        request.app.state.scripts.update(ident)
        request.app.state.scripts.saved(ident)
        return {"ok": True}
    if kind == "blocks":
        bm = request.app.state.blocks
        return bm.save(ident, bm.get(ident)["code"])
    m = t.meta()
    m["updated"] = time.time()
    t.set_meta(m)
    return {"ok": True}


@router.get("/lib")
def libraries(request: Request) -> list[dict]:
    base = request.app.state.cfg.data_dir / "lib"
    return [
        {"id": p.parent.name, **CodeTree(p.parent).meta()}
        for p in sorted(base.glob("*/pioneer.json"))
    ]


class Library(BaseModel):
    id: str
    name: str


@router.post("/lib")
def create_library(body: Library, request: Request) -> dict:
    t = guard(project_tree, request.app.state.cfg.data_dir, "lib", body.id)
    if not body.id.isidentifier():
        raise HTTPException(400, "имя библиотеки должно быть Python-идентификатором")
    if t.root.exists():
        raise HTTPException(409, "библиотека уже есть")
    t.set_meta({"name": body.name, "kind": "lib", "updated": time.time()})
    return {"id": body.id}


@router.get("/{kind}/{ident}/tree")
def files(kind: str, ident: str, request: Request) -> dict:
    t = tree(request, kind, ident)
    return {"meta": t.meta(), "files": guard(t.files)}


@router.get("/{kind}/{ident}/file")
def read_file(kind: str, ident: str, path: str, request: Request) -> dict:
    data = guard(tree(request, kind, ident).read, path)
    return {"path": path, "text": guard(data.decode, "utf-8")}


class TextFile(BaseModel):
    path: str
    text: str


@router.put("/{kind}/{ident}/file")
def write_file(kind: str, ident: str, body: TextFile, request: Request) -> dict:
    t = tree(request, kind, ident)
    guard(t.write, body.path, body.text.encode())
    return changed(request, kind, ident, t)


async def limited_body(request: Request, limit: int) -> bytes:
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, "превышен лимит загрузки")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/{kind}/{ident}/upload")
async def upload(kind: str, ident: str, path: str, request: Request) -> dict:
    t = tree(request, kind, ident)
    guard(t.write, path, await limited_body(request, FILE_LIMIT))
    return changed(request, kind, ident, t)


@router.get("/{kind}/{ident}/download")
def download(kind: str, ident: str, path: str, request: Request) -> Response:
    data = guard(tree(request, kind, ident).read, path)
    import mimetypes

    media = mimetypes.guess_type(path)[0] or "application/octet-stream"
    # SVG/HTML никогда не исполняются в origin приложения.
    if media not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
        media = "application/octet-stream"
    return Response(data, media_type=media, headers={"X-Content-Type-Options": "nosniff"})


class Operation(BaseModel):
    path: str
    target: str | None = None


@router.post("/{kind}/{ident}/mkdir")
@router.post("/{kind}/{ident}/move")
@router.post("/{kind}/{ident}/delete")
def operation(kind: str, ident: str, body: Operation, request: Request) -> dict:
    op = request.url.path.rsplit("/", 1)[-1]
    t = tree(request, kind, ident)
    if op == "mkdir":
        guard(t.mkdir, body.path)
    elif op == "move" and body.target:
        guard(t.move, body.path, body.target)
    elif op == "delete":
        guard(t.delete, body.path)
    else:
        raise HTTPException(400, "неизвестная операция")
    return changed(request, kind, ident, t)


@router.get("/{kind}/{ident}/export.zip")
def export(kind: str, ident: str, request: Request) -> FileResponse:
    t = tree(request, kind, ident)
    with tempfile.NamedTemporaryFile(prefix="pioneer-export-", suffix=".zip", delete=False) as f:
        path = Path(f.name)
    try:
        guard(t.export_to, path)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return FileResponse(
        path,
        media_type="application/zip",
        filename="project.zip",
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


@router.post("/{kind}/{ident}/import.zip")
async def import_zip(kind: str, ident: str, request: Request) -> dict:
    t = tree(request, kind, ident)
    with tempfile.TemporaryDirectory(prefix="pioneer-import-") as d:
        archive = Path(d) / "upload.zip"
        size = 0
        with archive.open("wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > TREE_LIMIT:
                    raise HTTPException(413, "архив больше 50 МБ")
                f.write(chunk)
        guard(t.import_zip, archive)
    return changed(request, kind, ident, t)


class Metadata(BaseModel):
    name: str | None = None
    entry: str | None = None
    autostart: bool | None = None
    restart_on_save: bool | None = None


@router.patch("/{kind}/{ident}/meta")
def metadata(kind: str, ident: str, body: Metadata, request: Request) -> dict:
    from dataclasses import asdict
    from pioneer.api.scripts import _guard

    t = tree(request, kind, ident)
    if kind == "scripts":
        return _guard(
            lambda: asdict(
                request.app.state.scripts.update(ident, **body.model_dump(exclude_none=True))
            )
        )
    if body.entry is not None or body.autostart is not None or body.restart_on_save is not None:
        raise HTTPException(400, "настройки запуска доступны только скриптам")
    if kind == "blocks":
        bm = request.app.state.blocks
        return bm.save(ident, bm.get(ident)["code"], body.name)
    meta = t.meta()
    if body.name:
        meta["name"] = body.name
    t.set_meta(meta)
    return meta


@router.post("/{kind}/{ident}/run")
def run(kind: str, ident: str, request: Request) -> dict:
    from dataclasses import asdict
    from pioneer.api.scripts import _guard

    tree(request, kind, ident)
    if kind != "scripts":
        raise HTTPException(400, "запускаются только скрипты")
    return asdict(_guard(request.app.state.scripts.restart, ident))


@router.delete("/{kind}/{ident}/dependencies")
@router.post("/{kind}/{ident}/dependencies")
def dependencies(kind: str, ident: str, request: Request, confirmed: bool = False):
    from pioneer.dependencies import install, remove

    t = tree(request, kind, ident)
    if kind == "lib":
        raise HTTPException(422, "зависимости устанавливаются в скрипт или пакет блока")
    if kind == "scripts" and request.app.state.scripts.status(ident).state == "running":
        raise HTTPException(409, "сначала остановите скрипт")
    if kind == "blocks" and request.app.state.projects.engine.runtimes:
        raise HTTPException(409, "для установки зависимостей блока сначала остановите проекты")
    if request.method == "DELETE":
        return guard(remove, t.root)
    try:
        return install(t.root, confirmed)
    except (OSError, TreeError):
        raise HTTPException(
            422, "проверьте requirements.txt и подтверждение установки колёс"
        ) from None


async def upload_to(request: Request, path: Path, limit: int):
    """Bound uploaded archives on disk rather than retaining two full body copies in RAM."""
    import asyncio, os

    size = 0
    with path.open("xb") as file:
        os.chmod(path, 0o600)
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, "превышен лимит загрузки")
            await asyncio.to_thread(file.write, chunk)
    return path
