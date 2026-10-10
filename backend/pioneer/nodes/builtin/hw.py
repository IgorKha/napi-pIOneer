"""Железо: цифровые входы/выходы, АЦП, soft-PWM, UART, LED платы (spec 3.7)."""
from __future__ import annotations

import collections
import threading

from pioneer.engine.node import In, Node, NodeError, Out, Param, node
from pioneer.engine.types import Origin

CAT, COLOR = "Железо", "#b5523b"
_PIN_DOC = "Пин гребёнки, например J9-7. Можно перетащить провод с пина платы на ноду."


@node("builtin.digital_in", title="Цифровой вход", category=CAT, color=COLOR, doc="""
Кнопка, концевик, датчик с цифровым выходом. Выдаёт уровень и события по фронтам.

Фронты приходят из ядра (gpiod) с ядерным антидребезгом; вход разделяемый — тот же пин
могут слушать другие проекты и скрипты.

Пример: кнопка на J10-2 → Toggle → Цифровой выход J9-7.

Железо: логика 3.3 В, не подавать 5 В. Подтяжку софтом не поменять — у пина своя
аппаратная (см. тултип пина), при необходимости — внешний резистор 10 кОм.
Кнопка на GND с подтяжкой вверх даёт LOW при нажатии — включите «Инвертировать».
""")
class DigitalIn(Node):
    pin = Param("pin", None, title="Пин", modes=("gpio_in",), required=True, doc=_PIN_DOC)
    invert = Param("bool", False, title="Инвертировать",
                   doc="value = НЕ уровень (кнопка на GND: нажата = true)")
    debounce_ms = Param("int", 20, title="Антидребезг", min=0, max=1000, unit="мс",
                        doc="Ядро отбрасывает импульсы короче этого; для кнопок 20–50 мс, 0 — выключено")
    value = Out("Bool", title="Значение", doc="Уровень на пине с учётом «Инвертировать»")
    rising = Out("Event", title="Включение", doc="value стал true (нажатие)")
    falling = Out("Event", title="Выключение", doc="value стал false (отпускание)")

    def setup(self):
        self.hw.input(self, self.pin, self.debounce_ms, self._edge)

    def _edge(self, pin, ev):  # поток HAL
        level = ev.rising
        self.from_thread(Origin("gpio", pin), lambda: self._apply(level))

    def start(self):
        self._apply(bool(self.hw.value(self.pin)), initial=True)

    def _apply(self, level: bool, initial: bool = False):
        v = level != self.invert
        prev = self.value.value
        self.value.set(v)
        if not initial and prev is not None and prev != v:
            (self.rising if v else self.falling).emit()


@node("builtin.digital_out", title="Цифровой выход", category=CAT, color=COLOR, doc="""
LED, транзисторный ключ, модуль реле с драйвером. Выход захватывается проектом
эксклюзивно: другой проект с этим пином не запустится.

Безопасное состояние выставляется при старте и при остановке проекта (и при панике).

Пример: Гистерезис(температура) → Задержка → Цифровой выход (реле насоса).

Железо: 3.3 В и 2–8 мА — только логический сигнал. Реле, мотор, лампу — только через
модуль с транзистором/оптроном, не напрямую. Модули реле на 5 В с активным LOW —
включите «Инвертировать».
""")
class DigitalOut(Node):
    pin = Param("pin", None, title="Пин", modes=("gpio_out",), required=True, doc=_PIN_DOC)
    invert = Param("bool", False, title="Инвертировать", doc="true на входе = LOW на пине (реле с активным LOW)")
    safe_state = Param("choice", "off", title="Безопасное состояние",
                       choices=[("off", "Выключен"), ("on", "Включён")],
                       doc="Состояние при старте/остановке проекта (с учётом «Инвертировать»)")
    value = In("Bool", False, title="Значение", doc="Включить выход")

    def _level(self, on: bool) -> bool:
        return bool(on) != self.invert

    def setup(self):
        self.hw.output(self.pin, initial=self._level(self.safe_state == "on"))

    def start(self):
        self.on_change({"value"})

    def on_change(self, changed):
        self.hw.write(self.pin, self._level(self.value))

    def stop(self):
        self.hw.release(self.pin, safe_value=self._level(self.safe_state == "on"))


