"""Non-blocking I2C sensor nodes with explicit validity and exclusive address claims."""

import threading

from pioneer.engine.node import Node, Param, Out, node
from pioneer.engine.types import Origin
from pioneer.drivers import sensors


class Sensor(Node):
    interface = Param("interface", "i2c1", title="Шина", modes=("i2c",), required=True)
    interval_ms = Param("int", 2000, title="Интервал", min=500, max=3600000, unit="мс")
    valid = Out("Bool", title="Измерение корректно")
    error = Out("String", title="Ошибка")
    measured = Out("Event", title="Измерено")

    def setup(self):
        if self.addr not in self.addresses:
            raise ValueError("неверный адрес датчика")
        self._io = self.hw.i2c(self.interface, self.addr)
        self._pending = False
        self._idle = threading.Event()
        self._idle.set()

    def start(self):
        self.valid.set(False)
        self._poll()
        self.every(self.interval_ms / 1000, self._poll, origin=Origin("i2c", self.interface))

    def _poll(self):
        if self._pending:
            return
        self._pending = True
        self._idle.clear()

        def read():
            try:
                return self.measure(self._io)
            finally:
                self._idle.set()

        self.run_async(read, self._done)

    def _done(self, sample, failure):
        self._pending = False
        if failure:
            self.valid.set(False)
            self.error.set(str(failure))
            return
        for key, value in sample.items():
            getattr(self, key).set(value)
        self.valid.set(True)
        self.error.set("")
        self.measured.emit(sample)

    def stop(self):
        if hasattr(self, "_idle"):
            self._idle.wait(0.5)  # finish the bounded conversion before releasing its address


@node(
    "builtin.sht30",
    title="SHT30 / SHT31: температура и влажность",
    category="Датчики",
    color="#639477",
    doc="""
I2C-датчик SHT3x-DIS: температура °C и относительная влажность % RH. Адрес 0x44
(68) или 0x45 (69); питание и подтяжки SDA/SCL — 3.3 В. Шина должна быть включена.
Адрес захватывается проектом эксклюзивно. Каждый ответ проверяется по CRC.
Чтение выполняется вне потока движка. При ошибке valid=false, числовые значения
остаются последними успешными; используйте valid при управлении нагрузкой.
Пример: Температура → Гистерезис → реле через модуль драйвера.
""",
)
class Sht30(Sensor):
    addr = Param("int", 0x44, title="Адрес (десятичный)", required=True, min=0x44, max=0x45)
    addresses = (0x44, 0x45)
    measure = staticmethod(sensors.sht30)
    temperature = Out("Number", title="Температура, °C")
    humidity = Out("Number", title="Влажность, % RH")


@node(
    "builtin.bh1750",
    title="BH1750: освещённость",
    category="Датчики",
    color="#639477",
    doc="""
I2C-датчик освещённости, выход в люксах. Адрес 0x23 (35) или 0x5C (92), питание
и подтяжки SDA/SCL — 3.3 В. Шина должна быть включена. Адрес захватывается
эксклюзивно; одноразовое измерение 180 мс выполняется вне потока движка.
При ошибке valid=false, lux остаётся последним успешным значением.
Пример: Освещённость → Сравнение (< порога) → освещение через модуль реле.
""",
)
class Bh1750(Sensor):
    addr = Param("int", 0x23, title="Адрес (десятичный)", required=True, min=0x23, max=0x5C)
    addresses = (0x23, 0x5C)
    measure = staticmethod(sensors.bh1750)
    lux = Out("Number", title="Освещённость, лк")
