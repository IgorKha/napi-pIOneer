"""Секреты: имена читаются, значения только пишутся (spec 6)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/secrets")


@router.get("")
def list_secrets(request: Request) -> dict:
    return {"names": request.app.state.secrets.names()}


@router.put("")
def update_secrets(values: dict[str, str | None], request: Request) -> dict:
    try:
        return {"names": request.app.state.secrets.update(values)}
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
