"""Фаза 0, открытые вопросы 2–3: проверка gpiod debounce/edge и soft-PWM на плате.

Запуск на плате:
    python3 gpio_probe.py debounce            # без проводов
    python3 gpio_probe.py pwm-nowire          # джиттер soft-PWM без проводов
    python3 gpio_probe.py loopback OUT IN     # нужна перемычка между пинами (chip:line)
"""
from __future__ import annotations

import os
import statistics
import sys
import threading
import time
from datetime import timedelta

import gpiod
from gpiod.line import Direction, Edge, Value

CONSUMER = "pioneer-probe"
EVBUF = int(os.environ.get("EVBUF", "16"))
RT = os.environ.get("RT") == "1"


def parse(spec: str) -> tuple[str, int]:
    chip, line = spec.split(":")
    return f"/dev/gpiochip{chip}", int(line)


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def summary(name: str, xs_us: list[float]) -> None:
    print(
        f"  {name}: n={len(xs_us)} mean={statistics.mean(xs_us):.1f}us "
        f"p50={pct(xs_us, .5):.1f} p95={pct(xs_us, .95):.1f} "
        f"p99={pct(xs_us, .99):.1f} max={max(xs_us):.1f}"
    )


def cmd_debounce(spec: str = "2:4") -> None:
    path, line = parse(spec)
    chip = gpiod.Chip(path)
    for period in (timedelta(0), timedelta(milliseconds=5), timedelta(milliseconds=20)):
        try:
            req = chip.request_lines(
                consumer=CONSUMER,
                config={line: gpiod.LineSettings(
                    direction=Direction.INPUT, edge_detection=Edge.BOTH,
                    debounce_period=period)},
            )
        except OSError as e:
            print(f"  debounce={period}: request FAILED: {e}")
            continue
        info = chip.get_line_info(line)
        print(f"  debounce={period}: request ok, line_info.debounced={info.debounced} "
              f"period={info.debounce_period} edge={info.edge_detection} "
              f"clock={info.event_clock}")
        req.release()


def soft_pwm(req, line: int, freq: float, duty: float, stop: threading.Event,
             marks: list[float] | None = None) -> None:
    """Наивный soft-PWM: sleep до абсолютных дедлайнов, без накопления дрейфа."""
    if RT:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(50))
    period = 1.0 / freq
    on_t = period * duty
    t = time.monotonic()
    while not stop.is_set():
        req.set_value(line, Value.ACTIVE)
        if marks is not None:
            marks.append(time.monotonic())
        t_off = t + on_t
        time.sleep(max(0.0, t_off - time.monotonic()))
        req.set_value(line, Value.INACTIVE)
        t += period
        time.sleep(max(0.0, t - time.monotonic()))


def cmd_pwm_nowire(spec: str = "2:4") -> None:
    """Меряет момент set_value(ACTIVE) по monotonic: ошибка периода относительно идеала."""
    path, line = parse(spec)
    with gpiod.request_lines(path, consumer=CONSUMER, config={line: gpiod.LineSettings(
            direction=Direction.OUTPUT, output_value=Value.INACTIVE)}) as req:
        for freq in (50, 100, 200, 500, 1000):
            marks: list[float] = []
            stop = threading.Event()
            th = threading.Thread(target=soft_pwm, args=(req, line, freq, 0.5, stop, marks))
            th.start()
            time.sleep(3)
            stop.set()
            th.join()
            ideal = 1e6 / freq
            err = [abs((b - a) * 1e6 - ideal) for a, b in zip(marks, marks[1:])]
            print(f" freq={freq}Hz periods={len(err)} (ожидалось ~{3*freq})")
            summary("|period error|", err)


