import pytest

from pioneer.board import load_board
from pioneer.config import REPO_ROOT, Config

BOARD_FILE = REPO_ROOT / "boards" / "napi-c.yaml"
# Дефолтный uEnv NapiLinux 0.3.1
DEFAULT_OVERLAYS = list(Config().mock_overlays)


@pytest.fixture
def board():
    return load_board(BOARD_FILE, DEFAULT_OVERLAYS)