@node("builtin.analog_in", title="Аналоговый вход", category=CAT, color=COLOR, doc="""
Напряжение на входе АЦП (J9-26 — канал 0, J10-3 — канал 1): опрос с заданным
интервалом, усреднение последних N отсчётов.

Выходы: «Напряжение» 0…1.8 В и «Доля» 0…1 (удобно для Map Range).

Пример: потенциометр → Аналоговый вход → Map Range (0…1 → 0…100) → Watch.

Железо: диапазон 0…1.8 В, 10 бит (≈1.76 мВ). Больше 1.8 В — только через делитель
(для 3.3 В: 10 кОм + 12 кОм). Потенциометр питайте от 1.8 В или ставьте делитель.
""")
class AnalogIn(Node):
    pin = Param("pin", None, title="Пин", modes=("adc",), required=True, doc="J9-26 или J10-3")
    interval_ms = Param("int", 100, title="Интервал", min=10, max=60_000, unit="мс", doc="Период опроса")
    average = Param("int", 4, title="Усреднение", min=1, max=100, doc="Сколько последних отсчётов усреднять")
    deadband = Param("float", 0.002, title="Порог изменения", min=0, max=1.8, unit="В",
                     doc="Меньшие изменения не передаются дальше (подавление шума)")
    volts = Out("Number", title="Напряжение", doc="Среднее напряжение, В (0…1.8)")
    ratio = Out("Number", title="Доля", doc="Напряжение / 1.8 — 0…1")

    def setup(self):
        self.hw.adc(self.pin)
        self._win = collections.deque(maxlen=self.average)
        self._last: float | None = None

    def start(self):
        self._poll()
        self.every(self.interval_ms / 1000, self._poll, origin=Origin("adc", self.pin))

    def _poll(self):
        self._win.append(float(self.hw.value(self.pin)))
        avg = sum(self._win) / len(self._win)
        if self._last is None or abs(avg - self._last) >= self.deadband:
            self._last = avg
            vref = self.hw.board.adc_vref
            self.volts.set(round(avg, 4))
            self.ratio.set(round(avg / vref, 4))


@node("builtin.soft_pwm", title="ШИМ (программный)", category=CAT, color=COLOR, doc="""
Программный ШИМ на любом GPIO-пине: яркость LED, медленные нагрузки через ключ.

Заполнение 0…1 (вход «Заполнение»), частота — параметр (до 500 Гц). Отдельный поток
реального времени держит джиттер < 0.1 мс.

Пример: Слайдер (0…1) → ШИМ J9-7 (LED через резистор).

Железо: не годится для вентиляторов 25 кГц и серв (нужна точность) — для них аппаратный
PWM (J9-11/J9-13, фаза 3) или внешний контроллер. Ток пина 2–8 мА.
""")
class SoftPwm(Node):
    pin = Param("pin", None, title="Пин", modes=("soft_pwm",), required=True, doc=_PIN_DOC)
    freq = Param("float", 100.0, title="Частота", min=1, max=500, unit="Гц", doc="Частота ШИМ")
    duty = In("Number", 0.0, title="Заполнение", min=0, max=1, step=0.01, doc="0 — выключен, 1 — включён всегда")

    def setup(self):
        self.hw.pwm(self.pin)
        self.hw.set_pwm(self.pin, freq=self.freq, duty=0.0)

    def start(self):
        self.on_change({"duty"})

    def on_change(self, changed):
        self.hw.set_pwm(self.pin, duty=min(1.0, max(0.0, self.duty)))


