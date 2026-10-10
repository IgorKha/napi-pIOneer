# Рецепты

Готовые фрагменты: скопируйте в скрипт или нажмите «Открыть в редакторе» у примера. Строки `# стенд:` подсказывают, что подключить на виртуальном стенде.

## Мигалка

```python run
# стенд: led pin=J9-7
from pioneer.sdk import Output, every

led = Output("J9-7")


@every(0.5)
def blink():
    led.toggle()
```

## Кнопка на GND с подтяжкой вверх

У J10-16 аппаратная подтяжка вверх: кнопка между пином и GND даёт LOW при нажатии. `active_low=True` делает так, что `on_press` срабатывает на нажатие, а не на отпускание.

```python run
# стенд: button pin=J10-16
from pioneer.sdk import Input, log

button = Input("J10-16", debounce_ms=30, active_low=True)


@button.on_press
def pressed(event):
    log("нажата в", round(event.time, 3))
```

## Долгое нажатие

```python run
# стенд: button pin=J10-2
# стенд: led pin=J9-7
from pioneer.sdk import Input, Output, after, log

button = Input("J10-2", debounce_ms=30)
led = Output("J9-7")
state = {"timer": 0}


@button.on_press
def down(event):
    state["timer"] += 1
    token = state["timer"]
    after(1.0, lambda: long_press(token))


@button.on_release
def up(event):
    if state["timer"] > 0:
        state["timer"] += 1   # отменяет ожидающий long_press
        led.toggle()          # короткое нажатие


def long_press(token):
    if token == state["timer"] and button.pressed:
        state["timer"] = 0
        led.off()
        log("долгое нажатие: всё выключено")
```

## АЦП: скользящее среднее и делитель

АЦП меряет 0…1.8 В. Чтобы мерить до 3.3 В, поставьте делитель 10 кОм (сверху) + 12 кОм (снизу): на вход придёт 3.3 × 12 / 22 = 1.8 В.

```python run
# стенд: voltage pin=J9-26 volts=0.9
from collections import deque

from pioneer.sdk import Adc, every, log

adc = Adc("J9-26")
window = deque(maxlen=10)
DIVIDER = 22 / 12   # (R1 + R2) / R2


@every(0.2)
def measure():
    window.append(adc.volts)
    avg = sum(window) / len(window)
    log(f"вход {avg:.3f} В, до делителя {avg * DIVIDER:.2f} В")
```

## Плавный PWM

```python run
# стенд: led pin=J9-12
from pioneer.sdk import Pwm, every

lamp = Pwm("J9-12", freq=200)
state = {"step": 0.05}


@every(0.05)
def fade():
    duty = lamp.duty + state["step"]
    if not 0 <= duty <= 1:
        state["step"] = -state["step"]
        duty = min(1.0, max(0.0, duty))
    lamp.duty = duty
```

## I2C: прочитать ID датчика BME280

BME280 по адресу 0x76 (или 0x77) отвечает на регистр `0xD0` значением `0x60`. Датчик — к J9-3 (SDA) и J9-5 (SCL), питание 3.3 В.

```python run
# стенд: i2c_regs bus=1 addr=0x76
from pioneer.sdk import I2C, PioneerError, log

sensor = I2C("i2c1", 0x76)
try:
    chip_id = sensor.read_reg(0xD0)[0]
    log(f"chip id = 0x{chip_id:02X}", "(BME280)" if chip_id == 0x60 else "")
except PioneerError as e:
    log("датчик не отвечает:", e)
```

## UART: отправить и принять строку

uart3 — J10-13 (TX) и J10-14 (RX). Для проверки без устройства соедините TX и RX перемычкой.

```python run
# стенд: uart_loopback device=/dev/ttyS3
from pioneer.sdk import UART, log

port = UART("uart3", baud=115200)
port.write("hello\n")
log("пришло:", port.read(64, timeout=0.5))
```

## Индикатор на встроенном LED

```python run
from pioneer.sdk import BoardLed, every

status = BoardLed("user")


@every(1.0)
def heartbeat():
    status.toggle()
```
