"""Прямой ввод-вывод в worker-процессе (блоки, скрипты) и счётчик активности.

Обход «worker → core → HAL» стоит ~3 мс на A35. Драйвер LCD через PCF8574 делает
одну запись I2C на каждый бит, и строка текста превращается в сотни вызовов — секунды.
Поэтому для I2C core только захватывает адрес (проверка владельца остаётся в
ResourceManager), а транзакции worker делает сам через /dev/i2c-N. На стенде (BenchHAL)
прямого доступа нет — там нужна модель устройства, и всё идёт через core.

`ops` — счётчик операций с железом в этом процессе: core продлевает таймаут обработчика,
пока счётчик растёт (обработчик работает, а не завис).
"""
from __future__ import annotations

import threading

ops = 0
_lock = threading.Lock()


def bump() -> None:
    global ops
    ops += 1  # гонка безвредна: нужен лишь факт роста


class DirectI2C:
    """Одна шина /dev/i2c-N на процесс; транзакция write+read — один ioctl (атомарно)."""

    def __init__(self, bus: int):
        from smbus2 import SMBus
        self.smbus = SMBus(bus)
        self.lock = threading.Lock()

    def transfer(self, addr: int, data: bytes = b"", n: int = 0) -> bytes:
        from smbus2 import i2c_msg
        msgs = []
        if data:
            msgs.append(i2c_msg.write(addr, bytes(data)))
        if n:
            rd = i2c_msg.read(addr, n)
            msgs.append(rd)
        with self.lock:
            self.smbus.i2c_rdwr(*msgs)
        bump()
        return bytes(rd) if n else b""


_buses: dict[int, DirectI2C] = {}


def i2c(bus: int) -> DirectI2C:
    with _lock:
        if bus not in _buses:
            _buses[bus] = DirectI2C(bus)
        return _buses[bus]
