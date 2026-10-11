"""SDK скриптов pioneer — исполняется в worker-процессе, железо — через core (RPC).

    from pioneer.sdk import Input, Output, log

    led = Output("J9-7")
    button = Input("J10-2", debounce_ms=30)

    @button.on_press
    def toggle(event):
        led.toggle()

Если после выполнения скрипта есть обработчики или таймеры, цикл событий запускается
сам (явный run() не обязателен). Ресурсы освобождаются при остановке скрипта, выходы
уходят в безопасное состояние (LOW).

Адресация: пины — по гребёнке (`"J9-7"`), шины — по имени интерфейса из
`boards/napi-c.yaml` без mux-варианта (`"i2c1"`, `"uart2"`, `"spi2"`); активный вариант
(`uart2_m0`/`uart2_m1`) выбирается по живому Device Tree.

Докстринги публичных символов — Google-style (`Args:` / `Returns:` / `Raises:` /
`Example:` / `Hardware:`): из них генерируется справочник (docs/ideas/004). Публичный
API — ровно `__all__`.
"""
from __future__ import annotations

import heapq
import itertools
import queue
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass

from pioneer.sdk import _rpc

__all__ = ["Input", "Output", "Pwm", "Adc", "I2C", "SPI", "UART", "BoardLed", "Event",
           "every", "after", "run", "sleep", "log", "PioneerError", "DHT22", "SHT30", "BH1750", "HardwarePwm"]

PioneerError = _rpc.PioneerError

# Обработчик дольше этого блокирует цикл: другие кнопки и таймеры ждут.
SLOW_HANDLER_S = 0.1
SLOW_WARN_EVERY_S = 10.0


@dataclass(frozen=True)
class Event:
    """Событие фронта на цифровом входе — аргумент обработчиков `Input.on_*`.

    Поля `pin`, `rising`, `time` стабильны. Позже (удалённые команды, docs/ideas/003)
    добавятся `source` и `payload` — без изменения существующих полей.

    Args:
        pin: Пин, на котором случился фронт, например `"J10-2"`.
        rising: True — фронт LOW→HIGH, False — спад HIGH→LOW.
        time: Время фронта по монотонным часам ядра, секунды (сравнимо с `time.monotonic()`).

    Example:
        @button.on_change
        def changed(event):
            log(event.pin, "↑" if event.rising else "↓", f"{event.time:.3f}")
    """
    pin: str
    rising: bool
    time: float

    @property
    def falling(self) -> bool:
        """True, если это спад (HIGH→LOW)."""
        return not self.rising


def log(*args, **kwargs) -> None:
    """Написать строку в лог скрипта (панель «Лог» в редакторе).

    То же, что `print`, но сразу сбрасывает буфер — строка появляется без задержки.

    Args:
        *args: Значения через пробел, как у `print`.
        **kwargs: Аргументы `print` (`sep`, `end`).

    Example:
        log("температура", 21.5)
    """
    print(*args, **kwargs, flush=True)


# ---------------------------------------------------------------- GPIO / ADC
class Output:
    """Цифровой выход (LED, транзисторный ключ, модуль реле с драйвером).

    Пин захватывается эксклюзивно: другой скрипт или проект его не получит. При
    остановке скрипта выход уходит в LOW.

    Args:
        pin: Пин гребёнки с режимом `gpio_out`, например `"J9-7"`.
        initial: Уровень сразу после захвата.

    Raises:
        PioneerError: Пин занят другим владельцем, отдан интерфейсу или драйверу ядра.

    Hardware:
        Логика 3.3 В, ток пина 2–8 мА — только логический сигнал. Реле и моторы —
        только модулем с драйвером, не напрямую.

    Example:
        led = Output("J9-7")
        led.on()
    """

    def __init__(self, pin: str, initial: bool = False):
        self.pin, self._value = pin, bool(initial)
        _rpc.call("acquire", pin=pin, mode="gpio_out", initial=self._value)

    @property
    def value(self) -> bool:
        """Текущий уровень выхода; присваивание выставляет новый."""
        return self._value

    @value.setter
    def value(self, v: bool) -> None:
        self._value = bool(v)
        _rpc.call("write", pin=self.pin, value=self._value)

    def on(self) -> None:
        """Выставить HIGH."""
        self.value = True

    def off(self) -> None:
        """Выставить LOW."""
        self.value = False

    def toggle(self) -> None:
        """Инвертировать уровень."""
        self.value = not self._value


