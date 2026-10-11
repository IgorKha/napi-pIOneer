"""Shared wheel-only driver site, controlled Blinka resolution and project locks."""

import ast
import base64
import configparser
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
import uuid
import zipfile
from importlib.metadata import distributions
from pathlib import Path

BUNDLE = "https://raw.githubusercontent.com/adafruit/Adafruit_CircuitPython_Bundle/main/.gitmodules"
STUB_VERSION = "99.0.0"
FORBIDDEN = (
    "board",
    "busio",
    "digitalio",
    "analogio",
    "pwmio",
    "microcontroller",
    "adafruit_blinka",
)


def normalized(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "napi-pioneer"})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = response.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise ValueError("Ответ каталога больше 4 МиБ")
    return data


def stub_wheels(root):
    root.mkdir(parents=True, exist_ok=True)
    for name in ("Adafruit_Blinka", "Adafruit_PlatformDetect"):
        info = f"{name}-{STUB_VERSION}.dist-info"
        files = {
            f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name.replace('_', '-')}\nVersion: {STUB_VERSION}\nSummary: Pioneer compatibility stub\n\n".encode(),
            f"{info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: pioneer\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        }
        if name.endswith("PlatformDetect"):
            files["adafruit_platformdetect/__init__.py"] = (
                b'from types import SimpleNamespace\nclass Detector:\n    def __init__(self):\n        self.board = SimpleNamespace(id="napi_c", NAPI_C=True)\n        self.chip = SimpleNamespace(id="RK3308")\n'
            )
        rows = [
            (
                p,
                "sha256="
                + base64.urlsafe_b64encode(hashlib.sha256(b).digest()).rstrip(b"=").decode(),
                len(b),
            )
            for p, b in files.items()
        ]
        rows.append((f"{info}/RECORD", "", ""))
        record = io.StringIO()
        csv.writer(record).writerows(rows)
        files[f"{info}/RECORD"] = record.getvalue().encode()
        with zipfile.ZipFile(root / f"{name}-{STUB_VERSION}-py3-none-any.whl", "w") as wheel:
            for path, data in files.items():
                wheel.writestr(path, data)


def installed(site):
    return {normalized(d.metadata["Name"]): d.version for d in distributions(path=[str(site)])}


def validate_site(site):
    for name in FORBIDDEN:
        if (site / f"{name}.py").exists() or (site / name).exists():
            raise ValueError(f"Настоящая Blinka перекрывает Pioneer: {name}; установка отменена")
    versions = installed(site)
    for name in ("adafruit-blinka", "adafruit-platformdetect"):
        if versions.get(name) != STUB_VERSION:
            raise ValueError(f"Не установлена контролируемая заглушка {name}")
    for dist in distributions(path=[str(site)]):
        if not (site / dist._path.name / "WHEEL").is_file():
            raise ValueError("Драйвер должен быть установлен из wheel")
        if not any(
            line == "Root-Is-Purelib: true"
            for line in (site / dist._path.name / "WHEEL").read_text().splitlines()
        ):
            raise ValueError(f"Допускаются только чисто Python wheel: {dist.metadata['Name']}")
    return versions


