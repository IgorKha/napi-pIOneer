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


def test_review_2_stopped_submission_rejects_immediately(client, monkeypatch):
    from pioneer.engine.types import Origin, RequestContext
    from pioneer.remote import Remote
    from pioneer.api.app import API
    pm = client.app.state.projects
    p = pm.create("Remote race")
    pm.apply_ops(p.id, [{"op":"add_node","id":"cmd","type":"builtin.command","params":{"name":"race"}}])
    pm.start(p.id)
    remote = Remote(pm)
    original = pm.engine.inject_call
    def race(*args, **kwargs):
        pm.stop(p.id)
        return original(*args, **kwargs)
    monkeypatch.setattr(pm.engine, "inject_call", race)
    start = time.monotonic()
    result, status = remote.execute_command("race", {"id":"one"}, "http")
    assert status == 409 and not result["ok"] and time.monotonic()-start < 1
    done = threading.Event()
    ctx = RequestContext();ctx.on_complete.append(lambda c:done.set())
    original(p.id, Origin("remote", "http"), lambda:pytest.fail("must not execute"), ctx=ctx)
    assert done.wait(1) and ctx.done and ctx.rejected


@pytest.mark.parametrize("crash", [False, True])
def test_review_3_script_output_safe_high(hw, tmp_path, monkeypatch, crash):
    from pioneer.scripts import ScriptManager
    from conftest import wait_for
    writes = []
    original = hw.hal.open_output
    def output(*args, **kwargs):
        line = original(*args, **kwargs);write = line.write
        line.write = lambda value:(writes.append(value),write(value))
        return line
    monkeypatch.setattr(hw.hal, "open_output", output)
    sm = ScriptManager(tmp_path, hw)
    try:
        code = 'from pioneer.sdk import Output, run\nOutput("J9-7", initial=True, safe=True).off()\n'
        code += 'raise RuntimeError("test crash")\n' if crash else 'run()\n'
        sid = sm.create("relay", code).id;sm.start(sid)
        if crash: assert wait_for(lambda:sm.status(sid).state == "error", 5)
        else:
            assert wait_for(lambda:"J9-7" in hw.res.claims, 5)
            sm.stop(sid)
        assert writes[-1] is True and not hw.res.claims
        hw.acquire("other", "other", "J9-7", "gpio_out", safe=True)
        hw.close_all();assert writes[-1] is True
    finally:sm.stop_all()


def test_review_4_large_runtime_file_does_not_break_workspace(client):
    from pioneer.api.app import API
    from pioneer.codetree import CodeTree, FILE_LIMIT
    sm = client.app.state.scripts
    sid = sm.create("logger").id;tree=sm.tree(sid)
    with (tree.root/"large.csv").open("wb") as f:f.truncate(FILE_LIMIT*12)
    entry=next(e for e in tree.files() if e["path"]=="large.csv")
    assert entry["oversized"] and "sha256" not in entry
    tree.write("helper.py", b"value=1");tree.mkdir("folder")
    with pytest.raises(ValueError):tree.write("too_big", b"x"*(FILE_LIMIT+1))
    tree.delete("large.csv")
    with (tree.root/"large.csv").open("wb") as f:f.truncate(FILE_LIMIT+1)
    sm.delete(sid);assert not tree.root.exists()


def test_review_5_unexpected_rpc_error_responds_and_recovers(hw, tmp_path, monkeypatch):
    from pioneer.scripts import ScriptManager, _Runner
    from conftest import wait_for
    original = _Runner._handle
    def fail(self, op, **args):
        if op == "acquire": raise AssertionError("unexpected HAL failure")
        return original(self, op, **args)
    monkeypatch.setattr(_Runner, "_handle", fail)
    sm=ScriptManager(tmp_path,hw)
    try:
        sid=sm.create("RPC", 'from pioneer.sdk import Output, log\ntry:\n Output("J9-7")\nexcept Exception:\n log("RECOVERED")\n').id
        sm.start(sid)
        assert wait_for(lambda:sm.status(sid).state == "exited",5)
        assert "RECOVERED" in str(sm.log_tail(sid))
    finally:sm.stop_all()


def test_review_5_sdk_rpc_timeout_cleans_pending(monkeypatch):
    import socket
    from pioneer.sdk import _rpc
    monkeypatch.setattr(_rpc,"RPC_TIMEOUT_S",.03)
    a,b=socket.socketpair();c=_rpc._Client(a)
    try:
        with pytest.raises(_rpc.PioneerError,match="core не ответил"):c.call("read")
        assert not c._pending
    finally:b.close();a.close()