class Input:
    """Цифровой вход с событиями по фронтам и ядерным debounce.

    Вход разделяемый: один пин могут слушать несколько скриптов и проектов.

    Args:
        pin: Пин гребёнки с режимом `gpio_in`, например `"J10-2"`.
        debounce_ms: Подавление дребезга ядром, мс (0 — выключено; для кнопок 20–50).
        active_low: «Нажатие» = спад (кнопка на GND с подтяжкой вверх). Меняет смысл
            `pressed`, `on_press`, `on_release`.

    Raises:
        PioneerError: Пин отдан интерфейсу, драйверу ядра или занят как выход.

    Hardware:
        Подтяжку софтом не поменять (vendor-ядро игнорирует bias): у каждого пина своя
        аппаратная подтяжка (см. страницу «Плата»), при необходимости — внешний резистор.

    Example:
        button = Input("J10-2", debounce_ms=30)

        @button.on_press
        def pressed(event):
            log("нажата")
    """

    def __init__(self, pin: str, debounce_ms: int = 0, active_low: bool = False):
        self.pin, self.active_low = pin, active_low
        self._handlers: list[tuple[str, Callable[[Event], None]]] = []
        _rpc.call("acquire", pin=pin, mode="gpio_in", debounce_ms=int(debounce_ms))
        _loop.inputs.setdefault(pin, []).append(self)

    @property
    def value(self) -> bool:
        """Текущий уровень на пине (True = HIGH)."""
        return bool(_rpc.call("read", pin=self.pin))

    @property
    def pressed(self) -> bool:
        """Нажата ли кнопка с учётом `active_low`."""
        return self.value != self.active_low

    def _on(self, kind: str):
        def deco(fn):
            self._handlers.append((kind, fn))
            return fn
        return deco

    def on_rise(self, fn):
        """Декоратор: вызывать `fn(event)` на фронте LOW→HIGH.

        Args:
            fn: Обработчик, получает `Event`.

        Returns:
            Тот же `fn` (декоратор не оборачивает функцию).
        """
        return self._on("rise")(fn)

    def on_fall(self, fn):
        """Декоратор: вызывать `fn(event)` на спаде HIGH→LOW.

        Args:
            fn: Обработчик, получает `Event`.

        Returns:
            Тот же `fn`.
        """
        return self._on("fall")(fn)

    def on_change(self, fn):
        """Декоратор: вызывать `fn(event)` на любом фронте.

        Args:
            fn: Обработчик, получает `Event`.

        Returns:
            Тот же `fn`.
        """
        return self._on("change")(fn)

    def on_press(self, fn):
        """Декоратор: вызывать `fn(event)` при нажатии (с учётом `active_low`).

        Args:
            fn: Обработчик, получает `Event`.

        Returns:
            Тот же `fn`.
        """
        return self._on("fall" if self.active_low else "rise")(fn)

    def on_release(self, fn):
        """Декоратор: вызывать `fn(event)` при отпускании (с учётом `active_low`).

        Args:
            fn: Обработчик, получает `Event`.

        Returns:
            Тот же `fn`.
        """
        return self._on("rise" if self.active_low else "fall")(fn)

    def _fire(self, ev: Event) -> None:
        for kind, fn in self._handlers:
            if kind == "change" or (kind == "rise") == ev.rising:
                _loop.call(fn, ev)


class Pwm:
    """Программный ШИМ на любом GPIO-пине (яркость LED, медленные нагрузки).

    Args:
        pin: Пин гребёнки с режимом `soft_pwm`, например `"J9-7"`.
        freq: Частота, Гц (до 500; выше — растёт джиттер).
        duty: Заполнение 0…1 (0 — всегда LOW, 1 — всегда HIGH).

    Raises:
        PioneerError: Пин занят или недоступен как GPIO.

    Hardware:
        Soft-PWM: джиттер < 0.1 мс при ≤ 500 Гц. Для вентиляторов 25 кГц и серв нужен
        аппаратный PWM (J9-11/J9-13, фаза 3) или внешний контроллер.

    Example:
        lamp = Pwm("J9-7", freq=200, duty=0.25)
        lamp.duty = 0.8
    """

    def __init__(self, pin: str, freq: float = 100.0, duty: float = 0.0):
        self.pin = pin
        _rpc.call("acquire", pin=pin, mode="soft_pwm")
        self._freq, self._duty = float(freq), float(duty)
        _rpc.call("pwm", pin=pin, freq=self._freq, duty=self._duty)

    @property
    def duty(self) -> float:
        """Заполнение 0…1; присваивание меняет его на лету."""
        return self._duty

    @duty.setter
    def duty(self, d: float) -> None:
        self._duty = float(d)
        _rpc.call("pwm", pin=self.pin, duty=self._duty)

    @property
    def freq(self) -> float:
        """Частота, Гц; присваивание меняет её на лету."""
        return self._freq

    @freq.setter
    def freq(self, f: float) -> None:
        self._freq = float(f)
        _rpc.call("pwm", pin=self.pin, freq=self._freq)