class Drivers:
    def __init__(self, data):
        self.data = Path(data)
        self.root = self.data / "drivers/adafruit"
        self.root.mkdir(parents=True, exist_ok=True)
        self.site = self.root / "site"
        self.lock = threading.RLock()
        self.job = None

    def record_test(self, packages, mode, detail):
        """Evidence belongs to an exact installed version, never the package name alone."""
        if mode not in ("bench", "hardware"):
            raise ValueError("bench/hardware")
        with self.lock:
            path = self.root / "tested.json"
            evidence = json.loads(path.read_text()) if path.exists() else {}
            for package in packages:
                version = self.state()["versions"][package]
                key = package + "==" + version
                if mode == "hardware" or evidence.get(key, {}).get("mode") != "hardware":
                    evidence[key] = {"mode": mode, "detail": detail}
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
            os.replace(temp, path)

    def state(self):
        try:
            return json.loads((self.site / ".pioneer-adafruit.json").read_text())
        except FileNotFoundError:
            return {"requested": {}, "versions": {}}

    def inspect(self, package, path=""):
        """Read only files owned by an installed distribution, without importing it."""
        version = self.state()["versions"].get(package)
        if not version or not package.startswith("adafruit-circuitpython-"):
            raise ValueError("Драйвер не установлен: установите его в Устройства → Adafruit")
        site = self.site.resolve()
        dist = next(
            (
                d
                for d in distributions(path=[str(site)])
                if normalized(d.metadata["Name"]) == package
            ),
            None,
        )
        if dist is None:
            raise ValueError("Метаданные установленного драйвера не найдены")
        files = sorted(str(f) for f in dist.files or [] if str(f).endswith(".py"))
        result = {
            "package": package,
            "version": dist.version,
            "files": files,
            "summary": dist.metadata.get("Summary", ""),
            "docs": "https://pypi.org/project/" + package + "/",
        }
        from pioneer.adafruit_templates import (
            PROFILES,
            package as profile_package,
            snippet,
            block_template,
        )

        for chip, profile in PROFILES.items():
            if profile_package(chip) == package:
                result["profile"] = profile
                result["connection_example"] = snippet(chip, "i2c1", profile["address"])
                if chip in ("bme280", "sht4x", "pca9685"):
                    result["block_example"] = block_template(chip, "i2c1", profile["address"])
        if path:
            if path not in files:
                raise ValueError("Выберите Python-файл из списка files установленного драйвера")
            file = (site / path).resolve()
            if not file.is_relative_to(site) or not file.is_file():
                raise ValueError("Файл вне site драйверов")
            with file.open("r", encoding="utf-8") as stream:
                source = stream.read(24001)
            result.update(path=path, untrusted_source=source[:24000], truncated=len(source) > 24000)
        return result

    def uses(self, package):
        result = []
        module = package.removeprefix("adafruit-circuitpython-").replace("-", "_")
        aliases = {"charlcd": "character_lcd", "busdevice": "bus_device"}
        modules = {
            "adafruit_" + aliases.get(module, module),
            "adafruit_" + module,
            "adafruit_" + module.replace("busdevice", "bus_device"),
        }
        for kind in ("scripts", "blocks", "lib", "modules"):
            for meta in (self.data / kind).glob("*/pioneer.json"):
                root = meta.parent
                referenced = False
                for file in root.rglob("*.py"):
                    if file.stat().st_size > 1024 * 1024 or file.is_symlink():
                        continue
                    try:
                        tree = ast.parse(file.read_text())
                    except (SyntaxError, UnicodeError):
                        continue
                    if any(
                        (
                            isinstance(n, ast.Import)
                            and any(x.name.split(".")[0] in modules for x in n.names)
                        )
                        or (
                            isinstance(n, ast.ImportFrom)
                            and (n.module or "").split(".")[0] in modules
                        )
                        for n in ast.walk(tree)
                    ):
                        referenced = True
                        break
                lock = root / "adafruit.lock.json"
                if lock.exists() and package in json.loads(lock.read_text()).get("drivers", {}):
                    referenced = True
                if referenced:
                    result.append(
                        {
                            "kind": kind,
                            "id": root.name,
                            "name": json.loads(meta.read_text()).get(
                                "name", json.loads(meta.read_text()).get("title", root.name)
                            ),
                        }
                    )
        # Graph references to custom blocks are included so users can stop affected projects.
        used_blocks = set()
        for item in result:
            if item["kind"] in ("blocks", "modules"):
                meta = json.loads(
                    (self.data / item["kind"] / item["id"] / "pioneer.json").read_text()
                )
                prefix = "user." if item["kind"] == "blocks" else f"module.{meta['id']}."
                used_blocks.update(prefix + n["id"] for n in meta.get("nodes", []))
        if used_blocks:
            for path in (self.data / "projects").glob("*.json"):
                graph = json.loads(path.read_text())
                if any(
                    n.get("type", "").split("@")[0] in used_blocks
                    for n in graph.get("graph", {}).get("nodes", [])
                ):
                    result.append(
                        {"kind": "projects", "id": path.stem, "name": graph.get("name", path.stem)}
                    )
        return result

    def catalog(self, refresh=False):
        cache = self.root / "catalog.json"
        offline = False
        if refresh or not cache.exists():
            try:
                parser = configparser.ConfigParser()
                parser.read_string(fetch(BUNDLE).decode())
                rows = []
                for section in parser.sections():
                    url = parser[section].get("url", "")
                    name = url.rsplit("/", 1)[-1].removesuffix(".git")
                    if not name.startswith("Adafruit_CircuitPython_"):
                        continue
                    chip = name.removeprefix("Adafruit_CircuitPython_").lower()
                    package = "adafruit-circuitpython-" + chip.replace("_", "-")
                    if chip == "busdevice":
                        package = "adafruit-circuitpython-busdevice"
                    rows.append(
                        {
                            "package": package,
                            "chip": chip,
                            "url": url.removesuffix(".git"),
                            "compatibility": "не проверено",
                        }
                    )
                cache.write_text(json.dumps(rows))
            except (OSError, ValueError):
                offline = True
        rows = json.loads(cache.read_text()) if cache.exists() and not offline else []
        state = self.state()
        evidence_file = self.root / "tested.json"
        evidence = json.loads(evidence_file.read_text()) if evidence_file.exists() else {}
        by_name = {r["package"]: r for r in rows}
        for name, version in state["requested"].items():
            by_name.setdefault(
                name,
                {
                    "package": name,
                    "chip": name.removeprefix("adafruit-circuitpython-"),
                    "url": "https://pypi.org/project/" + name,
                    "compatibility": "не проверено",
                },
            )
        return {
            "drivers": [
                {
                    **row,
                    "version": state["versions"].get(name),
                    "requested": name in state["requested"],
                    "compatibility": {"bench": "на стенде", "hardware": "проверено на Napi-C"}.get(
                        evidence.get(name + "==" + state["versions"].get(name, ""), {}).get("mode"),
                        "не проверено",
                    ),
                }
                for name, row in sorted(by_name.items())
            ],
            "offline": offline,
            "job": self.job,
        }

    def start(self, package, version=None, remove=False, confirmed=False):
        if not re.fullmatch(r"adafruit-circuitpython-[a-z0-9]+(?:-[a-z0-9]+)*", package):
            raise ValueError("Только имена adafruit-circuitpython-* из PyPI")
        if version is not None and not re.fullmatch(r"[0-9][a-zA-Z0-9.+-]{0,63}", version):
            raise ValueError("Неверная версия")
        with self.lock:
            if self.job and self.job["state"] == "running":
                raise ValueError("Другая установка ещё выполняется")
            uses = self.uses(package)
            current = self.state()["versions"].get(package)
            if uses and (remove or current is not None) and not confirmed:
                return {"confirmation_required": True, "uses": uses}
            self.job = {
                "id": uuid.uuid4().hex,
                "state": "running",
                "log": "",
                "package": package,
                "uses": uses,
            }
            threading.Thread(
                target=self._install, args=(package, version, remove), daemon=True
            ).start()
            return {"job": self.job}

    def example(self, package):
        if not re.fullmatch(r"adafruit-circuitpython-[a-z0-9]+(?:-[a-z0-9]+)*", package):
            raise ValueError("Неверное имя драйвера")
        version = self.state()["versions"].get(package)
        if version is None:
            raise ValueError("Сначала установите драйвер")
        cache = self.root / "examples"
        cache.mkdir(exist_ok=True)
        key = hashlib.sha256((package + "==" + version).encode()).hexdigest()
        file = cache / (key + ".json")
        if file.exists():
            return json.loads(file.read_text())
        row = next((r for r in self.catalog()["drivers"] if r["package"] == package), None)
        url = row["url"] if row else ""
        if not re.fullmatch(
            r"https://github.com/adafruit/Adafruit_CircuitPython_[A-Za-z0-9_]+", url
        ):
            raise ValueError("Нет репозитория примеров в Bundle; откройте документацию драйвера")
        repo = url.rsplit("/", 1)[-1]
        entries = json.loads(
            fetch(f"https://api.github.com/repos/adafruit/{repo}/contents/examples")
        )
        candidates = sorted(
            (
                e
                for e in entries
                if e.get("type") == "file" and re.fullmatch(r"[A-Za-z0-9_]+\.py", e.get("name", ""))
            ),
            key=lambda e: (
                not e["name"].endswith("_simpletest.py"),
                "simple" not in e["name"],
                e["name"],
            ),
        )
        if not candidates:
            raise ValueError("В репозитории нет Python-примера")
        example = candidates[0]
        source = example.get("download_url", "")
        if (
            not source.startswith(f"https://raw.githubusercontent.com/adafruit/{repo}/")
            or example.get("size", 0) > 200000
        ):
            raise ValueError("Недопустимый источник или размер примера")
        code = fetch(source).decode()
        if len(code.encode()) > 200000:
            raise ValueError("Пример больше 200 КиБ")
        result = {
            "source": source,
            "version": version,
            "name": example["name"],
            "code": f"# Official Adafruit example: {source}\n# Review pins and power before running. board.SDA/SCL/I2C use Napi-C defaults.\n"
            + code,
        }
        file.write_text(json.dumps(result))
        return result

    def _install(self, package, version, remove):
        stage = self.root / (".site-" + uuid.uuid4().hex)
        try:
            requested = dict(self.state()["requested"])
            if remove:
                requested.pop(package, None)
            else:
                info = json.loads(
                    fetch(
                        "https://pypi.org/pypi/"
                        + package
                        + ("/" + version if version else "")
                        + "/json"
                    )
                )
                version = info["info"]["version"]
                requested[package] = version
            stage.mkdir()
            wheels = self.root / "stubs"
            stub_wheels(wheels)
            constraints = self.root / "constraints.txt"
            previous = self.state()["versions"]
            pinned = {name: v for name, v in previous.items() if name != package}
            pinned.update(
                {"adafruit-blinka": STUB_VERSION, "adafruit-platformdetect": STUB_VERSION}
            )
            constraints.write_text("".join(f"{name}=={v}\n" for name, v in pinned.items()))
            cmd = [
                sys.executable,
                "-m",
                "pip",
                "--isolated",
                "install",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "--only-binary=:all:",
                "--target",
                str(stage),
                "--constraint",
                str(constraints),
                "--find-links",
                str(wheels),
                f"Adafruit-Blinka=={STUB_VERSION}",
                f"Adafruit-PlatformDetect=={STUB_VERSION}",
                *[f"{name}=={v}" for name, v in requested.items()],
            ]
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            timer = threading.Timer(240, process.kill)
            timer.start()
            try:
                for chunk in iter(lambda: process.stdout.read1(2048), b""):
                    self.job["log"] = (self.job["log"] + chunk.decode(errors="replace"))[-16000:]
                returncode = process.wait()
            finally:
                timer.cancel()
                process.stdout.close()
                if process.poll() is None:
                    process.kill()
                    process.wait()
            if returncode:
                raise ValueError("pip завершился с ошибкой; предыдущие драйверы сохранены")
            versions = validate_site(stage)
            (stage / ".pioneer-adafruit.json").write_text(
                json.dumps({"requested": requested, "versions": versions}, indent=2)
            )
            link = self.root / (".link-" + uuid.uuid4().hex)
            link.symlink_to(stage.name, target_is_directory=True)
            os.replace(link, self.site)
            self.job["state"] = "done"
        except Exception as e:
            shutil.rmtree(stage, ignore_errors=True)
            self.job.update(state="error", error=str(e))

    def bind(self, root, package):
        from pioneer.codetree import CodeTree

        root = Path(root)
        versions = self.state()["versions"]
        packages = [package] if isinstance(package, str) else package
        if any(p not in versions for p in packages):
            raise ValueError("Сначала установите драйвер")
        with CodeTree(root).lock:
            lock = root / "adafruit.lock.json"
            value = json.loads(lock.read_text()) if lock.exists() else {"drivers": {}}
            value["drivers"].update({p: versions[p] for p in packages})
            temp = lock.with_suffix(".tmp")
            temp.write_text(json.dumps(value, indent=2))
            os.replace(temp, lock)
        return value
