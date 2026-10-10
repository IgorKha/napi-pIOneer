"""Проба DHT22/AM2302 из userspace: старт-импульс + захват фронтов с ядерными таймстемпами.

Протокол: хост тянет DATA в 0 ≥ 1 мс и отпускает; датчик через 20–40 мкс отвечает 80 мкс 0 +
80 мкс 1, затем 40 бит: 50 мкс 0 + (26–28 мкс 1 = «0» | 70 мкс 1 = «1»). Последние 8 бит — сумма.

Из Python протокол не «бит-бэнгается», но фронты ловит ядро (gpiod edge events, таймстемп в
обработчике прерывания) — успеть нужно только переключить линию на вход (≈ первые 200 мкс).
Декодируем по последним 40 высоким импульсам; контрольная сумма говорит, удалось ли.

Запуск на плате (линию не должен держать pioneer):
    python3 dht22_probe.py 3:10 [попыток]
"""
from __future__ import annotations

import os
import sys
import time
from datetime import timedelta

import gpiod
from gpiod.line import Direction, Edge, Value


def realtime() -> None:
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(80))
    except OSError:
        print("  (без SCHED_FIFO)")


def read_once(path: str, line: int) -> tuple[list, float]:
    out = gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE)
    inp = gpiod.LineSettings(direction=Direction.INPUT, edge_detection=Edge.BOTH)
    with gpiod.request_lines(path, consumer="dht22-probe", config={line: out},
                             event_buffer_size=1024) as req:
        time.sleep(0.002)  # старт: ≥ 1 мс в нуле
        t0 = time.monotonic_ns()
        req.reconfigure_lines({line: inp})  # отпускаем линию и сразу ловим фронты
        switch_us = (time.monotonic_ns() - t0) / 1000
        events = []
        deadline = time.monotonic() + 0.02
        while time.monotonic() < deadline:
            if req.wait_edge_events(timedelta(milliseconds=5)):
                events += req.read_edge_events()
        return events, switch_us


def decode(events) -> tuple[list[float], bytes | None]:
    highs = []  # длительности высоких импульсов, мкс
    for a, b in zip(events, events[1:]):
        if a.event_type == a.Type.RISING_EDGE and b.event_type == b.Type.FALLING_EDGE:
            highs.append((b.timestamp_ns - a.timestamp_ns) / 1000)
    if len(highs) < 40:
        return highs, None
    bits = [1 if h > 48 else 0 for h in highs[-40:]]
    data = bytes(int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, 40, 8))
    return highs, data


def main() -> None:
    chip, line = sys.argv[1].split(":")
    path, line = f"/dev/gpiochip{chip}", int(line)
    tries = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    with gpiod.request_lines(path, consumer="dht22-probe",
                             config={line: gpiod.LineSettings(direction=Direction.INPUT)}) as req:
        idle = req.get_value(line)
    print(f"DATA в покое: {idle.name} ({'ок, подтяжка есть' if idle == Value.ACTIVE else 'НИЗКИЙ — проверьте питание/подключение'})")

    realtime()
    for n in range(tries):
        if n:
            time.sleep(2.2)  # DHT22: не чаще раза в 2 с
        events, switch_us = read_once(path, line)
        highs, data = decode(events)
        print(f"попытка {n + 1}: переключение на вход {switch_us:.0f} мкс, фронтов {len(events)}, "
              f"высоких импульсов {len(highs)}")
        if data is None:
            short = [round(h) for h in highs if h <= 48]
            long_ = [round(h) for h in highs if h > 48]
            print(f"  длительности: «0» {min(short, default=0)}–{max(short, default=0)} мкс, "
                  f"«1» {min(long_, default=0)}–{max(long_, default=0)} мкс")
            if len(highs) >= 24:  # хвост кадра: температура (16 бит) + сумма (8 бит)
                bits = [1 if h > 48 else 0 for h in highs[-24:]]
                t_hi, t_lo, crc = (int("".join(map(str, bits[i:i + 8])), 2) for i in (0, 8, 16))
                raw_t = (t_hi & 0x7F) << 8 | t_lo
                temp = (-raw_t if t_hi & 0x80 else raw_t) / 10
                print(f"  частично (начало потеряно): температура {temp:.1f} °C, байт суммы {crc:02x} — проверить нечем")
            else:
                print("  мало импульсов — потеряна большая часть кадра")
            continue
        ok = (sum(data[:4]) & 0xFF) == data[4]
        hum = (data[0] << 8 | data[1]) / 10
        raw_t = (data[2] & 0x7F) << 8 | data[3]
        temp = (-raw_t if data[2] & 0x80 else raw_t) / 10
        short = [round(h) for h in highs[-40:] if h <= 48]
        long_ = [round(h) for h in highs[-40:] if h > 48]
        print(f"  байты {data.hex(' ')}  сумма {'OK' if ok else 'НЕ сходится'}  "
              f"→ {temp:.1f} °C, {hum:.1f} %RH")
        print(f"  «0»: {min(short, default=0)}–{max(short, default=0)} мкс, "
              f"«1»: {min(long_, default=0)}–{max(long_, default=0)} мкс")


if __name__ == "__main__":
    main()
