"""Versioned trusted peripheral packages. Python executes in existing block/model workers."""

import io
import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
import yaml
from pioneer.codetree import CodeTree, TreeError, TREE_LIMIT
from pioneer.dependencies import install as install_dependencies
from pioneer.userblocks import extract_schema, _proxy_type
from pioneer.hal.models import MODEL_KINDS
from pioneer.module_models import ModuleModel

log = logging.getLogger(__name__)


class Modules:
    def __init__(self, data, registry, hal):
        self.data, self.registry, self.hal = data, registry, hal
        self.root = data / "modules"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.records = {}
        for path in self.root.glob("*/pioneer.json"):
            try:
                meta = CodeTree(path.parent).meta()
                self.register(path.parent, meta)
            except (OSError, ValueError, KeyError):
                log.exception("module catalog entry skipped")

    def list(self):
        return list(self.records.values())

    def path(self, ident, version):
        if (
            not re.fullmatch("[a-z][a-z0-9_]{0,39}", ident)
            or not isinstance(version, int)
            or not 1 <= version <= 9999
        ):
            raise TreeError("неверный id/версия модуля")
        return self.root / f"{ident}-v{version}"

    def register(self, root, meta):
        ident, version = meta["id"], meta["version"]
        types = []
        try:
            for schema in meta["nodes"]:
                cls = _proxy_type(
                    schema,
                    root / meta["entry"],
                    self.registry,
                    prefix=f"module.{ident}.",
                    version=version,
                )
                types.append(cls.TYPE.key)
        except Exception:
            for key in types:
                self.registry.remove(key)
            raise
        kind = f"module.{ident}@{version}"
        model = meta.get("model")
        if model:
            widget = (
                f"/pioneer/api/modules/{ident}/{version}/widget" if meta.get("widget") else None
            )
            MODEL_KINDS[kind] = type(
                "ModuleSimulation",
                (ModuleModel,),
                {
                    "kind": kind,
                    "title": meta["title"],
                    "root": root,
                    "simulation": model["entry"],
                    "widget": widget,
                    "params": model.get("params", {}),
                    "actions": model.get("actions", []),
                },
            )
        self.records[(ident, version)] = {
            **meta,
            "root": str(root),
            "types": types,
            "model_kind": kind if model else None,
        }

    def install(self, data, confirmed, source="zip", commit=None):
        if not confirmed:
            raise TreeError("подтвердите доверие Python-коду модуля")
        if (data.stat().st_size if isinstance(data, Path) else len(data)) > TREE_LIMIT:
            raise TreeError("модуль больше 50 МиБ")
        with (
            self.lock,
            tempfile.TemporaryDirectory(dir=self.data, prefix=".pioneer-module-") as tmp,
        ):
            tree = CodeTree(Path(tmp) / "stage")
            tree.root.mkdir()
            tree.import_zip(data)
            manifest = tree.path("module.yaml")
            if manifest.stat().st_size > 16384:
                raise TreeError("module.yaml больше 16 КиБ")
            raw = yaml.safe_load(manifest.read_text())
            ident, version = raw["id"], raw["version"]
            dest = self.path(ident, version)
            entry = raw.get("entry", "block.py")
            tree.path(entry)
            title = raw.get("title", ident)
            if not isinstance(title, str) or len(title) > 120:
                raise TreeError("неверное название модуля")
            readme = tree.path("README.md")
            if readme.stat().st_size > 65536:
                raise TreeError("README.md больше 64 КиБ")
            if dest.exists():
                from pioneer.project_bundle import digest

                if digest(CodeTree(dest)) != digest(tree):
                    raise TreeError("эта версия уже установлена с другим кодом; увеличьте version")
                return {"changed": False, **self.records[(ident, version)]}
            model = raw.get("model")
            widget = raw.get("widget")
            if model:
                tree.path(model["entry"])
                if (
                    len(model.get("params", {})) > 16
                    or len(model.get("actions", [])) > 16
                    or any(not isinstance(a, str) or len(a) > 40 for a in model.get("actions", []))
                ):
                    raise TreeError("неверный манифест модели")
                json.dumps(model, allow_nan=False)
            if widget:
                file = tree.path(widget)
                if file.suffix != ".js" or file.stat().st_size > 65536:
                    raise TreeError("widget: JS до 64 КиБ")
            requirements = tree.path("requirements.txt")
            # Final root is needed so dependency site is stable; remove on any failure.
            shutil.copytree(tree.root, dest)
            try:
                if requirements.exists():
                    install_dependencies(dest, True)
                result = extract_schema(dest / entry)
                if not result.get("ok"):
                    raise TreeError("схема модуля: " + result.get("error", "нет нод"))
                meta = {
                    "id": ident,
                    "version": version,
                    "title": title,
                    "entry": entry,
                    "nodes": result["nodes"],
                    "model": model,
                    "widget": widget,
                    "source": source,
                    "commit": commit,
                    "connection": str(raw.get("connection", ""))[:4000],
                }
                CodeTree(dest).set_meta(meta)
                self.register(dest, meta)
            except BaseException:
                shutil.rmtree(dest)
                from pioneer.dependencies import site_for

                site = site_for(dest)
                if site.exists():
                    shutil.rmtree(site)
                raise
            return {"changed": True, **self.records[(ident, version)]}

    def git(self, url, ref, confirmed):
        if not confirmed:
            raise TreeError("подтвердите доверие git-репозиторию")
        u = urlsplit(url)
        if (
            u.scheme != "https"
            or not u.hostname
            or u.username
            or u.password
            or u.query
            or u.fragment
        ):
            raise TreeError("источник git: HTTPS URL без credentials/query")
        if not re.fullmatch("[A-Za-z0-9][A-Za-z0-9_./-]{0,100}", ref) or ".." in ref:
            raise TreeError("укажите фиксированный тег или ветку")
        with tempfile.TemporaryDirectory(dir=self.data) as tmp:
            root = Path(tmp) / "repo"
            try:
                r = subprocess.run(
                    [
                        "git",
                        "-c",
                        "core.hooksPath=/dev/null",
                        "clone",
                        "--depth",
                        "1",
                        "--single-branch",
                        "--branch",
                        ref,
                        "--",
                        url,
                        str(root),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=90,
                )
            except subprocess.TimeoutExpired:
                raise TreeError("git clone превысил 90 с") from None
            if r.returncode:
                raise TreeError("не удалось клонировать репозиторий")
            commit = subprocess.check_output(
                ["git", "-C", str(root), "rev-parse", "HEAD"], timeout=5, text=True
            ).strip()
            shutil.rmtree(root / ".git")
            t = CodeTree(root)
            t.set_meta({})
            archive = Path(tmp) / "module.zip"
            t.export_to(archive)
            return self.install(archive, True, url, commit)

    def remove(self, ident, version):
        with self.lock:
            meta = self.records[(ident, version)]
            for key in meta["types"]:
                self.registry.remove(key)
            kind = meta.get("model_kind")
            if kind in MODEL_KINDS:
                del MODEL_KINDS[kind]
            from pioneer.dependencies import remove

            remove(self.path(ident, version))
            shutil.rmtree(self.path(ident, version))
            del self.records[(ident, version)]
