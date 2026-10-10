"""Справка (docs/ideas/004): справочник SDK из кода + руководства + схемы нод."""
from __future__ import annotations

from fastapi import APIRouter, Request

from pioneer.help import build_reference

router = APIRouter()


@router.get("/help")
def help_reference(request: Request) -> dict:
    # поддерживающие пины/интерфейсы — по текущему состоянию платы (оверлеи, живой DT)
    return build_reference(request.app.state.board, request.app.state.projects.registry)
