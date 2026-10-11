"""Prefer enabled hardware PWM, otherwise bounded software PWM."""

import warnings
from pioneer.compat.circuitpython import current, rpc


class PWMOut:
    def __init__(self, pin, *, duty_cycle=0, frequency=500, variable_frequency=False):
        interfaces = rpc("describe")["interfaces"]
        interface = next(
            (i for i in interfaces if i["kind"] == "pwm" and i["enabled"] and pin.id in i["pins"]),
            None,
        )
        self._soft = interface is None
        self.variable_frequency = variable_frequency
        self._frequency, self._duty = int(frequency), int(duty_cycle)
        self._validate()
        self._context = current()
        self._h = self._context.call(
            "circuitpython",
            action="open",
            **(
                {"kind": "pwm", "interface": interface["id"]}
                if interface
                else {"kind": "soft_pwm", "pin": pin.id}
            ),
        )
        try:
            self._set()
        except Exception:
            self.deinit()
            raise
        if self._soft:
            warnings.warn(
                "Soft-PWM ≤500 Гц; для серв используйте аппаратный PWM/PCA9685",
                RuntimeWarning,
                stacklevel=2,
            )

    def _validate(self):
        if not 0 <= self._duty <= 65535 or not 1 <= self._frequency <= (
            500 if self._soft else 1000000
        ):
            raise ValueError("PWM duty_cycle: 0…65535; frequency вне доступного диапазона")

    def _set(self):
        if self._h is None:
            raise RuntimeError("PWMOut deinit")
        self._context.call(
            "circuitpython",
            action="pwm",
            h=self._h,
            frequency=self._frequency,
            duty=self._duty / 65535,
        )

    @property
    def duty_cycle(self):
        return self._duty

    @duty_cycle.setter
    def duty_cycle(self, value):
        previous = self._duty
        self._duty = int(value)
        try:
            self._validate()
            self._set()
        except Exception:
            self._duty = previous
            raise

    @property
    def frequency(self):
        return self._frequency

    @frequency.setter
    def frequency(self, value):
        if not self.variable_frequency:
            raise AttributeError("variable_frequency=False")
        previous = self._frequency
        self._frequency = int(value)
        try:
            self._validate()
            self._set()
        except Exception:
            self._frequency = previous
            raise

    def deinit(self):
        if self._h is not None:
            self._context.call("circuitpython", action="close", h=self._h)
            self._h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.deinit()
