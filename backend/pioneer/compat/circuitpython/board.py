"""Napi-C header aliases. The default I2C uses J9-3 SDA and J9-5 SCL."""

from microcontroller import pin
from pioneer.compat.circuitpython import current, SHIMS

globals().update(vars(pin))
board_id = "napi_c"
FONT5X8 = str(SHIMS / "font5x8.bin")
SDA, SCL = pin.J9_3, pin.J9_5
SCK, MOSI, MISO = pin.J9_23, pin.J9_19, pin.J9_21
TX, RX = pin.J9_19, pin.J9_21


def _default(kind, create):
    context = current()
    device = context.defaults.get(kind)
    if device is None or device._h is None:
        device = context.defaults[kind] = create()
    return device


def I2C():
    import busio

    return _default("i2c", lambda: busio.I2C(SCL, SDA))


def SPI():
    import busio

    return _default("spi", lambda: busio.SPI(SCK, MOSI, MISO))


def UART():
    import busio

    return _default("uart", lambda: busio.UART(TX, RX))
