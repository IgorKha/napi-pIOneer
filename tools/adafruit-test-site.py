"""Prepare isolated real PyPI wheels for tests/test_circuitpython.py (no system install)."""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from pioneer.adafruit import Drivers, validate_site

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("data", type=Path, help="Isolated test data directory")
args = parser.parse_args()
drivers = Drivers(args.data.resolve())
for chip in (
    "busdevice",
    "bme280",
    "sht4x",
    "ssd1306",
    "framebuf",
    "pca9685",
    "pcf8574",
    "charlcd",
):
    package = "adafruit-circuitpython-" + chip
    if package in drivers.state()["requested"]:
        continue
    drivers.start(package)
    while drivers.job["state"] == "running":
        time.sleep(0.5)
    print(package, drivers.job["state"], flush=True)
    if drivers.job["state"] != "done":
        print(drivers.job["log"])
        raise SystemExit(drivers.job["error"])
validate_site(drivers.site)
print("PIONEER_ADAFRUIT_TEST_SITE=" + str(drivers.site))
