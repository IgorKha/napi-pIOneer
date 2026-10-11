"""Manual LCD wiring test, independent of Pioneer and Adafruit integration.

Run on Napi-C: python3 smoke.py --rows 4
Only run with no application using this I2C address. Standard backpack mapping:
P0=RS, P1=RW, P2=EN, P3=backlight, P4..P7=D4..D7.
"""

import argparse
import time

from smbus2 import SMBus, i2c_msg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, choices=(2, 4), default=2)
    parser.add_argument("--address", type=lambda value: int(value, 0), default=0x27)
    args = parser.parse_args()
    if args.address not in (*range(0x20, 0x28), *range(0x38, 0x40)):
        parser.error("address must be in the PCF8574/PCF8574A range")
    rows = (
        ["Napi-C Pioneer", "LCD1602 I2C 0x27"]
        if args.rows == 2
        else ["1 Napi-C Pioneer", "2 LCD1604 16x4", "3 I2C1 0x27", "4 Line four"]
    )
    offsets = (0x00, 0x40, 0x10, 0x50)
    with SMBus(1) as bus:
        def port(value):
            bus.i2c_rdwr(i2c_msg.write(args.address, [value]))

        def nibble(value, rs=0):
            value = (value & 0xF0) | 8 | rs
            port(value)
            port(value | 4)
            time.sleep(0.001)
            port(value)
            time.sleep(0.001)

        def write(value, rs=0):
            nibble(value, rs)
            nibble(value << 4, rs)
            time.sleep(0.005)

        def read(rs=0):
            value = 0xF0 | 8 | 2 | rs
            parts = []
            for _ in range(2):
                port(value)
                port(value | 4)
                time.sleep(0.001)
                message = i2c_msg.read(args.address, 1)
                bus.i2c_rdwr(message)
                parts.append(list(message)[0] & 0xF0)
                port(value)
                time.sleep(0.001)
            port(8)
            return parts[0] | (parts[1] >> 4)

        try:
            port(8)
            time.sleep(0.1)
            for _ in range(3):
                nibble(0x30)
                time.sleep(0.01)
            nibble(0x20)
            time.sleep(0.01)
            for command in (0x28, 0x08, 0x01, 0x06, 0x0C):
                write(command)
            for offset, text in zip(offsets, rows):
                write(0x80 | offset)
                for character in text.ljust(16):
                    write(ord(character), 1)
            for row, (offset, text) in enumerate(zip(offsets, rows), start=1):
                write(0x80 | offset)
                status = read()
                if status != offset:
                    raise RuntimeError(f"row {row}: cursor {status:#x}, expected {offset:#x}")
                data = bytes(read(1) for _ in range(16))
                expected = text.ljust(16).encode("ascii")
                if data != expected:
                    raise RuntimeError(f"row {row}: {data!r}, expected {expected!r}")
                print(f"row {row}, DDRAM {offset:#04x}: {data.decode('ascii')!r} — verified")
            write(0x80)
            print(f"PASS: I2C1 {args.address:#04x}, 16x{args.rows}, exact DDRAM readback")
            print("Physical row placement and contrast still require visual confirmation.")
        finally:
            port(8)  # RW/EN low, backlight on, including after a failed check.


if __name__ == "__main__":
    main()
