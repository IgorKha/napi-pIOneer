"""Known kernel drivers: compatible .ko, reviewed boot overlay, IIO readings.

No arbitrary module name, shell command, DTS or sysfs path comes from the browser.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import platform
import re
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from pioneer.board import load_board, parse_uenv_overlays
from pioneer.uenv import UEnvError
from pioneer.codetree import CodeTree
from pioneer.overlays import OverlayError

log = logging.getLogger(__name__)
OVERLAY = "pioneer-dht22"
MODULE_LIMIT = 1024 * 1024


class DriverError(OverlayError):
    pass


class KernelDrivers:
    def __init__(self, cfg, board, hw, overlays):
        self.cfg, self.board, self.hw, self.overlays = cfg, board, hw, overlays
        self.release = platform.release()
        self.root = cfg.data_dir / "kernel-drivers"
        self.root.mkdir(exist_ok=True)
        self.file = self.root / "dht22.json"
        self.module = self.root / self.release / "dht11.ko"
        self.lock = threading.RLock()
        self.sample = None
        self.sample_at = 0.0
        self.sample_lock = threading.Lock()
        self.packages = None

    def command(self, args):
        try:
            return subprocess.run(args, check=True, text=True, capture_output=True, timeout=15).stdout.strip()
        except (OSError, subprocess.SubprocessError) as e:
            # Never echo uploaded content or uncontrolled command output.
            raise DriverError("не удалось выполнить " + Path(args[0]).name + ": проверьте журнал ядра") from e

    def real(self):
        if self.hw.hal.name != "linux":
            raise DriverError("драйверы ядра подключаются только на реальной плате")

    def expected_vermagic(self):
        base = self.cfg.modules_dir / self.release
        for p in sorted(base.rglob("*.ko*")):
            if p.name.endswith((".ko", ".ko.xz", ".ko.gz", ".ko.zst")):
                value = self.command(["modinfo", "-F", "vermagic", str(p)])
                if value and value.split()[0] == self.release:
                    return value
        raise DriverError("нет эталонного vermagic текущего ядра; нужен совместимый kernel build")

    def validate_module(self, path):
        data = path.read_bytes()
        if (len(data) < 64 or len(data) > MODULE_LIMIT or data[:6] != b"\x7fELF\x02\x01"
                or struct.unpack_from("<HH", data, 16) != (1, 183)):
            raise DriverError("нужен несжатый ELF-модуль ARM64 (.ko) до 1 МиБ")
        if self.command(["modinfo", "-F", "name", str(path)]) != "dht11":
            raise DriverError("в этом разделе разрешён только модуль dht11 для DHT11/DHT22")
        if self.command(["modinfo", "-F", "vermagic", str(path)]) != self.expected_vermagic():
            raise DriverError("модуль собран для другого ядра/ABI — пересоберите для " + self.release)
        return hashlib.sha256(data).hexdigest()

    def system_module(self):
        if self.hw.hal.name != "linux":
            return False
        try:
            self.command(["modinfo", "dht11"])
            return True
        except DriverError:
            return False

    def installed(self):
        return self.module.is_file() or bool(self.packages and self.packages.package_for_module("dht11")) or self.system_module()

    def config(self):
        try:
            return json.loads(self.file.read_text())
        except FileNotFoundError:
            return {"pin": "", "enabled": False}

    def iio_device(self):
        for p in sorted(self.cfg.iio_dir.glob("iio:device*")):
            try:
                if (p / "name").read_text().strip() in ("dht11", OVERLAY) and (p / "of_node").resolve().name == OVERLAY:
                    return p
            except OSError:
                continue
        return None

    def status(self):
        real = self.hw.hal.name == "linux"
        module = self.installed()
        device = self.iio_device() if real else None
        return {"id": "dht22", "title": "DHT22 / AM2302", "module": "dht11", "kernel": self.release,
                "real": real, "installed": module, "uploaded": self.module.is_file(),
                "loaded": real and (self.cfg.kernel_sys_dir / "module" / "dht11").is_dir(),
                "active": device is not None, "device": device.name if device else None,
                "package": self.packages.package_for_module("dht11") if self.packages else None,
                "config": self.config(), "pins": [p.id for p in self.board.pins.values() if p.gpio],
                "sample": self.sample, "requires_reboot": True,
                "reason": "" if module else "CONFIG_DHT11 выключен или модуль отсутствует. Загрузите dht11.ko из сборки текущего ядра."}

    def install(self, data):
        self.real()
        with self.lock, tempfile.TemporaryDirectory(dir=self.root) as tmp:
            p = Path(tmp) / "dht11.ko"
            p.write_bytes(data)
            os.chmod(p, 0o600)
            digest = self.validate_module(p)
            if (self.cfg.kernel_sys_dir / "module" / "dht11").exists():
                raise DriverError("драйвер уже загружен; заменять его можно после отключения и перезагрузки", 409)
            CodeTree._write(self.module, data)
            os.chmod(self.module, 0o600)
            log.info("DHT11 module installed kernel=%s sha256=%s", self.release, digest)
            return {"installed": True, "sha256": digest, "loaded": False}

    def load(self):
        self.real()
        with self.lock:
            if (self.cfg.kernel_sys_dir / "module" / "dht11").exists():
                return {"loaded": True, "changed": False}
            if self.packages and (ident := self.packages.package_for_module("dht11")):
                return self.packages.load(ident)
            if self.module.exists():
                self.validate_module(self.module)
                self.command(["insmod", str(self.module)])
            elif self.system_module():
                self.command(["modprobe", "dht11"])
            else:
                raise DriverError("сначала установите совместимый dht11.ko")
            if not (self.cfg.kernel_sys_dir / "module" / "dht11").is_dir():
                raise DriverError("драйвер не появился в /sys/module/dht11")
            log.info("DHT11 module loaded kernel=%s", self.release)
            return {"loaded": True, "changed": True}

    def autoload(self):
        # Only configurations explicitly applied by the user enable persistent loading.
        if self.hw.hal.name != "linux":
            return
        # Universal packages own their own opt-in autoload and module parameters.
        if self.packages and self.packages.package_for_module("dht11"):
            return
        try:
            if self.config().get("enabled"):
                self.load()
        except (DriverError, ValueError, OSError):
            log.exception("DHT11 autoload failed; application continues")

    def source(self, pin):
        p = self.board.pins.get(pin)
        if p is None or p.gpio is None:
            raise DriverError("выберите GPIO-пин гребёнки J9/J10")
        bank, line = p.gpio.chip, p.gpio.line
        return f'''/dts-v1/;
/plugin/;
/ {{
    fragment@0 {{ target-path = "/"; __overlay__ {{
        {OVERLAY} {{ compatible = "dht11"; status = "okay";
            gpios = <&gpio{bank} {line} 0>;
            pinctrl-names = "default"; pinctrl-0 = <&pioneer_dht22_pin>;
        }};
    }}; }};
    fragment@1 {{ target = <&pinctrl>; __overlay__ {{
        pioneer-dht22 {{ pioneer_dht22_pin: data-pin {{ rockchip,pins = <{bank} {line} 0 &pcfg_pull_up>; }}; }};
    }}; }};
}};
'''

    def boot_text(self):
        p = self.cfg.uenv_file
        if p.is_symlink() or not p.is_file() or p.stat().st_size > 65536:
            raise DriverError("uEnv должен быть обычным файлом до 64 КБ")
        text = p.read_text()
        if len(re.findall(r"^user_overlays=", text, re.M)) > 1:
            raise DriverError("в uEnv несколько строк user_overlays=")
        return text

    def compile(self, source, text, overlay_name=OVERLAY):
        # Validate the COMPLETE next-boot overlay chain against the actual base DTB.
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            dts, dtbo = Path(tmp) / "sensor.dts", Path(tmp) / "sensor.dtbo"
            dts.write_text(source)
            self.command(["dtc", "-@", "-I", "dts", "-O", "dtb", "-o", str(dtbo), str(dts)])
            values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#"))
            fdtfile = values.get("fdtfile", "")
            if not re.fullmatch(r"[A-Za-z0-9_.-]+\.dtb", fdtfile):
                raise DriverError("неверный fdtfile в uEnv")
            chain = []
            for key, base in (("overlays", self.cfg.overlays_dir), ("user_overlays", self.cfg.user_overlays_dir)):
                for name in values.get(key, "").split():
                    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
                        raise DriverError("неверное имя оверлея в uEnv")
                    chain.append(dtbo if name == overlay_name and key == "user_overlays" else base / (name + ".dtbo"))
            self.command(["fdtoverlay", "-i", str(self.cfg.uenv_file.parent / fdtfile), "-o", str(Path(tmp) / "validated.dtb"), *map(str, chain)])
            return dtbo.read_bytes()

    def _plan(self, pin, enabled):
        source = self.source(pin) if enabled else ""
        text = self.boot_text()
        m = re.search(r"^user_overlays=(.*)$", text, re.M)
        names = m.group(1).split() if m else []
        names = [n for n in names if n != OVERLAY]
        if enabled:
            names.append(OVERLAY)
        line = "user_overlays=" + " ".join(names)
        updated = re.sub(r"^user_overlays=.*$", line, text, flags=re.M) if m else text + ("" if text.endswith("\n") else "\n") + line + "\n"
        blockers = []
        if self.hw.hal.name != "linux":
            blockers.append("на стенде загрузка драйверов и изменение boot запрещены")
        if enabled:
            pending = load_board(self.cfg.board_file, parse_uenv_overlays(text))
            if pending.function(pin) != "gpio":
                blockers.append("после загрузки пин будет занят интерфейсом " + str(pending.function(pin)))
            if not self.installed():
                blockers.append("нужен совместимый модуль dht11.ko")
            if self.board.function(pin) != "gpio" and any(u.node != "/" + OVERLAY for u in self.board._users(pin)):
                blockers.append("пин занят интерфейсом или другим драйвером ядра")
            if self.board.function(pin) != "gpio" and not self.board.dt:
                blockers.append("пин занят интерфейсом")
            if pin in self.hw.res.claims:
                blockers.append("пин используется: " + ", ".join(self.hw.res.claims[pin].owners.values()))
        compiled = False
        blob = b""
        if enabled and self.hw.hal.name == "linux":
            blob = self.compile(source, updated)
            compiled = True
        digest = hashlib.sha256((text + source + json.dumps(self.config(), sort_keys=True)).encode()).hexdigest()
        return {"pin": pin, "enabled": enabled, "hash": digest, "uenv_hash": hashlib.sha256(text.encode()).hexdigest(), "source": source,
                "diff": "".join(difflib.unified_diff(text.splitlines(True), updated.splitlines(True), fromfile="uEnv.txt", tofile="после применения")),
                "blockers": blockers, "ready": not blockers, "compiled": compiled, "needs_reboot": True}, updated, blob

    def preview(self, pin, enabled):
        with self.overlays.lock, self.lock:
            return self._plan(pin, enabled)[0]

    def apply(self, pin, enabled, expected_hash):
        self.real()
        with self.overlays.lock, self.lock:
            plan, text, blob = self._plan(pin, enabled)
            if not expected_hash or plan["hash"] != expected_hash:
                raise DriverError("настройки изменились — обновите предпросмотр", 409)
            if plan["blockers"]:
                raise DriverError("; ".join(plan["blockers"]), 409)
            # Kernel compilation runs before these locks; never stall live GPIO timers.
            with self.hw._lock, self.hw.res._lock:
                if enabled and pin in self.hw.res.claims:
                    raise DriverError("пин занят — остановите владельца", 409)
                source = self.source(pin) if enabled else ""
                current = hashlib.sha256((self.boot_text() + source + json.dumps(self.config(), sort_keys=True)).encode()).hexdigest()
                if current != expected_hash:
                    raise DriverError("uEnv изменился во время проверки — обновите предпросмотр", 409)
                return self._commit(pin, enabled, text, blob, plan["uenv_hash"])

    def _commit(self, pin, enabled, text, blob, uenv_hash):
        dest = self.cfg.user_overlays_dir / (OVERLAY + ".dtbo")
        files = {self.file: json.dumps({"pin": pin, "enabled": enabled}).encode()}
        if enabled:
            files[dest] = blob
        try:
            return self.overlays.uenv.commit(uenv_hash, text, files)
        except UEnvError as e:
            raise DriverError(str(e), 409) from e

    def read(self):
        self.real()
        with self.sample_lock:
            if self.sample and time.monotonic() - self.sample_at < 2:
                return self.sample
            device = self.iio_device()
            if device is None:
                raise DriverError("DHT22 не зарегистрирован в IIO: проверьте модуль, оверлей и перезагрузку", 409)
            try:
                temperature = int((device / "in_temp_input").read_text()) / 1000
                humidity = int((device / "in_humidityrelative_input").read_text()) / 1000
                if not (-40 <= temperature <= 80 and 0 <= humidity <= 100):
                    raise ValueError("значения вне диапазона DHT22")
                self.sample = {"ok": True, "temperature": temperature, "humidity": humidity, "time": time.time()}
            except (OSError, ValueError):
                self.sample = {"ok": False, "error": "Нет корректного ответа DHT22: проверьте 3.3 В, DATA, GND и подтяжку. См. журнал ядра.", "time": time.time()}
            self.sample_at = time.monotonic()
            return self.sample