class Adc:
    """Аналоговый вход (АЦП): J9-26 (канал 0) или J10-3 (канал 1).

    Вход разделяемый.

    Args:
        pin: Пин АЦП: `"J9-26"` или `"J10-3"`.

    Raises:
        PioneerError: Пин не аналоговый.

    Hardware:
        Диапазон 0…1.8 В, 10 бит (≈1.76 мВ на шаг). Больше 1.8 В — только через делитель.

    Example:
        pot = Adc("J9-26")
        log(f"{pot.volts:.3f} В")
    """

    def __init__(self, pin: str):
        self.pin = pin
        _rpc.call("acquire", pin=pin, mode="adc")

    @property
    def volts(self) -> float:
        """Напряжение на входе, В (0…1.8)."""
        return float(_rpc.call("read", pin=self.pin))


class BoardLed:
    """Встроенный LED платы (на гребёнку не выведен).

    При остановке скрипта LED возвращается системному триггеру (heartbeat / default-on).

    Args:
        led: `"user"` (синий, по умолчанию мигает heartbeat) или `"power"` (зелёный).

    Raises:
        PioneerError: Нет такого LED или его держит другой владелец.

    Example:
        status = BoardLed("user")
        status.on()
    """

    def __init__(self, led: str = "user"):
        self._h = _rpc.call("led_open", led=led)
        self._value = False

    @property
    def value(self) -> bool:
        """Включён ли LED; присваивание включает/выключает (снимает триггер ядра)."""
        return self._value

    @value.setter
    def value(self, v: bool) -> None:
        self._value = bool(v)
        _rpc.call("led", h=self._h, on=self._value)

    def on(self) -> None:
        """Включить."""
        self.value = True

    def off(self) -> None:
        """Выключить."""
        self.value = False

    def toggle(self) -> None:
        """Переключить."""
        self.value = not self._value

    def trigger(self, name: str) -> None:
        """Отдать LED триггеру ядра.

        Args:
            name: Имя триггера: `"heartbeat"`, `"timer"`, `"default-on"`, `"mmc0"`, `"none"`…
        """
        _rpc.call("led", h=self._h, trigger=name)


# ---------------------------------------------------------------- шины
class I2C:
    """Устройство на шине I2C по одному адресу.

    Шина общая (несколько устройств и скриптов), адрес — эксклюзивный.

    Args:
        interface: Интерфейс из описания платы: `"i2c1"` (J9-3 SDA, J9-5 SCL),
            `"i2c3"` (вариант m0/m1 выбирается по включённому оверлею), `"i2c0"`.
        addr: 7-битный адрес устройства, 0x03…0x77.

    Raises:
        PioneerError: Интерфейс выключен (текст подскажет оверлей и пины) или адрес
            занят другим владельцем. При обмене — если устройство не ответило (NACK).

    Hardware:
        Нужны подтяжки SDA/SCL к 3.3 В (на модулях обычно есть). На шине без
        подтяжек `i2cdetect` видит «все адреса».

    Example:
        bme = I2C("i2c1", 0x76)
        chip_id = bme.read_reg(0xD0)[0]   # BME280 → 0x60
    """

    def __init__(self, interface: str, addr: int):
        self.interface, self.addr = interface, addr
        self._h = _rpc.call("i2c_open", iface=interface, addr=addr)

    def write(self, data: bytes) -> None:
        """Записать байты одной транзакцией.

        Args:
            data: Байты для записи.

        Raises:
            PioneerError: Устройство не ответило (NACK).
        """
        _rpc.call("i2c", h=self._h, kind="write", data=bytes(data).hex())

    def read(self, n: int) -> bytes:
        """Прочитать n байт.

        Args:
            n: Сколько байт прочитать.

        Returns:
            Прочитанные байты.

        Raises:
            PioneerError: Устройство не ответило (NACK).
        """
        return bytes.fromhex(_rpc.call("i2c", h=self._h, kind="read", n=n))

    def write_read(self, data: bytes, n: int) -> bytes:
        """Записать и сразу прочитать (repeated start, без STOP между ними).

        Args:
            data: Байты для записи (обычно номер регистра).
            n: Сколько байт прочитать.

        Returns:
            Прочитанные байты.

        Raises:
            PioneerError: Устройство не ответило (NACK).
        """
        return bytes.fromhex(_rpc.call("i2c", h=self._h, kind="write_read", data=bytes(data).hex(), n=n))

    def read_reg(self, reg: int, n: int = 1) -> bytes:
        """Прочитать n байт, начиная с регистра reg.

        Args:
            reg: Номер регистра, 0…255.
            n: Сколько байт прочитать.

        Returns:
            Прочитанные байты.

        Raises:
            PioneerError: Устройство не ответило (NACK).
        """
        return self.write_read(bytes([reg]), n)

    def write_reg(self, reg: int, data: bytes | int) -> None:
        """Записать в регистр reg один байт (int) или несколько (bytes).

        Args:
            reg: Номер регистра, 0…255.
            data: Значение 0…255 или байты.

        Raises:
            PioneerError: Устройство не ответило (NACK).
        """
        self.write(bytes([reg]) + (bytes([data]) if isinstance(data, int) else bytes(data)))


