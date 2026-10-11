"""Linux PWM sysfs, resolved by device-tree identity rather than unstable chip index."""

import errno
import math
import threading
import time
from pathlib import Path
from pioneer.hal.base import PwmLine, LineBusy


def settings(freq, duty):
    if not math.isfinite(freq) or not 1 <= freq <= 1_000_000:
        raise ValueError("частота аппаратного PWM 1…1000000 Гц")
    if not math.isfinite(duty) or not 0 <= duty <= 1:
        raise ValueError("заполнение PWM 0…1")
    period = round(1_000_000_000 / freq)
    return period, round(period * duty)


class HardwarePwm(PwmLine):
    def __init__(self, chip: Path):
        self.chip, self.path = chip, chip / "pwm0"
        self.lock = threading.RLock()
        self.freq, self.duty = 100.0, 0.0
        self.closed = False
        if self.path.exists():
            raise LineBusy(errno.EBUSY, "канал PWM уже экспортирован другим владельцем")
        (chip / "export").write_text("0")
        try:
            deadline = time.monotonic() + 0.5
            while not self.path.exists():
                if time.monotonic() >= deadline:
                    raise OSError("PWM export не создал канал")
                time.sleep(0.005)
            # Freshly exported Rockchip channels have period=0; even disable is
            # rejected until a valid disabled PWM state has been established.
            if not int((self.path / "period").read_text()):
                self._write("period", settings(self.freq, 0)[0])
            self._write("enable", 0)
            self._write("duty_cycle", 0)
            self._write("polarity", "normal")
            self.set(self.freq, self.duty)
        except BaseException:
            (chip / "unexport").write_text("0")
            raise

    def _write(self, key, value):
        (self.path / key).write_text(str(value))

    def set(self, freq=None, duty=None):
        with self.lock:
            if self.closed:
                raise OSError("PWM закрыт")
            f, d = self.freq if freq is None else freq, self.duty if duty is None else duty
            period, active = settings(f, d)
            # Zero duty before shortening period; sysfs rejects duty > period.
            if f != self.freq or not int((self.path / "period").read_text()):
                self._write("enable", 0)
                self._write("duty_cycle", 0)
                self._write("period", period)
            self._write("duty_cycle", active)
            self._write("enable", 1)
            self.freq, self.duty = f, d

    def close(self, safe_value=False):
        with self.lock:
            if self.closed:
                return
            try:
                self.set(duty=1.0 if safe_value else 0.0)
                self._write("enable", 0)
            finally:
                (self.chip / "unexport").write_text("0")
                self.closed = True


def open_pwm(node: str, root=Path("/sys/class/pwm")):
    for chip in root.glob("pwmchip*"):
        if (chip / "device/of_node").resolve().name == Path(node).name:
            return HardwarePwm(chip)
    raise OSError("контроллер PWM отсутствует в sysfs; проверьте оверлей и перезагрузку")
