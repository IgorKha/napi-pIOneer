"""Пароль и сессии (spec 6). Один пользователь, пароль задаётся при установке.

- пароль: scrypt из stdlib (без зависимостей на плате), data/auth.json, права 600;
- сессии: случайный токен в cookie (HttpOnly, SameSite=Strict), на сервере —
  data/sessions.json (переживают перезапуск сервиса), скользящий срок SESSION_TTL;
- перебор: после MAX_FAILS неудач с адреса за FAIL_WINDOW — отказ до конца окна.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

SESSION_TTL = 30 * 24 * 3600
MAX_FAILS = 5
FAIL_WINDOW = 300
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}  # ~16 МБ и ~0.1–0.3 с на A35
MIN_PASSWORD = 6


def _write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _hash(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024, dklen=32)


class Auth:
    def __init__(self, data_dir: Path):
        self.pw_file = data_dir / "auth.json"
        self.sess_file = data_dir / "sessions.json"
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}
        try:
            self._sessions: dict[str, float] = json.loads(self.sess_file.read_text())
        except (OSError, ValueError):
            self._sessions = {}

    # ---- пароль
    @property
    def password_set(self) -> bool:
        return self.pw_file.exists()

    def set_password(self, password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"пароль короче {MIN_PASSWORD} символов")
        salt = secrets.token_bytes(16)
        _write_private(self.pw_file, {"scheme": "scrypt", **_SCRYPT, "salt": salt.hex(),
                                      "hash": _hash(password, salt, **_SCRYPT).hex()})
        with self._lock:  # смена пароля разлогинивает всех
            self._sessions.clear()
            self._save_sessions()

    def check_password(self, password: str) -> bool:
        try:
            rec = json.loads(self.pw_file.read_text())
        except (OSError, ValueError):
            return False
        got = _hash(password, bytes.fromhex(rec["salt"]), rec["n"], rec["r"], rec["p"])
        return hmac.compare_digest(got, bytes.fromhex(rec["hash"]))

    # ---- перебор
    def throttled(self, client: str) -> bool:
        now = time.time()
        with self._lock:
            fails = [t for t in self._fails.get(client, []) if now - t < FAIL_WINDOW]
            self._fails[client] = fails
            return len(fails) >= MAX_FAILS

    def record_fail(self, client: str) -> None:
        with self._lock:
            self._fails.setdefault(client, []).append(time.time())

    # ---- сессии
    def create_session(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[self._key(token)] = time.time() + SESSION_TTL
            self._save_sessions()
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        key, now = self._key(token), time.time()
        with self._lock:
            exp = self._sessions.get(key)
            if exp is None or exp < now:
                self._sessions.pop(key, None)
                return False
            # продлеваем, но пишем на диск не чаще раза в сутки
            if exp - now < SESSION_TTL - 86400:
                self._sessions[key] = now + SESSION_TTL
                self._save_sessions()
            return True

    def drop_session(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(self._key(token), None)
                self._save_sessions()

    @staticmethod
    def _key(token: str) -> str:
        # на диске — хэши токенов, не сами токены
        return hashlib.sha256(token.encode()).hexdigest()

    def _save_sessions(self) -> None:
        now = time.time()
        self._sessions = {k: v for k, v in self._sessions.items() if v > now}
        _write_private(self.sess_file, self._sessions)