class SPI:
    """Устройство на шине SPI (spidev), эксклюзивно по паре интерфейс + CS.

    Args:
        interface: Интерфейс из описания платы: `"spi1"` или `"spi2"` (по умолчанию
            выключены — нужен оверлей `rk3308-spiN-spidev`).
        cs: Номер chip select (на гребёнку выведен CS0).
        mode: Режим SPI 0…3 (CPOL/CPHA).
        speed_hz: Частота тактирования, Гц.

    Raises:
        PioneerError: Интерфейс выключен, CS не выведен или занят.

    Hardware:
        SPI1 делит пины с UART3, SPI2 — с UART1/UART2/I2C0: включение требует их выключить.

    Example:
        flash = SPI("spi2", cs=0, speed_hz=8_000_000)
        jedec = flash.xfer(b"\\x9f\\x00\\x00\\x00")[1:]
    """

    def __init__(self, interface: str, cs: int = 0, mode: int = 0, speed_hz: int = 1_000_000):
        self.interface, self.cs = interface, cs
        self._h = _rpc.call("spi_open", iface=interface, cs=cs, mode=mode, speed_hz=speed_hz)

    def xfer(self, data: bytes) -> bytes:
        """Полнодуплексный обмен: отправить data и вернуть столько же принятых байт.

        Args:
            data: Байты для отправки (MOSI).

        Returns:
            Принятые байты (MISO), той же длины.
        """
        return bytes.fromhex(_rpc.call("spi_xfer", h=self._h, data=bytes(data).hex()))


class HardwarePwm:
    """Аппаратный PWM через ядро Linux, частота 1…1000000 Гц, duty 0…1.

    Args:
        interface: pwm2 (J9-11) или pwm3 (J9-13); нужен DT-оверлей и reboot.
        freq: Частота в герцах.
        duty: Доля активного периода.

    Hardware:
        Конфликтует с I2C3 m0. Только логический сигнал 3.3 В; после освобождения
        канала уровень не гарантируется — для нагрузки нужен внешний safe-state.
    """
    def __init__(self, interface: str, freq: float = 100, duty: float = 0):
        from pioneer.hal.hardware_pwm import settings
        settings(freq, duty)
        self._h = _rpc.call("hardware_pwm_open", iface=interface)
        self.set(freq=freq, duty=duty)

    def set(self, *, freq: float | None = None, duty: float | None = None):
        """Изменить частоту и/или заполнение без смены владельца канала.

        Args:
            freq: Новая частота, Гц; None — оставить прежнюю.
            duty: Новое заполнение 0…1; None — оставить прежнее.
        """
        _rpc.call("hardware_pwm_set", h=self._h, freq=freq, duty=duty)


