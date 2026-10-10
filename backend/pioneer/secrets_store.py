"""Секреты (spec 6): data/secrets.json с правами 600. В проектах — только ссылки
`{{secret:имя}}`; значения наружу не отдаются (API показывает только имена)."""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from pioneer.engine.node import NodeError

REF = re.compile(r"\{\{\s*secret:([A-Za-z0-9_.-]+)\s*\}\}")
NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class SecretStore:
    def __init__(self, data_dir: Path):
        self.file = data_dir / "secrets.json"
        self._lock = threading.Lock()

    def _load(self) -> dict[str, str]:
        try:
            return json.loads(self.file.read_text())
        except (OSError, ValueError):
            return {}

    def names(self) -> list[str]:
        return sorted(self._load())

    def update(self, values: dict[str, str | None]) -> list[str]:
        """Задать/удалить секреты (None — удалить). Возвращает имена."""
        with self._lock:
            data = self._load()
            for k, v in values.items():
                if not NAME.match(k):
                    raise ValueError(f"имя секрета {k!r}: латиница, цифры, _ . -")
                if v is None:
                    data.pop(k, None)
                else:
                    data[k] = str(v)
            tmp = self.file.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.file)
            os.chmod(self.file, 0o600)
            return sorted(data)

    def resolve(self, text: str) -> str:
        if "{{" not in text:
            return text
        data = self._load()

        def sub(m: re.Match) -> str:
            if m[1] not in data:
                raise NodeError(f"нет секрета «{m[1]}» — добавьте на странице «Секреты»")
            return data[m[1]]
        return REF.sub(sub, text)