@node("builtin.uart_read", title="UART: чтение", category=CAT, color=COLOR, doc="""
Принимает строки из последовательного порта. Каждая строка (до \\n, \\r отрезается) —
событие «Получено» с текстом в payload и значение «Строка».

Порт захватывается проектом эксклюзивно; UART: запись с тем же интерфейсом в этом же
проекте использует тот же порт.

Пример: GPS-модуль на uart3 → UART: чтение → Log.

Железо: уровни 3.3 В. RS-232 — через MAX3232, RS-485 — через модуль. На uart2 по
умолчанию висит консоль serial-getty@ttyS2 — её надо остановить.
""")
class UartRead(Node):
    interface = Param("interface", "uart3", title="Интерфейс", modes=("uart",), required=True,
                      doc="uart1, uart2, uart3, uart4 — должен быть включён оверлеем")
    baud = Param("int", 9600, title="Скорость", min=50, max=4_000_000, unit="бод", doc="Скорость порта")
    line = Out("String", title="Строка", doc="Последняя принятая строка")
    received = Out("Event", title="Получено", doc="Пришла строка; payload — её текст")

    def setup(self):
        self._port = self.hw.uart(self.interface, self.baud)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._reader, name=f"uart-read-{self.id}", daemon=True)
        self._thread.start()

    def _reader(self):
        buf = b""
        origin = Origin("uart", self.interface)
        while not self._stop.is_set():
            try:
                data = self._port.read(4096, 0.2)
            except (OSError, ValueError):
                if self._stop.is_set():
                    return
                raise
            if not data:
                continue
            buf += data
            while b"\n" in buf or len(buf) > 4096:
                raw, sep, buf = buf.partition(b"\n") if b"\n" in buf else (buf, b"", b"")
                text = raw.rstrip(b"\r").decode("utf-8", "replace")
                self.from_thread(origin, lambda t=text: self._got(t))

    def _got(self, text: str):
        self.line.set(text)
        self.received.emit(text)

    def stop(self):
        if hasattr(self, "_stop"):
            self._stop.set()
            self._thread.join(1.0)


@node("builtin.uart_write", title="UART: запись", category=CAT, color=COLOR, doc="""
Отправляет строку в последовательный порт: по событию «Отправить» или (опционально)
при каждом изменении текста. К строке добавляется окончание (\\n, \\r\\n или ничего).

Пример: Аналоговый вход → Expression → UART: запись (send по Interval) — телеметрия.

Железо: уровни 3.3 В; см. UART: чтение.
""")
class UartWrite(Node):
    interface = Param("interface", "uart3", title="Интерфейс", modes=("uart",), required=True,
                      doc="uart1, uart2, uart3, uart4 — должен быть включён оверлеем")
    baud = Param("int", 9600, title="Скорость", min=50, max=4_000_000, unit="бод", doc="Скорость порта")
    ending = Param("choice", "lf", title="Окончание строки",
                   choices=[("lf", "\\n"), ("crlf", "\\r\\n"), ("none", "нет")], doc="Что дописать после текста")
    on_text_change = Param("bool", False, title="Слать при изменении",
                           doc="Отправлять каждый раз, когда меняется текст (без события)")
    text = In("String", "", title="Текст", doc="Что отправить")
    send = In("Event", title="Отправить", doc="Отправить текущий текст")

    def setup(self):
        self._port = self.hw.uart(self.interface, self.baud)

    def _send(self):
        end = {"lf": "\n", "crlf": "\r\n", "none": ""}[self.ending]
        self._port.write((self.text + end).encode())

    def on_event(self, name, ev):
        self._send()

    def on_change(self, changed):
        if self.on_text_change and "text" in changed:
            self._send()


@node("builtin.board_led", title="LED платы", category=CAT, color=COLOR, doc="""
Встроенный светодиод платы: «user» (синий) или «power» (зелёный). На гребёнку не
выведены — удобны как индикатор состояния без проводов.

При остановке проекта LED возвращается системному триггеру (heartbeat / default-on).
""")
class BoardLed(Node):
    led = Param("led", "user", title="LED", required=True, doc="user (синий) или power (зелёный)")
    on = In("Bool", False, title="Включён", doc="Включить светодиод")

    def setup(self):
        self._led = self.hw.led(self.led)

    def start(self):
        self.on_change({"on"})

    def on_change(self, changed):
        self._led.set(self.on)



