"""Лёгкий контракт Image: байты PNG/JPEG; ограниченное сохранение без декодирования."""

from __future__ import annotations

import re
import threading
import uuid
from pathlib import Path

from pioneer.engine.node import NodeError

MAX_IMAGE = 2 * 1024 * 1024
MAX_SAVED = 100
MAX_STORAGE = 50 * 1024 * 1024
IMAGE_NAME = re.compile(r"[a-f0-9]{32}\.(png|jpg)\Z")


def image_type(raw):
    if not isinstance(raw, bytes) or not raw:
        raise NodeError("нет изображения")
    if len(raw) > MAX_IMAGE:
        raise NodeError("изображение больше 2 МБ")
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff") and raw.endswith(b"\xff\xd9"):
        return "image/jpeg"
    raise NodeError("ожидается PNG или JPEG")


class ImageStore:
    def __init__(self, data_dir: Path):
        self.root = data_dir / "images"
        self._lock = threading.Lock()

    def directory(self, pid):
        if not re.fullmatch(r"[a-z0-9]{6,32}", pid):
            raise NodeError("неверный id проекта")
        folder = self.root / pid
        if self.root.is_symlink() or folder.is_symlink():
            raise NodeError("симлинки в хранилище изображений запрещены")
        return folder

    def save(self, pid, raw):
        ext = "png" if image_type(raw) == "image/png" else "jpg"
        with self._lock:
            folder = self.directory(pid)
            folder.mkdir(parents=True, exist_ok=True)
            files = [p for p in folder.iterdir() if IMAGE_NAME.fullmatch(p.name)]
            if (
                len(files) >= MAX_SAVED
                or sum(p.stat().st_size for p in files) + len(raw) > MAX_STORAGE
            ):
                raise NodeError("хранилище снимков заполнено (100 файлов / 50 МБ на проект)")
            name = uuid.uuid4().hex + "." + ext
            path = folder / name
            try:
                with path.open("xb") as f:
                    f.write(raw)
            except OSError:
                path.unlink(missing_ok=True)
                raise
            return name

    def path(self, pid, name):
        if not IMAGE_NAME.fullmatch(name):
            raise NodeError("неверное имя изображения")
        path = self.directory(pid) / name
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError(name)
        return path