class UART:
    """UART: общий RX с независимыми буферами, эксклюзивный TX.

    Args:
        interface: Интерфейс из описания платы: `"uart1"` (J9-24 TX, J9-23 RX),
            `"uart2"`, `"uart3"`, `"uart4"`. Вариант m0/m1 выбирается по включённому оверлею.
            `"uart0"` — системная консоль, недоступен.
        baud: Скорость, бод; должна совпадать у всех владельцев.
        direction: both — эксклюзивный порт; rx — только общий приём, tx — только запись.

    Raises:
        PioneerError: Интерфейс выключен, порт занят другим владельцем или процессом
            (например, `serial-getty@ttyS2` — текст подскажет, что остановить).

    Hardware:
        Уровни 3.3 В. RS-232 — через преобразователь (MAX3232), RS-485 — через модуль,
        направление DE переключает ядро (оверлей с `rts-gpios`).

    Example:
        port = UART("uart3", baud=115200)
        port.write("AT\\r\\n")
        log(port.read(64, timeout=0.5))
    """

    def __init__(self, interface: str, baud: int = 9600, *, direction: str = "both"):
        self.interface, self.baud = interface, baud
        self._h = _rpc.call("uart_open", iface=interface, baud=baud, direction=direction)

    def write(self, data: bytes | str) -> None:
        """Отправить данные.

        Args:
            data: Байты или строка (кодируется в UTF-8).
        """
        data = data.encode() if isinstance(data, str) else bytes(data)
        _rpc.call("uart_write", h=self._h, data=data.hex())

    def read(self, max_bytes: int = 4096, timeout: float = 0.0) -> bytes:
        """Прочитать то, что пришло.

        Args:
            max_bytes: Не больше стольких байт.
            timeout: Сколько ждать первых данных, секунды (0 — не ждать).

        Returns:
            Принятые байты (пусто, если ничего не пришло).
        """
        return bytes.fromhex(_rpc.call("uart_read", h=self._h, max=max_bytes, timeout=timeout))


# ---------------------------------------------------------------- цикл событий
class _Loop:
    def __init__(self):
        self.inputs: dict[str, list[Input]] = {}
        self.timers: list[tuple[float, int, Callable[[], None], float | None]] = []
        self._seq = itertools.count()
        self._slow_warned: dict[object, float] = {}
        self.running = False

    @property
    def has_work(self) -> bool:
        return bool(self.timers) or any(i._handlers for ins in self.inputs.values() for i in ins)

    def add_timer(self, delay: float, fn: Callable[[], None], interval: float | None) -> None:
        heapq.heappush(self.timers, (time.monotonic() + delay, next(self._seq), fn, interval))

    def call(self, fn, *args) -> None:
        # Ошибка в обработчике не роняет скрипт (spec 3.4): печатаем и живём дальше.
        t0 = time.monotonic()
        try:
            fn(*args)
        except PioneerError as e:
            print(f"ошибка: {e}", file=sys.stderr, flush=True)
        except Exception:
            traceback.print_exc()
            sys.stderr.flush()
        self._check_slow(fn, time.monotonic() - t0)

    def _check_slow(self, fn, took: float) -> None:
        """Обработчик дольше SLOW_HANDLER_S держит весь цикл — предупреждаем (не чаще раза в 10 с)."""
        if took <= SLOW_HANDLER_S:
            return
        now = time.monotonic()
        if now - self._slow_warned.get(fn, -SLOW_WARN_EVERY_S) < SLOW_WARN_EVERY_S:
            return
        self._slow_warned[fn] = now
        name = getattr(fn, "__name__", repr(fn))
        print(f"предупреждение: обработчик {name}() выполнялся {took:.2f} с и блокировал цикл — "
              f"другие кнопки и таймеры ждали. Вместо sleep() используйте after()",
              file=sys.stderr, flush=True)

    def run(self) -> None:
        self.running = True
        while True:
            timeout = None
            if self.timers:
                timeout = max(0.0, self.timers[0][0] - time.monotonic())
            try:
                msg = _rpc.events().get(timeout=timeout)
            except queue.Empty:
                msg = None
            if msg is not None:
                if msg.get("ev") == "stop":
                    return
                if msg.get("ev") == "edge":
                    ev = Event(msg["pin"], msg["rising"], msg["ts"] / 1e9)
                    for inp in self.inputs.get(ev.pin, []):
                        inp._fire(ev)
            now = time.monotonic()
            while self.timers and self.timers[0][0] <= now:
                due, _, fn, interval = heapq.heappop(self.timers)
                self.call(fn)
                if interval is not None:
                    # без накопления дрейфа; если сильно отстали — от текущего момента
                    nxt = due + interval
                    heapq.heappush(self.timers, (nxt if nxt > now else now + interval,
                                                 next(self._seq), fn, interval))


_loop = _Loop()


