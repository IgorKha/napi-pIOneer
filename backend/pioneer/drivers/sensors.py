"""SHT3x-DIS and BH1750 one-shot measurements; transport is provided by SDK/HW.

Protocol sources are linked in docs/sdk/sensors.md. No kernel/boot changes needed.
"""

import time


def crc8(data):
    crc = 0xFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = ((crc << 1) ^ (0x31 if crc & 0x80 else 0)) & 0xFF
    return crc


def sht30(io):
    io.write(b"\x24\x00")  # high repeatability, no clock stretching
    time.sleep(0.02)  # maximum conversion time 15.5 ms
    data = io.read(6)
    if len(data) != 6 or crc8(data[:2]) != data[2] or crc8(data[3:5]) != data[5]:
        raise ValueError("SHT30: неверная длина ответа или CRC")
    return {
        "temperature": round(-45 + 175 * int.from_bytes(data[:2], "big") / 65535, 2),
        "humidity": round(100 * int.from_bytes(data[3:5], "big") / 65535, 2),
    }


def bh1750(io):
    # Reset measurement time to its documented default on every read, including
    # after another application's use or a soft restart of this node.
    io.write(b"\x01")
    io.write(b"\x42")
    io.write(b"\x65")
    io.write(b"\x20")  # one-time high-resolution mode; powers down after conversion
    time.sleep(0.18)
    data = io.read(2)
    if len(data) != 2:
        raise ValueError("BH1750: ожидаются 2 байта")
    return {"lux": round(int.from_bytes(data, "big") / 1.2, 2)}
