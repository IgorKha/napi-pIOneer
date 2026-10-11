"""Curated connection templates; other Bundle drivers remain installable."""

PROFILES = {
    "bme280": {
        "title": "BME280: температура, влажность, давление",
        "address": 0x76,
        "model": "bme280",
        "properties": ["temperature", "humidity", "pressure"],
        "import": "from adafruit_bme280 import basic as driver",
        "constructor": "driver.Adafruit_BME280_I2C(i2c, address=address)",
    },
    "sht4x": {
        "title": "SHT4x: температура и влажность",
        "address": 0x44,
        "model": "sht4x",
        "properties": ["temperature", "relative_humidity"],
        "import": "import adafruit_sht4x as driver",
        "constructor": "driver.SHT4x(i2c, address=address)",
    },
    "ssd1306": {
        "title": "SSD1306: OLED 128×64",
        "address": 0x3C,
        "model": "ssd1306",
        "properties": [],
        "import": "import adafruit_ssd1306 as driver",
        "constructor": "driver.SSD1306_I2C(128, 64, i2c, addr=address)",
    },
    "pca9685": {
        "title": "PCA9685: 16 каналов PWM",
        "address": 0x40,
        "model": "pca9685",
        "properties": [],
        "import": "import adafruit_pca9685 as driver",
        "constructor": "driver.PCA9685(i2c, address=address)",
    },
    "lcd": {
        "title": "LCD1602 / LCD2004: PCF8574",
        "address": 0x27,
        "model": None,
        "properties": [],
        "import": "import adafruit_pcf8574\nimport adafruit_character_lcd.character_lcd as characterlcd",
        "constructor": "connect_lcd(i2c, address)",
    },
}


def package(chip):
    return "adafruit-circuitpython-" + ("pcf8574" if chip == "lcd" else chip)


def snippet(chip, iface, address, columns=20, rows=4):
    from pioneer.board import load_board
    from pioneer.config import REPO_ROOT

    board = load_board(REPO_ROOT / "boards/napi-c.yaml")
    bus = board.interfaces[iface]
    if bus.kind != "i2c" or not 8 <= address <= 0x77:
        raise ValueError("Шаблон требует I2C и адрес 0x08…0x77")
    pins = {role: pin.replace("-", "_") for pin, role in bus.pins.items()}
    profile = PROFILES[chip]
    code = "import board\nimport busio\n" + profile["import"] + "\n"
    if chip == "lcd":
        if columns not in (16, 20) or rows not in (2, 4):
            raise ValueError("LCD: columns 16/20, rows 2/4")
        code += f"""
import time

class BackpackLCD(characterlcd.Character_LCD_Mono):
    # Adafruit assumes 20x4 offsets; a 16x4 backpack needs 0x10/0x50.
    def cursor_position(self, column, row):
        column = max(0, min(column, self.columns - 1))
        row = max(0, min(row, self.lines - 1))
        offset = (0, 0x40, self.columns, 0x40 + self.columns)[row]
        self._write8(0x80 | (column + offset))
        self.row, self.column = row, column

def connect_lcd(i2c, address):
    expander = adafruit_pcf8574.PCF8574(i2c, address=address)
    expander.write_gpio(0x08)
    time.sleep(0.1)
    enable = expander.get_pin(2)
    # Slow startup worked on the 3.3 V LCD tested on Napi-C.
    for value in (0x30, 0x30, 0x30, 0x20):
        expander.write_gpio(value | 0x08)
        expander.write_gpio(value | 0x0c)
        time.sleep(0.001)
        expander.write_gpio(value | 0x08)
        time.sleep(0.01)
    lcd = BackpackLCD(
        expander.get_pin(0), enable, *(expander.get_pin(n) for n in range(4, 8)),
        {columns}, {rows})
    return lcd
"""
    code += f"\ni2c = busio.I2C(board.{pins['SCL']}, board.{pins['SDA']})\naddress = {address:#04x}\ndevice = {profile['constructor']}\n"
    if chip == "lcd":
        message = "Napi-C Pioneer\\nAdafruit PCF8574"
        if rows == 4:
            message += f"\\nCircuitPython\\n{columns}x4 RPC OK"
        code += f'device.message = "{message}"\n'
    elif chip == "ssd1306":
        code += 'device.fill(0)\ndevice.text("Napi-C Pioneer", 0, 0, 1, font_name=board.FONT5X8)\ndevice.show()\n'
    elif chip == "pca9685":
        code += "device.frequency = 50\ndevice.channels[0].duty_cycle = 32768\n"
    else:
        code += "print(" + ", ".join("device." + p for p in profile["properties"]) + ")\n"
    return code


def block_template(chip, iface, address):
    if chip not in ("bme280", "sht4x", "pca9685"):
        raise ValueError("Генератор блоков: BME280, SHT4x, PCA9685")
    profile = PROFILES[chip]
    # Keep imports at module scope and hardware allocation inside setup.
    lines = [
        "from pioneer.block import node, Node, Param, In, Out, Number",
        "import board",
        "import busio",
        profile["import"],
        "",
        f'@node(id="{chip}", title="{chip.upper()} (Adafruit)", category="Adafruit")',
        "class DeviceNode(Node):",
        f'    interface = Param(str, "{iface}", title="I2C")',
        f'    address = Param(int, {address}, title="Адрес")',
        '    period = Param(float, 2.0, title="Период, с")',
    ]
    for prop in profile["properties"]:
        lines.append(f'    {prop} = Out(Number, title="{prop}")')
    if chip == "pca9685":
        lines += [
            '    duty = In(Number, 0, title="Duty 0…65535")',
            '    channel = Param(int, 0, title="Канал")',
            '    frequency = Param(int, 50, title="Частота, Гц")',
        ]
    lines += [
        "",
        "    def setup(self):",
        "        from pioneer.compat.circuitpython import rpc",
        '        bus = next(i for i in rpc("describe")["interfaces"] if i["id"] == self.interface)',
        '        pins = {role: getattr(board, pin.replace("-", "_")) for pin, role in bus["pins"].items()}',
        '        self.i2c = busio.I2C(pins["SCL"], pins["SDA"])',
        f"        self.device = {profile['constructor'].replace('(i2c,', '(self.i2c,').replace('address=address', 'address=self.address')}",
    ]
    if chip == "pca9685":
        lines += [
            "        self.device.frequency = self.frequency",
            "",
            "    def on_change(self, changed):",
            "        if not 0 <= self.channel <= 15 or not 0 <= self.duty <= 65535:",
            '            raise ValueError("channel 0…15, duty 0…65535")',
            "        self.device.channels[self.channel].duty_cycle = int(self.duty)",
        ]
    else:
        lines += [
            "",
            "    def start(self):",
            '        if self.period < 0.1: raise ValueError("Период >=0.1 с")',
            "        self.poll()",
            "        self.every(self.period, self.poll)",
            "",
            "    def poll(self):",
        ]
        for prop in profile["properties"]:
            lines.append(f"        self.{prop}.set(self.device.{prop})")
    lines += ["", "    def stop(self):", "        if hasattr(self, 'i2c'): self.i2c.deinit()", ""]
    return "\n".join(lines)
