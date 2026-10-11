"""GPIO through Pioneer ownership; Linux pull changes require a DT plan."""

from enum import Enum
import warnings
from pioneer.compat.circuitpython import current


class Direction(Enum):
    INPUT = 0
    OUTPUT = 1


class Pull(Enum):
    UP = "up"
    DOWN = "down"


class DriveMode(Enum):
    PUSH_PULL = 0
    OPEN_DRAIN = 1


class DigitalInOut:
    def __init__(self, pin):
        self._context, self._pin, self._h = current(), pin, None
        self._direction, self._pull = None, None
        self._drive_mode = DriveMode.PUSH_PULL
        self._output_value = False
        self.switch_to_input()

    def _call(self, action, **args):
        if self._h is None:
            raise RuntimeError("DigitalInOut deinit")
        return self._context.call("circuitpython", action=action, h=self._h, **args)

    def _switch(self, direction, value=False):
        if direction not in (Direction.INPUT, Direction.OUTPUT):
            raise ValueError("Direction.INPUT/OUTPUT")
        mode = (
            "gpio_in"
            if direction == Direction.INPUT or (self._drive_mode == DriveMode.OPEN_DRAIN and value)
            else "gpio_out"
        )
        if self._h is not None:
            self._call("mode", kind=mode, value=bool(value))
        else:
            self._h = self._context.call(
                "circuitpython", action="open", pin=self._pin.id, kind=mode, value=bool(value)
            )
        self._direction = direction
        self._output_value = bool(value)

    @property
    def direction(self):
        return self._direction

    @direction.setter
    def direction(self, value):
        self._switch(value)

    @property
    def value(self):
        return bool(self._call("read"))

    @value.setter
    def value(self, value):
        if self.direction != Direction.OUTPUT:
            raise AttributeError("value доступен для записи только в OUTPUT")
        if self.drive_mode == DriveMode.OPEN_DRAIN:
            self._switch(Direction.OUTPUT, bool(value))
        else:
            self._call("write", value=bool(value))
            self._output_value = bool(value)

    @property
    def pull(self):
        return self._pull

    @pull.setter
    def pull(self, value):
        if self.direction != Direction.INPUT or value not in (None, Pull.UP, Pull.DOWN):
            raise ValueError("Pull доступен только для INPUT")
        result = self._call("pull", pull=value.value if value else "none")
        if result:
            warnings.warn(result, RuntimeWarning, stacklevel=2)
        self._pull = value

    @property
    def drive_mode(self):
        return self._drive_mode

    @drive_mode.setter
    def drive_mode(self, mode):
        if mode not in (DriveMode.PUSH_PULL, DriveMode.OPEN_DRAIN):
            raise ValueError("DriveMode.PUSH_PULL/OPEN_DRAIN")
        previous, self._drive_mode = self._drive_mode, mode
        try:
            if self._direction == Direction.OUTPUT:
                self._switch(Direction.OUTPUT, self._output_value)
        except Exception:
            self._drive_mode = previous
            raise

    def switch_to_input(self, pull=None):
        self._switch(Direction.INPUT)
        self.pull = pull

    def switch_to_output(self, value=False, drive_mode=DriveMode.PUSH_PULL):
        self.drive_mode = drive_mode
        self._switch(Direction.OUTPUT, value)

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
