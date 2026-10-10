import time

import pytest

from pioneer.board import load_board
from pioneer.config import REPO_ROOT, Config
from pioneer.hal import BenchHAL
from pioneer.hw import HW
from pioneer.resources import ResourceManager

BOARD_FILE = REPO_ROOT / "boards" / "napi-c.yaml"
# Дефолтный uEnv NapiLinux 0.3.1
DEFAULT_OVERLAYS = list(Config().default_overlays)


@pytest.fixture
def board():
    return load_board(BOARD_FILE, DEFAULT_OVERLAYS)


@pytest.fixture
def bench(board):
    b = BenchHAL(board)
    yield b
    b.close()


@pytest.fixture
def hw(board, bench):
    h = HW(board, bench, ResourceManager(board))
    yield h
    h.close_all()


def wait_for(cond, timeout=1.0):
    """События стенда приходят из его потока — ждём, а не проверяем сразу."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()
