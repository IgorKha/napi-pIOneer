"""Regression scenarios from docs/review-2026-10-11.md."""
import threading
import time
from pathlib import Path
import pytest
from test_api import client, anon


def test_review_1_boot_lock_order(client, monkeypatch):
    d = client.app.state.drivers
    held = []
    class Lock:
        def __init__(self, name): self.name = name
        def __enter__(self):
            if self.name == "drivers": assert held == ["overlays"]
            held.append(self.name)
        def __exit__(self, *args): assert held.pop() == self.name
    monkeypatch.setattr(d, "lock", Lock("drivers"))
    monkeypatch.setattr(d.overlays, "lock", Lock("overlays"))
    monkeypatch.setattr(d, "real", lambda: None)
    def plan(*args): raise RuntimeError("plan reached")
    monkeypatch.setattr(d, "_plan", plan)
    for fn, args in [(d.preview, ("J10-16", True)), (d.apply, ("J10-16", True, "hash"))]:
        with pytest.raises(RuntimeError, match="plan reached"): fn(*args)
    assert not held
