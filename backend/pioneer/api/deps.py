"""Доступ к объектам core из обработчиков."""
from __future__ import annotations

from fastapi import HTTPException, Request

from pioneer.board import Board
from pioneer.hal import HAL, BenchHAL
from pioneer.hw import HW


def board(request: Request) -> Board:
    return request.app.state.board


def hw(request: Request) -> HW:
    return request.app.state.hw


def hal(request: Request) -> HAL:
    return request.app.state.hal


def bench(request: Request) -> BenchHAL:
    h = request.app.state.hal
    if not isinstance(h, BenchHAL):
        raise HTTPException(404, "стенд доступен только с hal = bench")
    return h
