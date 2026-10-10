"""Python-блоки пользователя: CRUD, сохранение с проверкой схемы, проверка без сохранения."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from pioneer.userblocks import BlockError, BlockManager

router = APIRouter(prefix="/blocks")


def _bm(request: Request) -> BlockManager:
    return request.app.state.blocks


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except BlockError as e:
        raise HTTPException(e.code, str(e)) from e


@router.get("")
def list_blocks(request: Request) -> list[dict]:
    return _bm(request).list()


class CreateBlock(BaseModel):
    name: str
    code: str | None = None


@router.post("")
def create_block(body: CreateBlock, request: Request) -> dict:
    return _bm(request).create(body.name, body.code)


@router.post("/validate")
def validate_block(body: CreateBlock, request: Request) -> dict:
    return _bm(request).validate(body.code or "")


@router.get("/{slug}")
def get_block(slug: str, request: Request) -> dict:
    return _guard(_bm(request).get, slug)


class SaveBlock(BaseModel):
    code: str
    name: str | None = None


@router.put("/{slug}")
def save_block(slug: str, body: SaveBlock, request: Request) -> dict:
    _guard(_bm(request).get, slug)
    return _bm(request).save(slug, body.code, body.name)


@router.delete("/{slug}")
def delete_block(slug: str, request: Request) -> dict:
    _guard(_bm(request).get, slug)
    _bm(request).delete(slug)
    return {"deleted": slug}
