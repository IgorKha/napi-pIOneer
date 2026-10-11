import json
import os
import selectors
import subprocess
import sys
import threading
import time
from pioneer.config import REPO_ROOT
from pioneer.hal.models import Model


class ModuleModel(Model):
    root = None
    simulation = None
    widget = None

    def __init__(self, bench, model_id, **config):
        super().__init__(bench, model_id, **config)
        self.lock = threading.Lock()
        self.process = subprocess.Popen(
            [sys.executable, "-m", "pioneer.model_worker", str(self.root), self.simulation],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "backend"), "MALLOC_ARENA_MAX": "1"},
        )
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        os.set_blocking(self.process.stdout.fileno(), False)
        try:
            self.call("init", config)
        except BaseException:
            self.detach()
            raise
        self.watch_pins = {
            v for k, v in config.items() if k in ("pin", "a", "b") and isinstance(v, str)
        }

    def call(self, method, *args):
        with self.lock:
            if self.process.poll() is not None:
                raise ValueError("симулятор модуля остановлен")
            raw = (json.dumps({"method": method, "args": args}) + "\n").encode()
            if len(raw) > 65536:
                raise ValueError("запрос симулятора больше 64 КиБ")
            self.process.stdin.write(raw)
            self.process.stdin.flush()
            timeout = 3 if method == "init" else 0.5
            deadline = time.monotonic() + timeout
            line = bytearray()
            while not line.endswith(b"\n"):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self.selector.select(remaining):
                    self.process.kill()
                    raise ValueError("симулятор превысил время ответа")
                chunk = os.read(self.process.stdout.fileno(), 65537 - len(line))
                if not chunk:
                    raise ValueError("симулятор закрыл канал ответа")
                line.extend(chunk)
                if len(line) > 65536:
                    self.process.kill()
                    raise ValueError("неверный ответ симулятора")
            result = json.loads(line)
            if not result.get("ok"):
                raise ValueError("ошибка симулятора: " + result.get("error", "unknown"))
            return result.get("result")

    def attach(self):
        if "addr" in self.config:
            self.bench.attach_i2c(self, int(self.config.get("bus", 1)), int(self.config["addr"]))
        if "cs" in self.config:
            self.bench.attach_spi(self, int(self.config.get("bus", 1)), int(self.config["cs"]))

    def detach(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=1)
        self.selector.close()
        for pipe in (self.process.stdin, self.process.stdout):
            pipe.close()

    def on_gpio(self, pin, level, source):
        self.call("on_gpio", pin, level, source)

    def on_pwm(self, pin, pwm):
        self.call("on_pwm", pin, pwm)

    def i2c_write(self, bus, addr, data):
        self.call("i2c_write", bus, addr, data.hex())

    def i2c_read(self, bus, addr, n):
        return bytes.fromhex(self.call("i2c_read", bus, addr, n))

    def spi_xfer(self, bus, cs, data):
        return bytes.fromhex(self.call("spi_xfer", bus, cs, data.hex()))

    def state(self):
        try:
            return self.call("state")
        except (ValueError, OSError):
            return {
                "available": False,
                "fault": "симулятор остановлен; удалите и добавьте его заново",
            }

    def action(self, name, payload):
        if name not in self.actions:
            raise ValueError("неизвестное действие симулятора")
        self.call("action", name, payload)

    def describe(self):
        return {**super().describe(), "widget": self.widget}