def cmd_loopback(out_spec: str, in_spec: str, load: str = "0") -> None:
    out_path, out_line = parse(out_spec)
    in_path, in_line = parse(in_spec)
    out_req = gpiod.request_lines(out_path, consumer=CONSUMER, config={out_line: gpiod.LineSettings(
        direction=Direction.OUTPUT, output_value=Value.INACTIVE)})
    in_req = gpiod.request_lines(in_path, consumer=CONSUMER, event_buffer_size=EVBUF, config={in_line: gpiod.LineSettings(
        direction=Direction.INPUT, edge_detection=Edge.BOTH)})

    # 1) Проверка, что перемычка есть
    for v in (Value.ACTIVE, Value.INACTIVE, Value.ACTIVE, Value.INACTIVE):
        out_req.set_value(out_line, v)
        time.sleep(0.01)
        got = in_req.get_value(in_line)
        print(f"  set {v.name} -> read {got.name}")
        if got != v:
            print("  !! вход не повторяет выход — перемычки нет?")
            return
    while in_req.wait_edge_events(timedelta(milliseconds=50)):
        in_req.read_edge_events()

    burners = []
    if load != "0":
        import multiprocessing as mp
        burners = [mp.Process(target=_burn, daemon=True) for _ in range(int(load))]
        for p in burners:
            p.start()
        print(f"  нагрузка: {load} процессов busy-loop")

    # 2) Soft-PWM на OUT, edge-события на IN с ядерными таймстемпами
    for freq in (50, 100, 200, 500):
        stop = threading.Event()
        th = threading.Thread(target=soft_pwm, args=(out_req, out_line, freq, 0.5, stop))
        events = []
        th.start()
        t_end = time.monotonic() + 3
        while time.monotonic() < t_end:
            if in_req.wait_edge_events(timedelta(milliseconds=100)):
                events.extend(in_req.read_edge_events())
        stop.set()
        th.join()
        while in_req.wait_edge_events(timedelta(milliseconds=50)):
            events.extend(in_req.read_edge_events())

        rising = [e.timestamp_ns for e in events if e.event_type == e.Type.RISING_EDGE]
        falling = [e.timestamp_ns for e in events if e.event_type == e.Type.FALLING_EDGE]
        seqs = [e.line_seqno for e in events]
        gaps = sum(1 for a, b in zip(seqs, seqs[1:]) if b != a + 1)
        alt_bad = sum(1 for a, b in zip(events, events[1:]) if a.event_type == b.event_type)
        ideal = 1e6 / freq
        per = [(b - a) / 1e3 for a, b in zip(rising, rising[1:])]
        err = [abs(p - ideal) for p in per]
        # duty по парам rising->falling
        highs = []
        fi = 0
        for r in rising:
            while fi < len(falling) and falling[fi] < r:
                fi += 1
            if fi < len(falling):
                highs.append((falling[fi] - r) / 1e3)
        print(f" freq={freq}Hz: events={len(events)} (ожидалось ~{6*freq}) rising={len(rising)} "
              f"seq_gaps={gaps} same-type-consecutive={alt_bad}")
        if err:
            summary("|period error| (kernel ts)", err)
        if highs:
            duty = [h / ideal for h in highs]
            print(f"  duty: mean={statistics.mean(duty):.3f} min={min(duty):.3f} max={max(duty):.3f}")

    for p in burners:
        p.terminate()

    # 3) Латентность «edge на входе → реакция в userspace»
    lat = []
    for _ in range(200):
        out_req.set_value(out_line, Value.ACTIVE)
        if in_req.wait_edge_events(timedelta(seconds=1)):
            ev = in_req.read_edge_events()[0]
            lat.append((time.monotonic_ns() - ev.timestamp_ns) / 1e3)
        out_req.set_value(out_line, Value.INACTIVE)
        in_req.wait_edge_events(timedelta(seconds=1)) and in_req.read_edge_events()
        time.sleep(0.005)
    summary("event->userspace latency", lat)

    # 4) Реально ли фильтрует debounce: короткие импульсы (~0.2–1 мс) при debounce=10 мс
    in_req.release()
    for period_ms in (0, 10):
        in_req = gpiod.request_lines(in_path, consumer=CONSUMER, event_buffer_size=EVBUF, config={in_line: gpiod.LineSettings(
            direction=Direction.INPUT, edge_detection=Edge.BOTH,
            debounce_period=timedelta(milliseconds=period_ms))})
        time.sleep(0.05)
        while in_req.wait_edge_events(timedelta(milliseconds=50)):
            in_req.read_edge_events()
        for _ in range(50):  # 50 коротких импульсов
            out_req.set_value(out_line, Value.ACTIVE)
            time.sleep(0.0002)
            out_req.set_value(out_line, Value.INACTIVE)
            time.sleep(0.03)
        n_short = 0
        while in_req.wait_edge_events(timedelta(milliseconds=100)):
            n_short += len(in_req.read_edge_events())
        for _ in range(10):  # 10 длинных импульсов по 50 мс
            out_req.set_value(out_line, Value.ACTIVE)
            time.sleep(0.05)
            out_req.set_value(out_line, Value.INACTIVE)
            time.sleep(0.05)
        n_long = 0
        while in_req.wait_edge_events(timedelta(milliseconds=100)):
            n_long += len(in_req.read_edge_events())
        print(f"  debounce={period_ms}ms: короткие 50 имп. -> {n_short} событий; "
              f"длинные 10 имп. -> {n_long} событий (ожидалось 20)")
        in_req.release()

    out_req.release()


def _burn() -> None:
    os.nice(0)
    x = 0
    while True:
        x += 1


if __name__ == "__main__":
    cmd, *args = sys.argv[1:]
    {"debounce": cmd_debounce, "pwm-nowire": cmd_pwm_nowire, "loopback": cmd_loopback}[cmd](*args)
