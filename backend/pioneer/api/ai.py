"""Session/CSRF-protected AI settings and streaming project conversations."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from pioneer.projects import ProjectError
from pioneer.engine.node import NodeError

router = APIRouter(prefix="/ai")


@router.get("/settings")
def settings(request: Request):
    return request.app.state.ai.public_settings()


@router.put("/settings")
def save_settings(body: dict, request: Request):
    try:
        return request.app.state.ai.save_settings(body)
    except ValueError:
        # Validation errors may contain the submitted API key. Never echo request data.
        raise HTTPException(422, "неверные настройки AI: проверьте провайдера, URL, effort и лимиты") from None


@router.get("/chats/{pid}")
def history(pid: str, request: Request):
    try:
        return {"messages": request.app.state.ai.history(request.app, pid)}
    except ProjectError as e:
        raise HTTPException(e.code, str(e)) from None
    except ValueError:
        raise HTTPException(422, "история повреждена или слишком большая — начните новый чат") from None


@router.delete("/chats/{pid}")
def reset(pid: str, request: Request):
    ai = request.app.state.ai
    if ai.active == pid:
        raise HTTPException(409, "сначала остановите ответ AI")
    try:
        ai.path(request.app, pid).unlink(missing_ok=True)
    except ProjectError as e:
        raise HTTPException(e.code, str(e)) from None
    return {"ok": True}


class Message(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    model: str | None = Field(default=None, min_length=1, max_length=120)
    attachments: list[dict] = Field(default_factory=list, max_length=4)


class Decision(BaseModel):
    status: str


@router.put("/chats/{pid}/artifacts/{aid}")
def decision(pid: str, aid: str, body: Decision, request: Request):
    ai = request.app.state.ai
    if ai.active == pid:
        raise HTTPException(409, "дождитесь окончания ответа")
    if body.status not in ("accepted", "rejected"):
        raise HTTPException(422, "неверное решение")
    try:
        records = ai.history(request.app, pid)
    except ProjectError as e:
        raise HTTPException(e.code, str(e)) from None
    except ValueError:
        raise HTTPException(422, "история повреждена") from None
    found = False
    for m in records:
        for a in m.get("artifacts", []):
            if a["id"] == aid:
                a["status"] = body.status
                found = True
    if not found:
        raise HTTPException(404, "предложение не найдено")
    path = ai.path(request.app, pid)
    # Small bounded transcript; use the same private atomic JSONL writer.
    import os
    import json
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return {"ok": True}


@router.post("/chats/{pid}/messages")
async def chat(pid: str, request: Request):
    import json
    from pioneer.api.code import limited_body
    from pydantic import ValidationError
    from pioneer.ai.attachments import validate
    ai = request.app.state.ai
    try:
        body = Message.model_validate(json.loads(await limited_body(request, 1500 * 1024)))
        attachments = validate(body.attachments, ai.redact)
    except (ValueError, KeyError, TypeError, ValidationError):
        raise HTTPException(422, "проверьте модель и вложения: до 4 файлов, текст до 64 КиБ, изображения до 256 КиБ/4 Мп") from None
    ai = request.app.state.ai
    try:
        settings, key = await ai.begin(request.app, pid)
    except ProjectError as e:
        raise HTTPException(e.code, str(e)) from None
    except NodeError:
        raise HTTPException(422, "задайте API-ключ на странице Настройки → AI") from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    if body.model is not None:
        settings = settings.model_copy(update={"model": body.model.strip()})
        if not settings.model:
            ai.active = None; ai.lock.release()
            raise HTTPException(422, "укажите модель")
    return StreamingResponse(ai.chat(request.app, pid, body.text, settings, key, attachments), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


class FileEdit(BaseModel):
    code: str = Field(max_length=64000)


@router.put("/chats/{pid}/artifacts/{aid}/file")
def accept_file(pid: str, aid: str, body: FileEdit, request: Request):
    """Explicit user action; compare under the same CodeTree lock as ordinary writes.

    Saving AI code never triggers restart_on_save. Running the result is a separate action.
    """
    import ast
    import hashlib
    from pioneer.codetree import TreeError
    ai = request.app.state.ai
    is_block = pid.startswith("block:")
    if not is_block and not pid.startswith("script:"):
        raise HTTPException(422, "файл можно сохранить только в скрипте или кастомном блоке")
    if ai.active == pid:
        raise HTTPException(409, "дождитесь окончания ответа")
    try:
        records = ai.history(request.app, pid)
        artifact = next((a for m in records for a in m.get("artifacts", []) if a["id"] == aid), None)
        if not artifact or artifact.get("kind") != "file":
            raise HTTPException(404, "предложение файла не найдено")
        if artifact.get("status") != "pending":
            raise HTTPException(409, "предложение уже рассмотрено")
        if len(body.code.encode()) > 64000:
            raise HTTPException(422, "код больше 64 КБ")
        ast.parse(body.code)
        sm = request.app.state.scripts
        bm = request.app.state.blocks
        if is_block:
            from pioneer.codetree import project_tree
            t = project_tree(request.app.state.cfg.data_dir, "blocks", pid[6:])
        else:
            t = sm.tree(pid[7:])
        # BlockManager.save takes manager lock before CodeTree lock.
        with (bm._lock if is_block else t.lock), t.lock:
            path = t.path(artifact["path"])
            original = t.read(artifact["path"]) if path.exists() else None
            digest = hashlib.sha256(original).hexdigest() if original is not None else None
            if digest != artifact["base_hash"]:
                raise HTTPException(409, "файл изменился — попросите AI обновить предложение")
            t.write(artifact["path"], body.code.encode())
            if is_block:
                bm.save(pid[6:], bm.get(pid[6:])["code"])  # schema check; no node/project start
            else:
                sm.update(pid[7:])  # metadata only; never saved()/restart()
        decision(pid, aid, Decision(status="accepted"), request)
        return {"ok": True, "path": artifact["path"], "executed": False}
    except ProjectError as e:
        raise HTTPException(e.code, str(e)) from None
    except (TreeError, SyntaxError, UnicodeError, ValueError):
        raise HTTPException(422, "проверьте путь, синтаксис Python и историю чата") from None
