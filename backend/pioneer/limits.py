"""Таймауты и лимиты worker-процессов: одно место для всех цифр.

Значения складываются так: умолчания из кода ← `[limits]` в config.toml ← раздел
`limits` в data/settings.json (меняется из «Настройки → Система»). Читаются в момент
использования, поэтому новые значения действуют без перезапуска сервиса: таймауты
вызовов — сразу, запуск процесса блоков — со следующего старта проекта.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class Limits:
    block_worker_start_s: float = 15.0
    block_load_s: float = 20.0
    block_start_s: float = 5.0
    block_call_s: float = 1.0
    block_stop_s: float = 2.0
    block_hardware_rpc_s: float = 2.0
    block_schema_s: float = 15.0
    block_preload: bool = True
    script_rpc_s: float = 15.0


# (минимум, максимум, подпись, пояснение) — для проверки и для UI
SPEC: dict[str, tuple[float, float, str, str]] = {
    "block_worker_start_s": (2, 120, "Запуск процесса блоков",
                             "Старт Python, импорт pioneer и предзагрузка файлов блоков проекта."),
    "block_load_s": (1, 300, "Загрузка блока",
                     "Импорт файла блока и его setup(): инициализация железа, например LCD по I2C."),
    "block_start_s": (0.5, 120, "start() блока", "Первый вызов после запуска проекта."),
    "block_call_s": (0.1, 60, "Обработчик блока",
                     "on_change / on_event и таймеры. Если не уложился — процесс блоков проекта перезапускается кнопкой «Применить»."),
    "block_stop_s": (0.2, 60, "stop() блока", "Остановка ноды: вернуть железо в безопасное состояние."),
    "block_hardware_rpc_s": (0.2, 30, "Вызов железа из блока",
                             "Одна операция с пином или шиной из процесса блоков."),
    "block_schema_s": (2, 120, "Проверка блока при сохранении", "Импорт файла в отдельном процессе."),
    "script_rpc_s": (1, 300, "Вызов железа из скрипта", "Одна операция SDK скрипта (кроме чтения UART с таймаутом)."),
}
NAMES = tuple(f.name for f in fields(Limits))


def validate(values: dict) -> dict:
    """Проверить частичный словарь значений; вернуть нормализованный (float/bool)."""
    out = {}
    for key, value in values.items():
        if key not in NAMES:
            raise ValueError(f"неизвестный параметр {key!r}")
        if key == "block_preload":
            if not isinstance(value, bool):
                raise ValueError("block_preload: true или false")
            out[key] = value
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key}: нужно число секунд")
        lo, hi, title, _ = SPEC[key]
        if not lo <= value <= hi:
            raise ValueError(f"{title}: {lo:g}…{hi:g} с")
        out[key] = float(value)
    return out


class LimitsStore:
    def __init__(self, settings_file: Path, config_defaults: dict | None = None):
        self.file = settings_file
        self.base = asdict(Limits()) | validate(config_defaults or {})
        self._lock = threading.Lock()
        self._cached: Limits | None = None

    def _load_file(self) -> dict:
        try:
            return json.loads(self.file.read_text())
        except (OSError, ValueError):
            return {}

    def get(self) -> Limits:
        with self._lock:
            if self._cached is None:
                try:
                    saved = validate(self._load_file().get("limits", {}))
                except ValueError:
                    saved = {}  # испорченный файл не должен мешать запуску проектов
                self._cached = Limits(**(self.base | saved))
            return self._cached

    def overrides(self) -> dict:
        return {k: v for k, v in asdict(self.get()).items() if v != self.base[k]}

    def save(self, values: dict) -> Limits:
        """values: полный или частичный словарь; None у ключа — вернуть умолчание."""
        reset = {k for k, v in values.items() if v is None}
        clean = validate({k: v for k, v in values.items() if v is not None})
        with self._lock:
            data = self._load_file()
            stored = {k: v for k, v in data.get("limits", {}).items() if k in NAMES and k not in reset}
            stored |= clean
            data["limits"] = {k: v for k, v in stored.items() if v != self.base[k]}
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
            os.replace(tmp, self.file)
            self._cached = None
        return self.get()


_store: LimitsStore | None = None


def configure(store: LimitsStore) -> None:
    global _store
    _store = store


def get() -> Limits:
    """Текущие лимиты; без configure() (тесты, утилиты) — умолчания."""
    return _store.get() if _store is not None else Limits()
