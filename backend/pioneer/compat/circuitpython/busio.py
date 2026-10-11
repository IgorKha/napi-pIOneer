"""CircuitPython buses via Pioneer HW; no direct device access in workers."""

import threading
import time
from pioneer.compat.circuitpython import current, rpc


def interface(kind, roles):
    candidates = [
        i
        for i in rpc("describe")["interfaces"]
        if i["kind"] == kind
        and all(i["pins"].get(p.id) == role for role, p in roles.items() if p is not None)
    ]
    if not candidates:
        raise ValueError(f"Нет {kind} с указанными пинами Napi-C")
    return next((i["id"] for i in candidates if i["enabled"]), candidates[0]["id"])


class Bus:
    def __init__(self, kind, interface, **args):
        self._context = current()
        self._lock = threading.Lock()
        self._thread = None
        self._h = self._call("open", kind=kind, interface=interface, **args)

    def _call(self, action, **args):
        if action != "open" and self._h is None:
            raise RuntimeError("Шина deinit; создайте новый экземпляр")
        return self._context.call(
            "circuitpython", action=action, **({} if action == "open" else {"h": self._h}), **args
        )

    def try_lock(self):
        if self._h is None:
            raise RuntimeError("Шина deinit")
        locked = self._lock.acquire(False)
        if locked:
            self._thread = threading.get_ident()
        return locked

    def unlock(self):
        self._require_lock()
        self._thread = None
        self._lock.release()

    def _require_lock(self):
        if self._thread != threading.get_ident():
            raise RuntimeError("Сначала вызовите try_lock()")

    def deinit(self):
        if self._h is not None:
            try:
                self._call("close")
            finally:
                self._h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.deinit()


class I2C(Bus):
    def __init__(self, scl, sda, *, frequency=100000, timeout=255):
        if frequency != 100000:
            raise NotImplementedError("Частота I2C задаётся DT; поддерживается запрос 100000 Гц")
        super().__init__("i2c", interface("i2c", {"SCL": scl, "SDA": sda}))

    def scan(self):
        self._require_lock()
        return self._call("scan")

    def writeto(self, address, buffer, *, start=0, end=None, stop=True):
        self._require_lock()
        if not stop:
            raise NotImplementedError("Для repeated-start используйте writeto_then_readfrom")
        self._call(
            "i2c",
            operation="write",
            address=address,
            data=bytes(memoryview(buffer)[start:end]).hex(),
        )

    def readfrom_into(self, address, buffer, *, start=0, end=None):
        self._require_lock()
        target = memoryview(buffer)[start:end]
        target[:] = bytes.fromhex(
            self._call("i2c", operation="read", address=address, n=len(target))
        )

    def writeto_then_readfrom(
        self, address, out_buffer, in_buffer, *, out_start=0, out_end=None, in_start=0, in_end=None
    ):
        self._require_lock()
        target = memoryview(in_buffer)[in_start:in_end]
        target[:] = bytes.fromhex(
            self._call(
                "i2c",
                operation="write_read",
                address=address,
                data=bytes(memoryview(out_buffer)[out_start:out_end]).hex(),
                n=len(target),
            )
        )


class SPI(Bus):
    def __init__(self, clock, MOSI=None, MISO=None, *, half_duplex=False):
        if half_duplex:
            raise NotImplementedError("SPI half_duplex не поддерживается")
        super().__init__("spi", interface("spi", {"CLK": clock, "MOSI": MOSI, "MISO": MISO}))
        self.frequency = 1000000

    def configure(self, *, baudrate=1000000, polarity=0, phase=0, bits=8):
        self._require_lock()
        if bits != 8 or polarity not in (0, 1) or phase not in (0, 1):
            raise ValueError("SPI: bits=8, polarity/phase=0|1")
        self._call("configure", mode=(polarity << 1) | phase, baudrate=int(baudrate))
        self.frequency = baudrate

    def write(self, buffer, *, start=0, end=None):
        self._require_lock()
        self._call("spi", data=bytes(memoryview(buffer)[start:end]).hex())

    def readinto(self, buffer, *, start=0, end=None, write_value=0):
        target = memoryview(buffer)[start:end]
        self.write_readinto(bytes([write_value]) * len(target), target)

    def write_readinto(
        self, out_buffer, in_buffer, *, out_start=0, out_end=None, in_start=0, in_end=None
    ):
        self._require_lock()
        source, target = (
            memoryview(out_buffer)[out_start:out_end],
            memoryview(in_buffer)[in_start:in_end],
        )
        if len(source) != len(target):
            raise ValueError("SPI buffers должны иметь одинаковую длину")
        target[:] = bytes.fromhex(self._call("spi", data=bytes(source).hex()))


class UART(Bus):
    def __init__(
        self,
        tx,
        rx,
        *,
        baudrate=9600,
        bits=8,
        parity=None,
        stop=1,
        timeout=1,
        receiver_buffer_size=64,
    ):
        if bits != 8 or parity is not None or stop != 1:
            raise NotImplementedError("UART: только 8N1")
        if timeout is None or not 0 <= timeout <= 5:
            raise ValueError("UART timeout: 0…5 с")
        super().__init__("uart", interface("uart", {"TX": tx, "RX": rx}), baudrate=baudrate)
        self._baudrate, self.timeout, self._buffer = baudrate, timeout, bytearray()

    @property
    def baudrate(self):
        return self._baudrate

    @baudrate.setter
    def baudrate(self, value):
        self._call("configure", baudrate=int(value))
        self._baudrate = int(value)

    def _fill(self, n, timeout):
        if n <= 0:
            return
        self._buffer.extend(
            bytes.fromhex(self._call("uart", operation="read", n=n, timeout=timeout))
        )

    @property
    def in_waiting(self):
        self._fill(max(0, 4096 - len(self._buffer)), 0)
        return len(self._buffer)

    def read(self, nbytes=None):
        nbytes = min(4096, nbytes if nbytes is not None else 4096)
        if nbytes < 0:
            raise ValueError("nbytes >= 0")
        if len(self._buffer) < nbytes:
            self._fill(nbytes - len(self._buffer), self.timeout)
        value = bytes(self._buffer[:nbytes])
        del self._buffer[:nbytes]
        return value or None

    def readinto(self, buffer):
        data = self.read(len(buffer))
        if data:
            buffer[: len(data)] = data
        return len(data) if data else None

    def readline(self):
        deadline = time.monotonic() + self.timeout
        while b"\n" not in self._buffer and len(self._buffer) < 4096:
            self._fill(1, max(0, deadline - time.monotonic()))
            if time.monotonic() >= deadline:
                break
        n = self._buffer.find(b"\n") + 1
        n = n or len(self._buffer)
        data = bytes(self._buffer[:n])
        del self._buffer[:n]
        return data or None

    def write(self, buffer):
        return self._call("uart", operation="write", data=bytes(buffer).hex())

    def reset_input_buffer(self):
        self._buffer.clear()
        self._fill(4096, 0)
        self._buffer.clear()
