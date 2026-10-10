"""Конфиг сервиса: config.toml рядом с данными (spec 5). Путь — $PIONEER_CONFIG."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parents[2]
# Скрытый каталог: soft_reset делает `rm -rf /mount/rfs/rw/*`, dot-каталоги переживают (spec 11.1)
BOARD_DATA_DIR = Path("/mount/rfs/rw/.pioneer/data")


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8100
    hal: Literal["auto", "bench", "linux"] = "auto"  # bench на плате — режим «всухую»
    board_file: Path = REPO_ROOT / "boards" / "napi-c.yaml"
    uenv_file: Path = Path("/boot/uEnv.txt")
    data_dir: Path = REPO_ROOT / ".devdata"
    # Если uenv_file не читается (Mac, тесты): оверлеи, как в дефолтном uEnv 0.3.1
    default_overlays: tuple[str, ...] = (
        "rk3308-uart1", "rk3308-uart2-m0", "rk3308-uart3-m0", "rk3308-i2c1-ds1338",
        "rk3308-i2c3-m0", "rk3308-usb20-host",
    )


def load_config(path: Path | None = None) -> Config:
    path = path or (Path(p) if (p := os.environ.get("PIONEER_CONFIG")) else None)
    cfg = Config()
    if path is None or not path.exists():
        return cfg
    raw = tomllib.loads(path.read_text())
    for section in raw.values():
        for key, value in section.items():
            if not hasattr(cfg, key):
                raise ValueError(f"{path}: неизвестный параметр {key!r}")
            default = getattr(cfg, key)
            setattr(cfg, key, Path(value) if isinstance(default, Path) else value)
    return cfg
