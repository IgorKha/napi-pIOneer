"""Run on Napi-C, using a protected temporary API session file; never print its token.

Installs PCF8574/CharLCD through Pioneer, creates and runs the LCD2004 example,
then reads its actual DDRAM without reinitializing or writing replacement text.
Only run with this LCD connected at I2C1/0x27 and no other owner of that address.
"""

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, "/mount/rfs/rw/.pioneer/app/backend")
from pioneer.adafruit import Drivers, validate_site
from smbus2 import SMBus, i2c_msg

DATA = Path("/mount/rfs/rw/.pioneer/data")
SESSION = Path("/tmp/pioneer-adafruit-session")
token = SESSION.read_text().strip()


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


for name in ("pcf8574", "charlcd"):
    result = api("/adafruit/change", {"package": "adafruit-circuitpython-" + name})
    if result.get("confirmation_required"):
        raise RuntimeError(
            "Existing driver is used; explicitly confirm its update in Devices"
        )
    deadline = time.monotonic() + 260
    while True:
        job = api("/adafruit/job")
        if job["state"] != "running":
            break
        if time.monotonic() > deadline:
            raise TimeoutError("Driver installation deadline")
        time.sleep(1)
    print(name, job["state"], flush=True)
    if job["state"] != "done":
        print(job["log"])
        raise RuntimeError(job.get("error"))

body = {"chip": "lcd", "interface": "i2c1", "address": 0x27, "columns": 20, "rows": 4}
code = api("/adafruit/connect", body)["code"]
script = api("/scripts", {"name": "Adafruit LCD2004 — пример", "code": code})
api("/adafruit/connect", {**body, "kind": "scripts", "id": script["id"], "bind": True})
api("/scripts/" + script["id"] + "/start", {})
deadline = time.monotonic() + 30
while True:
    result = api("/scripts/" + script["id"])
    if result["status"]["state"] in ("exited", "error"):
        break
    if time.monotonic() > deadline:
        raise TimeoutError("LCD script deadline")
    time.sleep(0.25)
print("Script", script["id"], result["status"], flush=True)
if result["status"]["state"] != "exited":
    print(json.dumps(result["log"], ensure_ascii=False))
    raise RuntimeError("LCD script failed")
assert "i2c1@0x27" not in api("/board")["bus_claims"], "address still owned"

with SMBus(1) as bus:

    def port(value):
        bus.i2c_rdwr(i2c_msg.write(0x27, [value]))

    def nibble(value):
        port((value & 0xF0) | 8)
        port((value & 0xF0) | 12)
        time.sleep(0.001)
        port((value & 0xF0) | 8)
        time.sleep(0.001)

    def cursor(offset):
        value = 0x80 | offset
        nibble(value)
        nibble(value << 4)
        time.sleep(0.005)

    def read():
        parts = []
        for _ in range(2):
            port(0xFB)
            port(0xFF)
            time.sleep(0.001)
            message = i2c_msg.read(0x27, 1)
            bus.i2c_rdwr(message)
            parts.append(list(message)[0] & 0xF0)
            port(0xFB)
            time.sleep(0.001)
        port(8)
        return parts[0] | (parts[1] >> 4)

    try:
        for offset, expected in zip(
            (0, 0x40, 0x14, 0x54),
            ("Napi-C Pioneer", "Adafruit PCF8574", "CircuitPython", "20x4 RPC OK"),
        ):
            cursor(offset)
            data = bytes(read() for _ in range(20))
            print(f"DDRAM {offset:#04x}: {data!r}", flush=True)
            assert data == expected.ljust(20).encode(), (data, expected)
        cursor(0)
    finally:
        port(8)
drivers = Drivers(DATA)
versions = validate_site(drivers.site)
print("Versions:", json.dumps(versions, sort_keys=True))
drivers.record_test(
    ["adafruit-circuitpython-pcf8574", "adafruit-circuitpython-charlcd"],
    "hardware",
    "LCD2004 3.3V I2C1/0x27; API worker run and exact 80-byte DDRAM readback",
)
print(
    "PASS: actual Adafruit wheels → worker → CircuitPython RPC → HW → LCD2004; all 4 rows verified"
)
print(
    "Example script retained for the user; backlight on. Visual layout still needs user confirmation."
)
