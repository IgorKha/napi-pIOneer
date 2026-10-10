"""Виртуальный стенд: модели устройств, подключённые к пинам и шинам (hal = bench)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from pioneer.api import deps
from pioneer.hal import BenchHAL
from pioneer.hal.bench import BenchError
from pioneer.hal.models import MODEL_KINDS

router = APIRouter(prefix="/bench")


@router.get("")
def get_bench(request: Request) -> dict:
    hal = deps.hal(request)
    return {
        "enabled": isinstance(hal, BenchHAL),
        "kinds": [{"kind": k, "title": m.title, "params": m.params, "actions": m.actions}
                  for k, m in MODEL_KINDS.items()],
        "models": hal.snapshot() if isinstance(hal, BenchHAL) else [],
    }


class AddModel(BaseModel):
    kind: str
    id: str | None = None
    config: dict[str, Any] = {}


@router.post("/models")
def add_model(body: AddModel, request: Request) -> dict:
    try:
        return deps.bench(request).add(body.kind, body.id, **body.config).describe()
    except BenchError as e:
        raise HTTPException(422, str(e)) from e


@router.delete("/models/{model_id}")
def remove_model(model_id: str, request: Request) -> dict:
    try:
        deps.bench(request).remove(model_id)
    except BenchError as e:
        raise HTTPException(404, str(e)) from e
    return {"removed": model_id}


@router.post("/models/{model_id}/actions/{name}")
def model_action(model_id: str, name: str, request: Request, payload: dict[str, Any] | None = None) -> dict:
    try:
        return deps.bench(request).action(model_id, name, payload)
    except BenchError as e:
        raise HTTPException(404, str(e)) from e
    except (ValueError, KeyError) as e:
        raise HTTPException(422, str(e)) from e
