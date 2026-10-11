"""Install trusted kernel driver packages without adding device-specific core code."""
from __future__ import annotations

import difflib
import hashlib
import io
import json
import logging
import os
import re
import struct
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from pioneer.codetree import CodeTree
from pioneer.board import load_board, parse_uenv_overlays
from pioneer.kernel_drivers import DriverError

log = logging.getLogger(__name__)
UPLOAD_LIMIT = 16 * 1024 * 1024
EXPANDED_LIMIT = 32 * 1024 * 1024
IDENTIFIER = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_-]{0,63}")


class KernelPackages:
    def __init__(self, drivers):
        self.drivers = drivers
        self.cfg, self.hw = drivers.cfg, drivers.hw
        self.root = self.cfg.data_dir / "kernel-packages" / drivers.release
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.settings = self.cfg.data_dir / "kernel-packages.json"
        self.lock = threading.RLock()

    def identifier(self, value):
        if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
            raise DriverError("неверный идентификатор драйвера/модуля")
        return value

    def path(self, ident):
        return self.root / self.identifier(ident)

    def meta(self, ident):
        try:
            return json.loads((self.path(ident) / "manifest.json").read_text())
        except FileNotFoundError:
            raise DriverError("пакет драйвера не установлен", 404) from None

    def enabled(self):
        try:
            data = json.loads(self.settings.read_text())
        except FileNotFoundError:
            return []
        names = data.get("autoload") if isinstance(data, dict) else None
        if not isinstance(names, list) or any(not isinstance(n, str) or not IDENTIFIER.fullmatch(n) for n in names):
            raise DriverError("неверная конфигурация автозагрузки пакетов")
        return names

    def loaded(self, name):
        return (self.cfg.kernel_sys_dir / "module" / name.replace("-", "_")).is_dir()

    def package_for_module(self, name):
        # Immutable manifests are atomically installed; do not take package lock
        # here because device profiles may already hold the shared boot lock.
        for file in self.root.glob("*/manifest.json"):
            try:
                m = json.loads(file.read_text())
                if any(item["name"] == name for item in m["modules"]):
                    return m["id"]
            except FileNotFoundError:
                continue
            except (OSError, ValueError, KeyError, TypeError):
                log.warning("Invalid kernel package manifest: %s", file)
        return None

    def catalog(self):
        enabled = self.enabled()
        result = []
        for file in sorted(self.root.glob("*/manifest.json")):
            m = self.meta(file.parent.name)
            result.append({**m, "autoload": m["id"] in enabled,
                           "loaded": all(self.loaded(x["name"]) for x in m["modules"]),
                           "modules": [{**x, "loaded": self.loaded(x["name"])} for x in m["modules"]]})
        return {"kernel": self.drivers.release, "real": self.hw.hal.name == "linux", "packages": result}

    def validate_module(self, file):
        data = file.read_bytes()
        if (len(data) < 64 or len(data) > UPLOAD_LIMIT or data[:6] != b"\x7fELF\x02\x01"
                or struct.unpack_from("<HH", data, 16) != (1, 183)):
            raise DriverError("нужен несжатый ELF-модуль ARM64 (.ko) до 16 МиБ")
        command = self.drivers.command
        name = self.identifier(command(["modinfo", "-F", "name", str(file)]))
        vermagic = command(["modinfo", "-F", "vermagic", str(file)])
        if vermagic != self.drivers.expected_vermagic():
            raise DriverError("модуль не соответствует текущему ядру/ABI: " + self.drivers.release)
        return {"file": file.name, "name": name, "vermagic": vermagic,
                "description": command(["modinfo", "-F", "description", str(file)]),
                "license": command(["modinfo", "-F", "license", str(file)]),
                "depends": command(["modinfo", "-F", "depends", str(file)]),
                "sha256": hashlib.sha256(data).hexdigest()}

    def install(self, data):
        self.drivers.real()
        if len(data) > UPLOAD_LIMIT:
            raise DriverError("пакет превышает 16 МиБ")
        with self.lock, tempfile.TemporaryDirectory(dir=self.root) as tmp:
            stage = Path(tmp) / "package"
            stage.mkdir(mode=0o700)
            if data.startswith(b"PK"):
                try:
                    with zipfile.ZipFile(io.BytesIO(data)) as archive:
                        files = archive.infolist()
                        if len(files) > 32 or sum(f.file_size for f in files) > EXPANDED_LIMIT:
                            raise DriverError("слишком большой распакованный пакет")
                        names = [f.filename for f in files]
                        if len(set(names)) != len(names):
                            raise DriverError("повторяющиеся файлы в пакете")
                        for f in files:
                            if (not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", f.filename) or f.filename in (".", "..")
                                    or f.is_dir() or f.flag_bits & 1
                                    or (f.external_attr >> 16) & 0o170000 == 0o120000):
                                raise DriverError("пакет должен содержать обычные файлы без каталогов и ссылок")
                            CodeTree._write(stage / f.filename, archive.read(f))
                    manifest_file = stage / "manifest.json"
                    if not manifest_file.exists() or manifest_file.stat().st_size > 65536:
                        raise DriverError("нужен manifest.json до 64 КБ")
                    raw = json.loads(manifest_file.read_text())
                except (zipfile.BadZipFile, RuntimeError, UnicodeError, json.JSONDecodeError):
                    raise DriverError("неверный ZIP/manifest.json") from None
            else:
                CodeTree._write(stage / "driver.ko", data)
                raw = {"modules": ["driver.ko"]}
            if not isinstance(raw, dict):
                raise DriverError("manifest.json должен быть объектом")
            modules = raw.get("modules")
            if not isinstance(modules, list) or not 1 <= len(modules) <= 16:
                raise DriverError("modules: список .ko в порядке загрузки (1–16)")
            metadata = []
            for name in modules:
                if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]+\.ko", name):
                    raise DriverError("неверное имя .ko в manifest")
                metadata.append(self.validate_module(stage / name))
            module_names = [m["name"] for m in metadata]
            if len(set(module_names)) != len(module_names):
                raise DriverError("имена модулей пакета должны различаться")
            ident = self.identifier(raw.get("id", module_names[0]))
            title = raw.get("title", metadata[0]["description"] or ident)
            if not isinstance(title, str) or len(title) > 200:
                raise DriverError("название пакета должно быть строкой до 200 символов")
            for p in self.catalog()["packages"]:
                if p["id"] != ident and any(m["name"] in module_names for m in p["modules"]):
                    raise DriverError("модуль уже принадлежит другому пакету", 409)
            if any(self.loaded(n) for n in module_names):
                raise DriverError("модуль уже загружен; остановите потребителей и выгрузите его перед установкой", 409)
            pins = raw.get("pins", [])
            if not isinstance(pins, list) or any(not isinstance(p, str) or p not in self.drivers.board.pins or not self.drivers.board.pins[p].gpio for p in pins):
                raise DriverError("pins: список GPIO-пинов J9/J10, которые занимает оверлей")
            nodes = raw.get("device_nodes", [])
            if not isinstance(nodes, list) or any(not isinstance(n, str) or not re.fullmatch(r"/[a-zA-Z0-9_@,./-]+", n) or ".." in n for n in nodes):
                raise DriverError("device_nodes: абсолютные пути устройств в Device Tree")
            overlay = raw.get("overlay")
            if overlay is not None:
                if not isinstance(overlay, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]+\.dtbo", overlay):
                    raise DriverError("неверное имя .dtbo")
                blob = (stage / overlay).read_bytes()
                if len(blob) < 40 or len(blob) > 256 * 1024 or struct.unpack_from(">II", blob) != (0xd00dfeed, len(blob)):
                    raise DriverError("неверный DTBO")
            allowed = set(modules) | {"manifest.json"} | ({overlay} if overlay else set())
            if any(p.name not in allowed for p in stage.iterdir()):
                raise DriverError("в пакете есть неописанные файлы")
            params = raw.get("parameters", {})
            if not isinstance(params, dict) or any(k not in module_names for k in params):
                raise DriverError("parameters: параметры по именам модулей")
            for values in params.values():
                if not isinstance(values, list) or any(not isinstance(v, str) or len(v) > 256 or not re.fullmatch(r"[a-zA-Z0-9_]+=[a-zA-Z0-9_.,:/+-]*", v) for v in values):
                    raise DriverError("параметры должны быть списком key=value")
            manifest = {"id": ident, "title": title, "modules": metadata, "overlay": overlay,
                        "pins": list(dict.fromkeys(pins)), "device_nodes": nodes, "parameters": params}
            CodeTree._write(stage / "manifest.json", json.dumps(manifest, ensure_ascii=False).encode())
            dest = self.path(ident)
            if dest.exists() and any(self.loaded(x["name"]) for x in self.meta(ident)["modules"]):
                raise DriverError("предыдущая версия пакета ещё загружена", 409)
            previous = Path(tmp) / "previous"
            if dest.exists():
                os.replace(dest, previous)
            try:
                os.replace(stage, dest)
            except OSError:
                if previous.exists():
                    os.replace(previous, dest)
                raise
            log.info("Kernel package installed id=%s modules=%s", ident, module_names)
            return manifest

    def load(self, ident):
        self.drivers.real()
        with self.lock:
            m = self.meta(ident)
            started = []
            try:
                for item in m["modules"]:
                    if self.loaded(item["name"]):
                        continue
                    file = self.path(ident) / item["file"]
                    verified = self.validate_module(file)
                    if verified["sha256"] != item["sha256"]:
                        raise DriverError("файл модуля изменился после установки", 409)
                    for dep in filter(None, item["depends"].split(",")):
                        self.identifier(dep)
                        if not self.loaded(dep):
                            self.drivers.command(["modprobe", dep])
                    self.drivers.command(["insmod", str(file), *m["parameters"].get(item["name"], [])])
                    if not self.loaded(item["name"]):
                        raise DriverError("модуль не появился в /sys/module")
                    started.append(item["name"])
            except DriverError:
                # Only roll back modules started by this attempt, never force unloading.
                for name in reversed(started):
                    try:
                        self.drivers.command(["rmmod", name])
                    except DriverError:
                        log.exception("Kernel package partial load id=%s module=%s", ident, name)
                raise
            return {"loaded": True, "changed": bool(started)}

    def unload(self, ident):
        self.drivers.real()
        with self.lock:
            m = self.meta(ident)
            if ident in self.enabled():
                raise DriverError("сначала отключите автозагрузку пакета", 409)
            for item in reversed(m["modules"]):
                if self.loaded(item["name"]):
                    self.drivers.command(["rmmod", item["name"]])
            return {"loaded": False}

    def set_autoload(self, ident, enabled):
        self.drivers.real()
        with self.lock:
            self.meta(ident)
            names = self.enabled()
            names = [n for n in names if n != ident]
            if enabled:
                names.append(ident)
            CodeTree._write(self.settings, json.dumps({"autoload": names}).encode())
            return {"autoload": enabled}

    def autoload(self):
        if self.hw.hal.name != "linux":
            return
        try:
            names = self.enabled()
        except (OSError, ValueError, KeyError):
            log.exception("Kernel package autoload configuration invalid")
            return
        for ident in names:
            try:
                self.load(ident)
            except (DriverError, OSError, ValueError):
                log.exception("Kernel package autoload failed id=%s", ident)

    def _plan(self, ident, enabled):
        m = self.meta(ident)
        if not m["overlay"]:
            raise DriverError("у этого пакета нет boot-оверлея")
        text = self.drivers.boot_text()
        overlay_name = "pioneer-driver-" + ident
        match = re.search(r"^user_overlays=(.*)$", text, re.M)
        names = match.group(1).split() if match else []
        names = [n for n in names if n != overlay_name]
        if enabled:
            names.append(overlay_name)
        line = "user_overlays=" + " ".join(names)
        updated = re.sub(r"^user_overlays=.*$", line, text, flags=re.M) if match else text + ("" if text.endswith("\n") else "\n") + line + "\n"
        blob = (self.path(ident) / m["overlay"]).read_bytes()
        blockers = []
        if self.hw.hal.name != "linux":
            blockers.append("boot-оверлей подключается только на реальной плате")
        if enabled:
            pending = load_board(self.cfg.board_file, parse_uenv_overlays(text))
            for pin in m["pins"]:
                if pending.function(pin) != "gpio":
                    blockers.append("после загрузки пин занят интерфейсом: " + pin)
                if pin in self.hw.res.claims:
                    blockers.append("пин используется: " + pin)
                if any(u.node not in m["device_nodes"] for u in self.drivers.board._users(pin)):
                    blockers.append("пин занят драйвером/интерфейсом: " + pin)
        compiled = False
        source = ""
        if self.hw.hal.name == "linux":
            with tempfile.TemporaryDirectory(dir=self.root) as tmp:
                supplied = Path(tmp) / "supplied.dtbo"
                supplied.write_bytes(blob)
                source = self.drivers.command(["dtc", "-I", "dtb", "-O", "dts", str(supplied)])
                values = dict(l.split("=", 1) for l in updated.splitlines() if "=" in l and not l.startswith("#"))
                fdtfile = values.get("fdtfile", "")
                if not re.fullmatch(r"[a-zA-Z0-9_.-]+\.dtb", fdtfile):
                    raise DriverError("неверный fdtfile")
                chain = []
                for key, directory in (("overlays", self.cfg.overlays_dir), ("user_overlays", self.cfg.user_overlays_dir)):
                    for name in values.get(key, "").split():
                        self.identifier(name)
                        chain.append(supplied if name == overlay_name and key == "user_overlays" else directory / (name + ".dtbo"))
                self.drivers.command(["fdtoverlay", "-i", str(self.cfg.uenv_file.parent / fdtfile), "-o", str(Path(tmp) / "validated.dtb"), *map(str, chain)])
                compiled = True
        digest = hashlib.sha256(text.encode() + blob + json.dumps(m, sort_keys=True).encode() + str(enabled).encode()).hexdigest()
        return {"id": ident, "enabled": enabled, "hash": digest, "pins": m["pins"],
                "source": source,
                "ready": not blockers, "blockers": blockers, "compiled": compiled,
                "diff": "".join(difflib.unified_diff(text.splitlines(True), updated.splitlines(True), fromfile="uEnv.txt", tofile="после применения")),
                "needs_reboot": True}, updated, blob, overlay_name

    def plan(self, ident, enabled):
        with self.lock, self.drivers.overlays.lock:
            return self._plan(ident, enabled)[0]

    def apply(self, ident, enabled, expected_hash):
        self.drivers.real()
        with self.lock, self.drivers.overlays.lock:
            plan, text, blob, name = self._plan(ident, enabled)
            if plan["hash"] != expected_hash or not plan["ready"]:
                raise DriverError("предпросмотр устарел или есть конфликты", 409)
            with self.hw._lock, self.hw.res._lock:
                if enabled and any(p in self.hw.res.claims for p in plan["pins"]):
                    raise DriverError("GPIO занят", 409)
                # Catch external uEnv changes after compilation.
                old = self.drivers.boot_text()
                m = self.meta(ident)
                digest = hashlib.sha256(old.encode() + blob + json.dumps(m, sort_keys=True).encode() + str(enabled).encode()).hexdigest()
                if digest != expected_hash:
                    raise DriverError("uEnv изменился — обновите предпросмотр", 409)
                backup = self.root / ("backup-" + str(time.time_ns()))
                backup.mkdir(mode=0o700)
                dest = self.cfg.user_overlays_dir / (name + ".dtbo")
                mode = self.cfg.uenv_file.stat().st_mode & 0o777
                CodeTree._write(backup / "uEnv.txt", old.encode())
                if dest.exists():
                    CodeTree._write(backup / "overlay.dtbo", dest.read_bytes())
                try:
                    if enabled:
                        CodeTree._write(dest, blob)
                    CodeTree._write(self.cfg.uenv_file, text.encode())
                    os.chmod(self.cfg.uenv_file, mode)
                except OSError:
                    if (backup / "overlay.dtbo").exists():
                        CodeTree._write(dest, (backup / "overlay.dtbo").read_bytes())
                    elif enabled:
                        dest.unlink(missing_ok=True)
                    CodeTree._write(self.cfg.uenv_file, old.encode())
                    os.chmod(self.cfg.uenv_file, mode)
                    raise
                return {"changed": True, "needs_reboot": True, "backup": str(backup)}