def every(seconds: float):
    """Декоратор: вызывать функцию периодически.

    Период держится без накопления дрейфа; если цикл сильно отстал (долгий обработчик),
    следующий вызов — через `seconds` от текущего момента, пропущенные не догоняются.

    Args:
        seconds: Период, секунды (> 0).

    Returns:
        Декоратор; функция вызывается без аргументов.

    Example:
        @every(1.0)
        def blink():
            led.toggle()
    """
    def deco(fn):
        _loop.add_timer(seconds, fn, float(seconds))
        return fn
    return deco


def after(seconds: float, fn: Callable[[], None]) -> None:
    """Однократно вызвать функцию через заданное время, не блокируя цикл.

    Используйте вместо `sleep()` внутри обработчиков.

    Args:
        seconds: Задержка, секунды.
        fn: Функция без аргументов.

    Example:
        @button.on_press
        def pulse(event):
            relay.on()
            after(5, relay.off)
    """
    _loop.add_timer(seconds, fn, None)


def run() -> None:
    """Запустить цикл событий (не возвращается до остановки скрипта).

    Обычно не нужен: если после выполнения скрипта есть обработчики или таймеры,
    цикл запускается сам. Нужен, чтобы держать скрипт живым без обработчиков
    (например, только захваченный выход).
    """
    _loop.run()


def sleep(seconds: float) -> None:
    """Пауза.

    В основном коде скрипта — безопасно. **В обработчике** блокирует весь цикл:
    другие кнопки и таймеры ждут (в лог придёт предупреждение) — там используйте `after()`.

    Args:
        seconds: Длительность, секунды.
    """
    time.sleep(seconds)


class DHT22:
    """DHT22 / AM2302 через драйвер ядра dht11 и IIO, настроенный в админке.

    Returns:
        read(): Словарь ok, temperature (°C), humidity (% RH), time; при ошибке ok=False, error.

    Raises:
        PioneerError: Драйвер/датчик не подключён; настройте Настройки → Драйверы.

    Hardware:
        VCC 3.3 В, общий GND, DATA с подтяжкой к 3.3 В. Кэш в core не допускает
        чтение чаще одного раза в 2 с даже при нескольких потребителях.

    Example:
        sensor = DHT22()
        log(sensor.read())
    """

    def read(self) -> dict:
        """Прочитать температуру и влажность без управления пином из Python."""
        return _rpc.call("dht22_read")


class SHT30:
    """Температура и влажность SHT30/SHT31 (SHT3x-DIS) по I2C.

    Args:
        interface: Включённая шина, например "i2c1".
        addr: 0x44 или 0x45.

    Hardware:
        Питание и подтяжки SDA/SCL — 3.3 В, общий GND.

    Example:
        sensor = SHT30("i2c1")
        log(sensor.read())
    """
    def __init__(self, interface: str = "i2c1", addr: int = 0x44):
        if addr not in (0x44, 0x45):
            raise PioneerError("SHT30: адрес 0x44 или 0x45")
        self._io = I2C(interface, addr)

    def read(self) -> dict:
        """Измерить °C и % RH с проверкой CRC, около 20 мс.

        Returns:
            Словарь temperature, humidity. Ошибка шины/CRC вызывает PioneerError.
        """
        from pioneer.drivers.sensors import sht30
        try:
            return sht30(self._io)
        except ValueError as e:
            raise PioneerError(str(e)) from None


class BH1750:
    """Освещённость BH1750 по I2C, одноразовое измерение высокого разрешения.

    Args:
        interface: Включённая шина, например "i2c1".
        addr: 0x23 или 0x5C.

    Hardware:
        Питание и подтяжки SDA/SCL — 3.3 В, общий GND.

    Example:
        sensor = BH1750("i2c1")
        log(sensor.read()["lux"])
    """
    def __init__(self, interface: str = "i2c1", addr: int = 0x23):
        if addr not in (0x23, 0x5c):
            raise PioneerError("BH1750: адрес 0x23 или 0x5C")
        self._io = I2C(interface, addr)

    def read(self) -> dict:
        """Измерить освещённость в люксах, ожидание до 180 мс.

        Returns:
            Словарь lux. Ошибка шины вызывает PioneerError. Не вызывайте
            из обработчика кнопки: измерение блокирует цикл скрипта на 180 мс.
        """
        from pioneer.drivers.sensors import bh1750
        try:
            return bh1750(self._io)
        except ValueError as e:
            raise PioneerError(str(e)) from None
