"""uEnv changes require a fresh preview hash, known .dtbo and idle affected resources."""

from dataclasses import asdict
import difflib
import hashlib
import os
import re
import threading
import time

from pioneer.board import load_board, parse_uenv_overlays
from pioneer.uenv import UEnv, UEnvError


class OverlayError(ValueError):
    def __init__(self, message, code=422):
        super().__init__(message)
        self.code = code


class OverlayManager:
    def __init__(self, cfg, board, hw):
        self.cfg, self.board, self.hw = cfg, board, hw
        self.uenv = UEnv(cfg)
        self.lock = self.uenv.lock

    def preview(self, enable, disable):
        path = self.cfg.uenv_file
        if not path.exists():
            return {
                **asdict(self.board.plan_interfaces(enable, disable)),
                "writable": False,
                "hash": None,
                "diff": "",
                "missing": [],
                "claims": {},
            }
        if path.is_symlink() or path.stat().st_size > 64 * 1024:
            raise OverlayError("uEnv должен быть обычным файлом до 64 КБ")
        text = path.read_text()
        if len(re.findall(r"^overlays=", text, re.M)) != 1:
            raise OverlayError("в uEnv должна быть одна строка overlays=")
        # Plan from next-boot configuration; live DT may differ after an earlier edit.
        pending = load_board(self.cfg.board_file, parse_uenv_overlays(text))
        plan = pending.plan_interfaces(enable, disable)
        updated = re.sub(r"^overlays=.*$", "overlays=" + " ".join(plan.overlays), text, flags=re.M)
        missing = [
            o for o in plan.overlays if not (self.cfg.overlays_dir / (o + ".dtbo")).is_file()
        ]
        pins = {p for i in set(enable + disable) for p in self.board.interfaces[i].pins}
        ctls = {self.board.interfaces[i].controller for i in set(enable + disable)}
        claims = {
            rid: c.owners
            for rid, c in self.hw.res.claims.items()
            if rid in pins or rid.split("@")[0].split(".")[0].split(":")[0] in ctls
        }
        return {
            **asdict(plan),
            "writable": True,
            "hash": hashlib.sha256(text.encode()).hexdigest(),
            "diff": "".join(
                difflib.unified_diff(
                    text.splitlines(True),
                    updated.splitlines(True),
                    fromfile=str(path),
                    tofile="после применения",
                )
            ),
            "missing": missing,
            "claims": claims,
        }

    def apply(self, enable, disable, expected_hash):
        with self.lock:
            plan = self.preview(enable, disable)
            if not plan["writable"]:
                raise OverlayError("uEnv недоступен для записи")
            if not expected_hash or expected_hash != plan["hash"]:
                raise OverlayError("uEnv изменился — обновите предпросмотр", 409)
            if plan["missing"]:
                raise OverlayError("нет .dtbo: " + ", ".join(plan["missing"]))
            if plan["claims"]:
                raise OverlayError("сначала остановите владельцев затронутых ресурсов", 409)
            if not plan["needs_reboot"]:
                return {"changed": False, "needs_reboot": False}
            path = self.cfg.uenv_file
            text = path.read_text()
            if hashlib.sha256(text.encode()).hexdigest() != expected_hash:
                raise OverlayError("uEnv изменился — обновите предпросмотр", 409)
            updated = re.sub(
                r"^overlays=.*$", "overlays=" + " ".join(plan["overlays"]), text, flags=re.M
            )
            try:
                result = self.uenv.commit(expected_hash, updated)
            except UEnvError as e:
                raise OverlayError(str(e), 409) from e
            self.board.overlays = plan["overlays"]  # live DT still reflects running kernel
            return result
