"""Встроенные ноды каталога v1 (spec 3.7, кроме сети и камеры — фаза 2).

Импорт модулей регистрирует типы в pioneer.engine.node.BUILTIN.
"""
from pioneer.nodes.builtin import hw, logic, media, net, sensors, timing, ui  # noqa: F401
