#!/usr/bin/env python3
"""pioneer push/pull/watch: синхронизация папки через файловое API, только stdlib."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

IGNORED = {".git", ".venv", "__pycache__", "node_modules", "pioneer.json"}
FILE_LIMIT = 5 * 1024 * 1024
TREE_LIMIT = 50 * 1024 * 1024


def local_files(root: Path) -> dict[str, bytes]:
    if not root.is_dir() or root.is_symlink():
        raise ValueError("нужна существующая папка проекта без симлинка")
    files, total = {}, 0
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(n for n in dirs if n not in IGNORED and not n.startswith(".pioneer-"))
        for name in dirs + names:
            if name in IGNORED or name.startswith(".pioneer-"):
                continue
            path = Path(base) / name
            if path.is_symlink():
                raise ValueError(f"симлинк запрещён: {path}")
            if path.is_dir():
                continue
            if not path.is_file() or path.stat().st_size > FILE_LIMIT:
                raise ValueError(f"файл недоступен или больше 5 МБ: {path}")
            blob = path.read_bytes()
            total += len(blob)
            if total > TREE_LIMIT or len(files) >= 2000:
                raise ValueError("превышен лимит проекта: 50 МБ / 2000 файлов")
            files[path.relative_to(root).as_posix()] = blob
    return files


def local_target(root: Path, name: str) -> Path:
    if not name or name.startswith("/") or "\\" in name or any(p in ("", ".", "..") for p in name.split("/")):
        raise ValueError(f"недопустимый путь сервера: {name}")
    path = root
    for part in PurePosixPath(name).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"симлинк запрещён: {path}")
    return path


def write_file(path: Path, blob: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".pioneer-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


class Client:
    def __init__(self, url: str, key: str, kind: str, ident: str, insecure: bool = False):
        if urlparse(url).scheme not in ("http", "https"):
            raise ValueError("URL должен начинаться с http:// или https://")
        self.base = url.rstrip("/") + "/pioneer/api/code/" + kind + "/" + quote(ident, safe="")
        self.key = key
        self.context = ssl._create_unverified_context() if insecure else ssl.create_default_context()

    def request(self, path: str, method: str = "GET", data=None, binary=False):
        headers = {"X-Pioneer-Key": self.key, "X-Pioneer": "1"}
        if data is not None and not isinstance(data, bytes):
            data = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        req = Request(self.base + "/" + path, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=30, context=self.context) as res:
                blob = res.read(TREE_LIMIT + 1)
                if len(blob) > TREE_LIMIT:
                    raise ValueError("слишком большой ответ сервера")
                result = blob if binary else json.loads(blob)
                return result
        except HTTPError as e:
            body = e.read(4096).decode("utf-8", "replace")
            raise ValueError(f"HTTP {e.code}: {body}") from None


def push(client: Client, root: Path, *, delete=False, restart=False) -> int:
    local = local_files(root)
    remote = client.request("tree")
    files = {f["path"]: f for f in remote["files"] if f["kind"] == "file"}
    count = 0
    for name, blob in local.items():
        if files.get(name, {}).get("sha256") != hashlib.sha256(blob).hexdigest():
            client.request("upload?" + urlencode({"path": name}), "POST", blob)
            print("↑", name, flush=True)
            count += 1
    meta_path = root / "pioneer.json"
    if meta_path.exists():
        if meta_path.is_symlink():
            raise ValueError("pioneer.json не должен быть симлинком")
        meta = json.loads(meta_path.read_text())
        fields = ("name", "entry", "autostart", "restart_on_save") if "/scripts/" in client.base else ("name",)
        patch = {k: meta[k] for k in fields if k in meta and meta[k] != remote["meta"].get(k)}
        if patch:
            client.request("meta", "PATCH", patch)
            count += 1
    if delete:
        for name in sorted(files.keys() - local.keys()):
            client.request("delete", "POST", {"path": name})
            print("×", name, flush=True)
            count += 1
    final = client.request("tree")
    if final["meta"].get("error"):
        raise ValueError(final["meta"]["error"])
    if count and restart:
        client.request("run", "POST", {})
    return count


def pull(client: Client, root: Path, *, delete=False) -> int:
    if root.is_symlink():
        raise ValueError("корень проекта не должен быть симлинком")
    root.mkdir(parents=True, exist_ok=True)
    old = local_files(root)
    remote = client.request("tree")
    entries = remote["files"]
    blobs, total = {}, 0
    # Проверяем все пути и хэши прежде, чем менять локальные файлы.
    for f in entries:
        local_target(root, f["path"])
        if f["kind"] != "file":
            continue
        blob = client.request("download?" + urlencode({"path": f["path"]}), binary=True)
        if len(blob) > FILE_LIMIT or hashlib.sha256(blob).hexdigest() != f["sha256"]:
            raise ValueError(f"файл изменился во время pull, повторите: {f['path']}")
        total += len(blob)
        if total > TREE_LIMIT:
            raise ValueError("проект больше 50 МБ")
        blobs[f["path"]] = blob
    count = 0
    for name, blob in blobs.items():
        if old.get(name) != blob:
            write_file(local_target(root, name), blob)
            print("↓", name)
            count += 1
    if delete:
        for name in old.keys() - blobs.keys():
            local_target(root, name).unlink()
            count += 1
    write_file(local_target(root, "pioneer.json"), json.dumps(remote["meta"], ensure_ascii=False, indent=2).encode())
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("push", "pull", "watch"))
    parser.add_argument("folder", type=Path)
    parser.add_argument("--project", required=True, help="id скрипта/блока или имя библиотеки")
    parser.add_argument("--kind", choices=("scripts", "blocks", "lib"), default="scripts")
    parser.add_argument("--url", default=os.environ.get("PIONEER_URL", "http://127.0.0.1:8100"))
    parser.add_argument("--delete", action="store_true", help="удалять файлы, отсутствующие у источника")
    parser.add_argument("--restart", action="store_true", help="перезапускать скрипт после push")
    parser.add_argument("--insecure", action="store_true", help="принимать самоподписанный HTTPS-сертификат")
    args = parser.parse_args()
    key = os.environ.get("PIONEER_CODE_KEY")
    if not key:
        parser.error("задайте PIONEER_CODE_KEY (секрет code_api_key на плате)")
    if args.restart and args.kind != "scripts":
        parser.error("--restart работает только для scripts")
    root = args.folder.absolute()
    client = Client(args.url, key, args.kind, args.project, args.insecure)
    try:
        if args.command == "pull":
            pull(client, root, delete=args.delete)
        elif args.command == "push":
            push(client, root, delete=args.delete, restart=args.restart)
        else:
            while True:
                try:
                    push(client, root, delete=args.delete, restart=args.restart)
                except URLError as e:
                    print(f"Нет связи: {e.reason}; повтор через 2 с", file=sys.stderr)
                time.sleep(2)
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError) as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
