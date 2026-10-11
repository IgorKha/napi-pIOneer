"""Папки с кодом: проверка путей, лимиты и безопасная миграция старых файлов."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import zipfile
import weakref
from pathlib import Path, PurePosixPath

FILE_LIMIT = 5 * 1024 * 1024
TREE_LIMIT = 50 * 1024 * 1024
MAX_ENTRIES = 2000
META = "pioneer.json"
_LOCKS = weakref.WeakValueDictionary()
_LOCKS_GUARD = threading.Lock()


class TreeError(ValueError):
    pass


class CodeTree:
    def __init__(self, root: Path):
        self.root = root.absolute()
        with _LOCKS_GUARD:
            self.lock = _LOCKS.setdefault(self.root, threading.RLock())

    def path(self, name: str, *, metadata: bool = False) -> Path:
        parts = PurePosixPath(name).parts
        if (not name or name.startswith("/") or "\\" in name or "\0" in name or not parts
                or any(p in ("", "..", ".") or p.startswith(".pioneer-") for p in name.split("/"))
                or (not metadata and META in parts)):
            raise TreeError("недопустимый путь")
        p = self.root
        if p.is_symlink():
            raise TreeError("симлинки запрещены")
        for part in parts:
            p = p / part
            if p.is_symlink():
                raise TreeError("симлинки запрещены")
        if not p.resolve().is_relative_to(self.root.resolve()):
            raise TreeError("путь выходит за корень")
        return p

    def meta(self) -> dict:
        return json.loads(self.path(META, metadata=True).read_text())

    def set_meta(self, meta: dict) -> None:
        with self.lock:
            self._write(self.path(META, metadata=True), json.dumps(meta, ensure_ascii=False, indent=2).encode())

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".pioneer-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def files(self, *, hashes: bool = True) -> list[dict]:
        out = []
        for parent, dirs, files in os.walk(self.root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__" and not d.startswith(".pioneer-"))
            for name in sorted(dirs + files):
                if name == META or name.startswith(".pioneer-"):
                    continue
                rel = (Path(parent) / name).relative_to(self.root).as_posix()
                p = self.path(rel)
                if p.is_dir():
                    out.append({"path": rel, "kind": "dir"})
                elif p.is_file():
                    size = p.stat().st_size
                    entry = {"path": rel, "kind": "file", "size": size}
                    if size > FILE_LIMIT:
                        entry["oversized"] = True
                    elif hashes:
                        entry["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
                    out.append(entry)
                else:
                    raise TreeError("разрешены только обычные файлы и папки")
                if len(out) > MAX_ENTRIES:
                    raise TreeError("слишком много файлов и папок")
        return out

    def read(self, name: str) -> bytes:
        p = self.path(name)
        if not p.is_file():
            raise TreeError("нет такого файла")
        if p.stat().st_size > FILE_LIMIT:
            raise TreeError("файл больше 5 МБ")
        return p.read_bytes()

    def write(self, name: str, data: bytes) -> None:
        with self.lock:
            p = self.path(name)
            if len(data) > FILE_LIMIT:
                raise TreeError("файл больше 5 МБ")
            entries = self.files(hashes=False)
            total = sum(e.get("size", 0) for e in entries if not e.get("oversized")) - (p.stat().st_size if p.is_file() and p.stat().st_size <= FILE_LIMIT else 0)
            if total + len(data) > TREE_LIMIT or len(entries) + len(PurePosixPath(name).parts) > MAX_ENTRIES:
                raise TreeError("превышен лимит проекта (50 МБ / 2000 файлов и папок)")
            self._write(p, data)
            self._invalidate(p)

    def _invalidate(self, p):
        # Same-size edits in one second can otherwise retain a stale .pyc.
        if p.suffix == ".py":
            for cached in (p.parent / "__pycache__").glob(p.stem + ".*.pyc"):
                self.path(cached.relative_to(self.root).as_posix()).unlink()

    def mkdir(self, name: str) -> None:
        with self.lock:
            if len(self.files(hashes=False)) + len(PurePosixPath(name).parts) > MAX_ENTRIES:
                raise TreeError("слишком много файлов и папок")
            self.path(name).mkdir(parents=True, exist_ok=True)

    def move(self, name: str, target: str) -> None:
        with self.lock:
            src, dst = self.path(name), self.path(target)
            entry = self.meta().get("entry")
            if entry and (name == entry or entry.startswith(name + "/")):
                raise TreeError("сначала смените точку входа проекта")
            if dst.exists() or dst.is_relative_to(src):
                raise TreeError("путь назначения занят или вложен в исходную папку")
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)

    def delete(self, name: str) -> None:
        with self.lock:
            entry = self.meta().get("entry")
            if entry and (name == entry or entry.startswith(name + "/")):
                raise TreeError("нельзя удалить точку входа проекта")
            p = self.path(name)
            if p.is_dir():
                # Проверяем и вложенные симлинки перед рекурсивным удалением.
                CodeTree(p).files(hashes=False)
                shutil.rmtree(p)
            else:
                p.unlink()

    def export_to(self, target) -> None:
        with self.lock, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(META, json.dumps(self.meta(), ensure_ascii=False))
            entries = self.files(hashes=False)
            if any(e.get("oversized") for e in entries) or sum(e.get("size", 0) for e in entries) > TREE_LIMIT:
                raise TreeError("данные превышают лимиты экспорта; перенесите большие файлы отдельно")
            for e in entries:
                if e["kind"] == "dir":
                    z.writestr(e["path"] + "/", b"")
                else:
                    z.write(self.path(e["path"]), e["path"])

    def export(self) -> bytes:
        with io.BytesIO() as buf:
            self.export_to(buf)
            return buf.getvalue()

    def import_zip(self, data: bytes | Path) -> None:
        """Проверить архив на копии до записи, распаковывать по одному файлу."""
        with self.lock, tempfile.TemporaryDirectory(prefix=".pioneer-", dir=self.root.parent) as d:
            copy = CodeTree(Path(d) / "tree")
            self.files(hashes=False)  # запрещаем симлинки до copytree
            shutil.copytree(self.root, copy.root, copy_function=os.link, ignore=shutil.ignore_patterns("__pycache__"))
            staged: dict[str, bool] = {}
            try:
                with zipfile.ZipFile(io.BytesIO(data) if isinstance(data, bytes) else data) as z:
                    if len(z.infolist()) > MAX_ENTRIES:
                        raise TreeError("слишком много файлов в архиве")
                    total = 0
                    for info in z.infolist():
                        name = info.filename.rstrip("/")
                        if name == META:
                            continue
                        copy.path(name)
                        mode = stat.S_IFMT(info.external_attr >> 16)
                        if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                            raise TreeError("симлинки и специальные файлы запрещены")
                        if name in staged or info.file_size > FILE_LIMIT:
                            raise TreeError("дубликат пути или файл больше 5 МБ")
                        total += info.file_size
                        if total > TREE_LIMIT:
                            raise TreeError("архив больше 50 МБ после распаковки")
                        staged[name] = info.is_dir()
                        copy.path(name).mkdir(parents=True, exist_ok=True) if info.is_dir() else copy._write(copy.path(name), z.read(info))
            except (zipfile.BadZipFile, RuntimeError) as e:
                raise TreeError("повреждённый или зашифрованный ZIP") from e
            entries = copy.files(hashes=False)
            if sum(e.get("size", 0) for e in entries if not e.get("oversized")) > TREE_LIMIT:
                raise TreeError("превышен лимит проекта (50 МБ)")
            for name, is_dir in staged.items():
                if is_dir:
                    self.path(name).mkdir(parents=True, exist_ok=True)
                else:
                    self._write(self.path(name), copy.read(name))
                    self._invalidate(self.path(name))


def project_tree(data_dir: Path, kind: str, ident: str) -> CodeTree:
    if kind not in ("scripts", "blocks", "lib") or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,63}|[a-z0-9]{6,32}", ident):
        raise TreeError("недопустимый тип или id проекта")
    base = data_dir / kind
    if base.is_symlink():
        raise TreeError("симлинки запрещены")
    tree = CodeTree(base / ident)
    tree.path(META, metadata=True)
    return tree


def migrate(base: Path, entry: str) -> None:
    """Публикуем готовую папку rename-ом; исходники удаляем только после публикации."""
    base.mkdir(parents=True, exist_ok=True)
    if base.is_symlink():
        raise TreeError("симлинки запрещены")
    for py in base.glob("*.py"):
        if py.is_symlink():
            raise TreeError("симлинки запрещены")
        if not re.fullmatch(r"[a-z0-9]{6,32}", py.stem):
            continue
        tree = project_tree(base.parent, base.name, py.stem)
        if not tree.root.exists():
            meta_file = py.with_suffix(".json")
            meta = json.loads(meta_file.read_text()) if meta_file.exists() else {"name": py.stem}
            meta.setdefault("entry", entry)
            with tempfile.TemporaryDirectory(prefix=".pioneer-", dir=base) as d:
                staged = CodeTree(Path(d) / "tree")
                staged.write(entry, py.read_bytes())
                staged.set_meta(meta)
                staged.root.rename(tree.root)
        # Если миграция была прервана после rename, папка уже содержит оригинал.
        if tree.path(entry).exists() and tree.path(META, metadata=True).exists():
            if tree.read(entry) != py.read_bytes():
                raise TreeError(f"конфликт миграции {py.name}: оригинал сохранён")
            py.unlink()
            py.with_suffix(".json").unlink(missing_ok=True)
