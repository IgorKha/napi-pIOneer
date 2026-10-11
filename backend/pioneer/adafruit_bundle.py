"""Export an editable driver block and its protocol model as a Pioneer module."""

import io
import json
import re
import zipfile
import yaml

from pioneer.adafruit_templates import PROFILES, block_template, package
from pioneer.hal.models import MODEL_KINDS


def export(manager, chip, interface, address, ident, version, code=None):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", ident) or not 1 <= version <= 9999:
        raise ValueError("ID модуля: a-z/0-9/_ до 40 знаков; версия 1…9999")
    if chip not in ("bme280", "sht4x", "pca9685"):
        raise ValueError("Модуль с блоком и моделью: BME280, SHT4x, PCA9685")
    from pioneer.board import load_board
    from pioneer.config import REPO_ROOT

    bus = load_board(REPO_ROOT / "boards/napi-c.yaml").interfaces[interface]
    if bus.kind != "i2c" or not 8 <= address <= 0x77:
        raise ValueError("Требуется I2C, адрес 0x08…0x77")
    profile = PROFILES[chip]
    model = MODEL_KINDS[profile["model"]]
    driver = package(chip)
    installed = manager.state()["versions"]
    if driver not in installed:
        raise ValueError("Сначала установите драйвер")
    params = {
        **model.params,
        "bus": {"type": "int", "default": bus.number},
        "addr": {"type": "int", "default": address},
    }
    manifest = {
        "id": ident,
        "version": version,
        "title": profile["title"],
        "entry": "block.py",
        "connection": f"{interface}, {address:#04x}; логика 3.3 В",
        "model": {"entry": "simulation.py", "params": params, "actions": model.actions},
    }
    # Module worker has no hardware; only byte-level protocol simulation crosses JSON.
    simulation = f"""from pioneer.hal.models import MODEL_KINDS

class Simulation:
    def __init__(self, config):
        self.model = MODEL_KINDS[{profile["model"]!r}](self, "device", **config)
    def model_changed(self, model):
        pass
    def i2c_write(self, bus, addr, data):
        self.model.i2c_write(bus, addr, bytes.fromhex(data))
    def i2c_read(self, bus, addr, n):
        return self.model.i2c_read(bus, addr, n).hex()
    def state(self):
        return self.model.state()
    def action(self, name, payload):
        self.model.action(name, payload)
"""
    files = {
        "module.yaml": yaml.safe_dump(manifest, allow_unicode=True),
        "block.py": code if code is not None else block_template(chip, interface, address),
        "simulation.py": simulation,
        "adafruit.lock.json": json.dumps({"drivers": {driver: installed[driver]}}, indent=2),
        "README.md": f"# {profile['title']}\n\nДрайвер `{driver}=={installed[driver]}`. "
        f"Установите эту версию в Устройства → Adafruit перед импортом ZIP.\n\n"
        f"{interface}: {bus.pins}; адрес {address:#04x}. Логика 3.3 В, подтяжки к 5 В запрещены. "
        "Перед запуском проверьте питание по документации своего модуля.\n\n"
        "block.py можно редактировать. simulation.py использует встроенную протокольную модель "
        "Pioneer; это проверка обмена и значений, без эмуляции электрических неисправностей.\n",
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return output.getvalue()