@node("builtin.tm1637", title="Дисплей TM1637", category=CAT, color=COLOR, doc="""
4-разрядный 7-сегментный индикатор на TM1637 (модуль HW-069 и подобные), два провода:
CLK и DIO на любые GPIO-пины. Драйвер работает внутри сервиса — кадр выводится за
миллисекунды (из Python-скрипта тот же протокол стоит ~0.7 с из-за вызовов к ядру).

Что показывать:
- «Текст» (если не пустой) — до 4 знаков: цифры, `-`, `_`, `°`, часть латиницы (`PASS`,
  `Err`, `HOLd`); `12:34` — с двоеточием;
- иначе «Число»: целое −999…9999 или, в формате «ММ:СС», секунды как часы.

«Двоеточие» включает точки между разрядами (подключите Переключатель от Интервала —
будет мигать). Яркость 0–7. При остановке проекта индикатор гаснет.

Пример: Интервал 1 с → Счётчик → Дисплей TM1637 (формат ММ:СС) — секундомер.

Железо: питание модуля 3.3 В (от 5 В подтяжки модуля выдадут 5 В на пины платы — нельзя).
От 3.3 В светит чуть тусклее.
""")
class Tm1637(Node):
    clk = Param("pin", None, title="CLK", modes=("gpio_out",), required=True, doc="Пин тактов (CLK модуля)")
    dio = Param("pin", None, title="DIO", modes=("gpio_out",), required=True, doc="Пин данных (DIO модуля)")
    format = Param("choice", "number", title="Формат числа",
                   choices=[("number", "целое"), ("mmss", "ММ:СС из секунд")], doc="Как показывать вход «Число»")
    brightness = Param("int", 4, title="Яркость", min=0, max=7, doc="0 — тускло, 7 — максимум")
    value = In("Number", 0.0, title="Число")
    text = In("String", "", title="Текст", doc="Если не пусто — показывается вместо числа")
    colon = In("Bool", False, title="Двоеточие")

    def setup(self):
        from pioneer.drivers.tm1637 import TM1637
        self.hw.output(self.clk, initial=True)
        self.hw.output(self.dio, initial=True)
        pins = {"clk": self.clk, "dio": self.dio}
        self._drv = TM1637(lambda line, v: self.hw.write(pins[line], v))
        # кадры выводит свой поток: волна не ждёт bit-bang, промежуточные кадры схлопываются
        self._frame: tuple | None = None
        self._cv = threading.Condition()
        self._quit = False
        self._thread = threading.Thread(target=self._writer, name=f"tm1637-{self.id}", daemon=True)
        self._thread.start()

    def _writer(self):
        while True:
            with self._cv:
                while self._frame is None and not self._quit:
                    self._cv.wait()
                if self._quit:
                    return
                frame, self._frame = self._frame, None
            try:
                self._drv.show(*frame)
            except Exception as e:  # линия отобрана при остановке и т.п.
                if not self._quit:
                    self.from_thread(Origin("system"), lambda err=e: self._raise(err))

    def _raise(self, e: Exception):
        raise e

    def start(self):
        self.on_change({"value"})

    def on_change(self, changed):
        from pioneer.drivers.tm1637 import DisplayError, encode_mmss, encode_number, encode_text
        try:
            if self.text:
                segs, colon = encode_text(self.text)
            else:
                segs, colon = (encode_mmss if self.format == "mmss" else encode_number)(self.value), False
        except DisplayError as e:
            segs, colon = encode_text("----")[0], False
            self._push(segs, colon)
            raise NodeError(str(e)) from None
        self._push(segs, colon or self.colon)

    def _push(self, segs, colon):
        with self._cv:
            self._frame = (segs, colon, self.brightness)
            self._cv.notify()

    def stop(self):
        if not hasattr(self, "_thread"):
            return
        with self._cv:
            self._quit = True
            self._cv.notify()
        self._thread.join(2.0)
        try:
            self._drv.off()  # погасить, пока линии ещё наши
        except Exception:
            pass
