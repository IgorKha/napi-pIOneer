"""Static CircuitPython checks; never imports the user's drivers."""

import ast
from pathlib import Path


def diagnose(code, root, board, versions):
    from pioneer.compat.circuitpython import site

    tree = ast.parse(code)
    pins = {
        alias: p
        for p in board.get("pins", [])
        for alias in (p["id"].replace("-", "_"), p.get("name"))
        if alias
    }
    defaults = {
        "SDA": "J9_3",
        "SCL": "J9_5",
        "SCK": "J9_23",
        "MOSI": "J9_19",
        "MISO": "J9_21",
        "TX": "J9_19",
        "RX": "J9_21",
    }
    for alias, name in defaults.items():
        if name in pins:
            pins[alias] = pins[name]
    known = set(pins) | {"I2C", "SPI", "UART", "board_id", "FONT5X8"}
    items = []

    def add(node, message, fix=None):
        items.append(
            {"line": node.lineno, "column": node.col_offset, "message": message, "fix": fix}
        )

    aliases = {
        n.asname or n.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for n in node.names
        if n.name == "board"
    }
    pin_vars = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            call = node.value
            name = (
                call.func.attr
                if isinstance(call.func, ast.Attribute)
                else getattr(call.func, "id", "")
            )
            if name == "DigitalInOut" and call.args:
                pin = call.args[0]
                if (
                    isinstance(pin, ast.Attribute)
                    and isinstance(pin.value, ast.Name)
                    and pin.value.id in aliases
                    and pin.attr in pins
                ):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            pin_vars[target.id] = pins[pin.attr]

    def check_pull(node, variable, pull):
        if board.get("hal") == "bench":
            return
        pin = pin_vars.get(variable)
        if pin and isinstance(pull, ast.Attribute) and pull.attr in ("UP", "DOWN"):
            desired = pull.attr.lower()
            if pin.get("actual_pull") != desired:
                add(
                    node,
                    f"{pin['id']}: Pull.{pull.attr} требует DT-подтяжки и перезагрузки; скрипт продолжит работу",
                    f"pull:{pin['id']}:{desired}",
                )

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "pull"
                    and isinstance(target.value, ast.Name)
                ):
                    check_pull(node, target.value.id, node.value)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "switch_to_input"
            and isinstance(node.func.value, ast.Name)
        ):
            pull = next(
                (k.value for k in node.keywords if k.arg == "pull"),
                node.args[0] if node.args else None,
            )
            check_pull(node, node.func.value.id, pull)
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
        ):
            if node.attr not in known:
                add(node, f"Нет пина/API board.{node.attr} на Napi-C", "board")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [a.name.split(".")[0] for a in node.names]
                if isinstance(node, ast.Import)
                else [(node.module or "").split(".")[0]]
            )
            for name in names:
                if not name.startswith("adafruit_"):
                    continue
                if not (site(root) / (name + ".py")).exists() and not (site(root) / name).is_dir():
                    chip = name.removeprefix("adafruit_")
                    chip = {"character_lcd": "charlcd", "bus_device": "busdevice"}.get(chip, chip)
                    add(
                        node,
                        f"Не установлен драйвер {name}",
                        "install:adafruit-circuitpython-" + chip.replace("_", "-"),
                    )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("DigitalInOut", "AnalogIn", "PWMOut") and node.args:
                pin = node.args[0]
                if (
                    isinstance(pin, ast.Attribute)
                    and isinstance(pin.value, ast.Name)
                    and pin.value.id in aliases
                    and pin.attr in pins
                ):
                    p = pins[pin.attr]
                    if p.get("function") not in ("gpio", "adc"):
                        add(
                            node, f"{p['id']} занят интерфейсом/ядром: {p.get('function')}", "board"
                        )
                    elif p.get("claim"):
                        add(
                            node,
                            f"{p['id']} уже захвачен: " + ", ".join(p["claim"]["owners"].values()),
                            "board",
                        )
            if node.func.attr == "I2C" and len(node.args) >= 2:
                ids = []
                for arg in node.args[:2]:
                    ids.append(
                        pins[arg.attr]["id"]
                        if isinstance(arg, ast.Attribute)
                        and isinstance(arg.value, ast.Name)
                        and arg.value.id in aliases
                        and arg.attr in pins
                        else None
                    )
                if all(ids):
                    candidates = [
                        i
                        for i in board.get("interfaces", [])
                        if i["kind"] == "i2c"
                        and i["pins"].get(ids[0]) == "SCL"
                        and i["pins"].get(ids[1]) == "SDA"
                    ]
                    if not candidates or not any(i["enabled"] for i in candidates):
                        add(
                            node,
                            "I2C с этими пинами выключен или не существует; нужен план интерфейсов и перезагрузка",
                            "board",
                        )
    lock = Path(root) / "adafruit.lock.json"
    if lock.exists():
        import json

        for package, version in json.loads(lock.read_text()).get("drivers", {}).items():
            if versions.get(package) != version:
                items.append(
                    {
                        "line": 1,
                        "column": 0,
                        "message": f"{package}: lock={version}, установлен={versions.get(package, 'нет')}; обновите подключение через мастер",
                        "fix": "board",
                    }
                )
    return items
