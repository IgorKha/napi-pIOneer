"""Napi-C ADC normalized to CircuitPython's 16-bit range."""

from pioneer.compat.circuitpython import current


class AnalogIn:
    reference_voltage = 1.8

    def __init__(self, pin):
        self._context = current()
        self._h = self._context.call("circuitpython", action="open", kind="adc", pin=pin.id)

    @property
    def value(self):
        if self._h is None:
            raise RuntimeError("AnalogIn deinit")
        value = self._context.call("circuitpython", action="read", h=self._h)
        return round(max(0, min(1, value)) * 65535)

    def deinit(self):
        if self._h is not None:
            self._context.call("circuitpython", action="close", h=self._h)
            self._h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.deinit()
