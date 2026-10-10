"""Реестр типов нод: встроенные (builtin.*), позже — свои блоки (user.*) и модули (mod.*)."""
from __future__ import annotations

from pioneer.engine.node import BUILTIN, NodeRegistry


def registry() -> NodeRegistry:
    import pioneer.nodes.builtin  # noqa: F401 — регистрирует встроенные типы
    return BUILTIN
