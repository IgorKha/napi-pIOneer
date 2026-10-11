"""One UART reader, bounded independent buffers, one transmitter, matching baud."""

import threading
from pioneer.hal.base import UARTPort

LIMIT = 65536


class Subscription(UARTPort):
    def __init__(self, hub, owner, direction):
        self.hub, self.owner, self.direction, self.baud = hub, owner, direction, hub.baud
        self.buffer = bytearray()
        self.overflow = False
        self.closed = False

    def read(self, max_bytes=4096, timeout=0.0):
        if self.direction == "tx":
            raise ValueError("UART открыт только для записи")
        if not isinstance(max_bytes, int) or not 1 <= max_bytes <= LIMIT or not 0 <= timeout <= 5:
            raise ValueError("UART read: max 1…65536, timeout 0…5 с")
        with self.hub.condition:
            self.hub.condition.wait_for(
                lambda: self.buffer
                or self.overflow
                or self.hub.error
                or self.hub.closed
                or self.closed,
                timeout,
            )
            if self.closed or self.hub.closed:
                raise OSError("UART подписчик закрыт")
            if self.hub.error:
                raise OSError("UART reader остановлен") from self.hub.error
            if self.overflow:
                self.overflow = False
                raise OSError("переполнен буфер UART подписчика; часть данных потеряна")
            data = bytes(self.buffer[:max_bytes])
            del self.buffer[:max_bytes]
            return data

    def write(self, data):
        if self.direction == "rx":
            raise ValueError("UART открыт только для чтения")
        with self.hub.write_lock:
            if self.closed or self.hub.closed:
                raise OSError("UART подписчик закрыт")
            self.hub.port.write(data)

    def set_baud(self, baud):
        if baud != self.baud:
            raise ValueError("закройте всех владельцев UART перед сменой baud")

    def close(self):
        with self.hub.condition:
            self.closed = True
            if self in self.hub.subscriptions:
                self.hub.subscriptions.remove(self)
            self.hub.condition.notify_all()


class UARTHub:
    def __init__(self, port):
        self.port, self.baud = port, port.baud
        self.subscriptions = []
        self.condition, self.write_lock = threading.Condition(), threading.Lock()
        self.closed, self.error = False, None
        self.thread = threading.Thread(target=self._read, name="pioneer-uart-rx", daemon=True)
        self.thread.start()

    def subscribe(self, owner, direction, baud):
        if baud != self.baud:
            raise ValueError("все владельцы UART должны использовать одну скорость")
        sub = Subscription(self, owner, direction)
        with self.condition:
            self.subscriptions.append(sub)
        return sub

    def _read(self):
        try:
            while not self.closed:
                data = self.port.read(4096, 0.05)
                if not data:
                    continue
                with self.condition:
                    for sub in self.subscriptions:
                        if sub.direction == "tx":
                            continue
                        sub.buffer.extend(data)
                        if len(sub.buffer) > LIMIT:
                            del sub.buffer[:-LIMIT]
                            sub.overflow = True
                    self.condition.notify_all()
        except Exception as e:
            with self.condition:
                self.error = e
                self.condition.notify_all()

    def remove(self, owner, direction=None):
        with self.condition:
            for sub in list(self.subscriptions):
                if sub.owner == owner and (direction is None or sub.direction == direction):
                    sub.close()

    def close(self):
        self.closed = True
        with self.condition:
            self.condition.notify_all()
        self.thread.join(0.3)
        self.port.close()
