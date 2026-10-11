"""Napi-C: install real wheels through core API, then test isolated protocol models.

Uses /tmp/pioneer-adafruit-session (0600); never touches the Linux HAL or physical pins.
"""

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, "/mount/rfs/rw/.pioneer/app/backend")
from pioneer.adafruit import Drivers, validate_site
from pioneer.board import load_board
from pioneer.config import Config
from pioneer.compat.circuitpython import SHIMS, Context, activate
from pioneer.compat.hardware import CircuitPythonHW
from pioneer.hal import BenchHAL
from pioneer.hw import HW
from pioneer.resources import ResourceManager

DATA = Path("/mount/rfs/rw/.pioneer/data")
token = Path("/tmp/pioneer-adafruit-session").read_text().strip()


def api(path, body=None):
    request = urllib.request.Request(
        "http://127.0.0.1:8100/pioneer/api" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Cookie": "pioneer_session=" + token,
            "X-Pioneer": "1",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


packages = [
    "adafruit-circuitpython-" + chip
    for chip in ("bme280", "sht4x", "ssd1306", "pca9685")
]
for package in packages:
    if package in Drivers(DATA).state()["requested"]:
        print(package, "already installed", flush=True)
        continue
    r = api("/adafruit/change", {"package": package})
    assert not r.get("confirmation_required"), (
        "An existing driver is used; stop and review its update"
    )
    deadline = time.monotonic() + 260
    while True:
        job = api("/adafruit/job")
        if job["state"] != "running":
            break
        if time.monotonic() > deadline:
            raise TimeoutError(package)
        time.sleep(1)
    assert job["state"] == "done", job
    print(package, "installed", flush=True)

manager = Drivers(DATA)
validate_site(manager.site)
sys.path[:0] = [str(SHIMS), str(manager.site.resolve())]
board_def = load_board(Config().board_file, list(Config().default_overlays))
bench = BenchHAL(board_def)
hw = HW(board_def, bench, ResourceManager(board_def))
transport = CircuitPythonHW(hw, "bench:adafruit", "isolated board bench")
try:
    with activate(Context(lambda op, **args: transport.call(**args))):
        import board
        from adafruit_bme280 import basic
        import adafruit_sht4x, adafruit_ssd1306, adafruit_pca9685

        bme = bench.add("bme280")
        bench.add("sht4x")
        oled = bench.add("ssd1306")
        pwm = bench.add("pca9685")
        i2c = board.I2C()
        sensor = basic.Adafruit_BME280_I2C(i2c, address=0x76)
        assert abs(sensor.temperature - 25) < 0.01
        bench.action(
            bme.id, "set", {"temperature": -12, "humidity": 81, "pressure": 980}
        )
        assert abs(sensor.temperature + 12) < 0.01
        assert abs(sensor.humidity - 81) < 0.02 and abs(sensor.pressure - 980) < 0.01
        print(
            "BME280:", sensor.temperature, sensor.humidity, sensor.pressure, flush=True
        )
        sht = adafruit_sht4x.SHT4x(i2c)
        t, h = sht.measurements
        assert abs(t - 25) < 0.01 and abs(h - 50) < 0.01
        assert sht.serial_number == 0x12345678
        print("SHT4x:", t, h, flush=True)
        display = adafruit_ssd1306.SSD1306_I2C(128, 64, i2c)
        display.fill(0)
        display.text("Pioneer", 0, 0, 1, font_name=board.FONT5X8)
        display.show()
        assert oled.state()["lit_pixels"] > 20
        assert bytes(oled.buffer) == bytes(display.buffer[1:])
        print(
            "SSD1306:",
            oled.state()["lit_pixels"],
            "pixels, exact framebuffer",
            flush=True,
        )
        device = adafruit_pca9685.PCA9685(i2c)
        device.frequency = 50
        device.channels[3].duty_cycle = 32768
        assert (
            abs(pwm.state()["frequency"] - 50) < 1
            and pwm.state()["channels"][3] == 32768
        )
        print(
            "PCA9685:", pwm.state()["frequency"], pwm.state()["channels"][3], flush=True
        )
        i2c.deinit()
        assert not hw.res.claims
    manager.record_test(
        packages
        + ["adafruit-circuitpython-framebuf", "adafruit-circuitpython-busdevice"],
        "bench",
        "Napi-C isolated BenchHAL; unchanged real wheels, sensor values/CRC/framebuffer/PWM assertions",
    )
    print(
        "PASS: all four unchanged drivers on Napi-C protocol bench; no physical hardware accessed"
    )
finally:
    transport.close()
    hw.close_all()
    bench.close()
