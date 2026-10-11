"""Core-side bounded CircuitPython RPC. Each handle owns its resources."""

import errno
import itertools
import threading
import re
from pathlib import Path

from pioneer.resources import ResourceConflict, i2c_id


def pin_pulls(hw):
    if hw.hal.name != "linux":
        return {p.id: p.default_pull for p in hw.board.pins.values()}
    try:
        text = Path("/sys/kernel/debug/pinctrl/pinctrl-rockchip-pinctrl/pinconf-pins").read_text()
    except OSError:
        return {}
    result = {}
    for line in text.splitlines():
        gpio = re.search(r"\(gpio(\d+)-(\d+)\)", line)
        pull = re.search(r"pull[-_ ]?(up|down)|bias[-_ ]?disable", line)
        if gpio and pull:
            for p in hw.board.pins.values():
                if p.gpio and (p.gpio.chip, p.gpio.line) == (int(gpio[1]), int(gpio[2])):
                    result[p.id] = pull[1] or "none"
    return result


class CircuitPythonHW:
    def __init__(self, hw, owner, name):
        self.hw, self.owner, self.name = hw, owner, name
        self.handles = {}
        self.ids = itertools.count(1)
        self.lock = threading.RLock()
        self.closed = False

    def close(self):
        with self.lock:
            self.closed = True
            for h in list(self.handles):
                self._release(h)

    def _release(self, h):
        item = self.handles.pop(h)
        if item["kind"] in ("gpio_in", "gpio_out") and self.hw.hal.name == "bench":
            self.hw.hal.set_pull(item["owner"], item["pin"], None)
        self.hw.release(item["owner"])

    def call(self, action, **a):
        with self.lock:
            if self.closed:
                raise RuntimeError("CircuitPython worker остановлен")
            try:
                return self._call(action, a)
            except OSError as e:
                return {"_circuitpython_errno": e.errno, "message": str(e)}

    def _call(self, action, a):
        hw = self.hw
        if action == "describe":
            return {
                "hal": hw.hal.name,
                "interfaces": [
                    {
                        "id": i.id,
                        "kind": i.kind,
                        "pins": i.pins,
                        "enabled": hw.board.interface_enabled(i.id),
                        "overlays": i.overlays,
                    }
                    for i in hw.board.interfaces.values()
                ],
                "pins": {
                    p.id: {"pull": p.default_pull, "adc": p.adc_channel}
                    for p in hw.board.pins.values()
                },
            }
        if action == "open":
            h = next(self.ids)
            item = {"owner": f"{self.owner}:cp:{h}", "kind": a["kind"], "devices": {}}
            owner, kind = item["owner"], item["kind"]
            if kind in ("i2c", "spi", "uart", "pwm"):
                iface = hw.bus(a["interface"], kind)
                item["interface"] = iface.id
                if kind == "spi":
                    item["device"] = hw.spi(
                        owner,
                        self.name,
                        iface.id,
                        0,
                        mode=a.get("mode", 0),
                        speed_hz=a.get("baudrate", 1000000),
                    )
                elif kind == "uart":
                    item["device"] = hw.uart(owner, self.name, iface.id, baud=a["baudrate"])
                elif kind == "pwm":
                    item["device"] = hw.hardware_pwm(owner, self.name, iface.id)
            elif kind in ("gpio_in", "gpio_out", "adc", "soft_pwm"):
                item["pin"] = a["pin"]
                hw.acquire(
                    owner, self.name, a["pin"], kind, initial=a.get("value", False), safe=False
                )
            else:
                raise ValueError("неверный режим CircuitPython")
            self.handles[h] = item
            return h
        h = int(a["h"])
        if action == "close":
            self._release(h)
            return None
        item = self.handles[h]
        kind, owner = item["kind"], item["owner"]
        if action == "mode" and kind in ("gpio_in", "gpio_out"):
            mode = a["kind"]
            if mode not in ("gpio_in", "gpio_out"):
                raise ValueError("GPIO mode: gpio_in/gpio_out")
            hw.acquire(
                owner, self.name, item["pin"], mode, initial=bool(a.get("value")), safe=False
            )
            item["kind"] = mode
            if mode == "gpio_out":
                if hw.hal.name == "bench":
                    hw.hal.set_pull(owner, item["pin"], None)
                hw.write(item["pin"], bool(a.get("value")))
            return None
        if action == "scan" and kind == "i2c":
            found = []
            scan_owner = owner + ":scan"
            iface = hw.bus(item["interface"], "i2c")
            for addr in range(0x08, 0x78):
                try:
                    if addr in item["devices"]:
                        dev = item["devices"][addr]
                    else:
                        dev = hw.i2c(scan_owner, self.name, iface.id, addr)
                    dev.read(1)
                    found.append(addr)
                except ResourceConflict:
                    continue
                except OSError as e:
                    if e.errno not in (errno.ENXIO, getattr(errno, "EREMOTEIO", 121), errno.EIO):
                        raise
                finally:
                    hw.release(scan_owner, i2c_id(iface.controller, addr))
            return found
        data = bytes.fromhex(a.get("data", ""))
        n = int(a.get("n", 0))
        if len(data) > 4096 or not 0 <= n <= 4096:
            raise ValueError("операция CircuitPython до 4096 байт")
        if action == "i2c" and kind == "i2c":
            addr = int(a["address"])
            if not 0x08 <= addr <= 0x77:
                raise ValueError("I2C address: 0x08…0x77")
            dev = item["devices"].get(addr)
            if dev is None:
                dev = hw.i2c(owner, self.name, item["interface"], addr)
                item["devices"][addr] = dev
            if a["operation"] == "write":
                dev.write(data)
                return None
            if a["operation"] == "read":
                return dev.read(n).hex()
            if a["operation"] == "write_read":
                return dev.write_read(data, n).hex()
        if action == "spi" and kind == "spi":
            return item["device"].xfer(data).hex()
        if action == "configure" and kind in ("spi", "uart"):
            hw.release(owner)
            try:
                if kind == "spi":
                    item["device"] = hw.spi(
                        owner,
                        self.name,
                        item["interface"],
                        0,
                        mode=a["mode"],
                        speed_hz=a["baudrate"],
                    )
                else:
                    item["device"] = hw.uart(
                        owner, self.name, item["interface"], baud=a["baudrate"]
                    )
            except Exception:
                self.handles.pop(h)
                raise
            return None
        if action == "uart" and kind == "uart":
            if a["operation"] == "write":
                item["device"].write(data)
                return len(data)
            timeout = float(a.get("timeout", 0))
            if not 0 <= timeout <= 5:
                raise ValueError("UART timeout: 0…5 с")
            return item["device"].read(n, timeout).hex()
        if action == "pull" and kind == "gpio_in":
            value = a["pull"]
            if value not in ("up", "down", "none"):
                raise ValueError("Pull: up/down/none")
            if hw.hal.name == "bench":
                hw.hal.set_pull(owner, item["pin"], value)
                return None
            actual = pin_pulls(hw).get(item["pin"])
            if actual != value:
                return f"{item['pin']}: Pull.{value.upper()} софтом не применяется; настройте DT-подтяжку в Устройства → Ядерные драйверы (план и перезагрузка)"
            return None
        if action == "read" and kind in ("gpio_in", "gpio_out", "adc"):
            return hw.value(item["pin"])
        if action == "write" and kind == "gpio_out":
            hw.write(item["pin"], bool(a["value"]))
            return None
        if action == "pwm" and kind in ("pwm", "soft_pwm"):
            if kind == "pwm":
                item["device"].set(a["frequency"], a["duty"])
            else:
                hw.set_pwm(item["pin"], freq=a["frequency"], duty=a["duty"])
            return None
        raise ValueError(f"CircuitPython: {action} недоступно для {kind}")
