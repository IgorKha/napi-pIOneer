"""Regression scenarios from docs/review-2026-10-11.md."""

import threading
import time
from pathlib import Path
import pytest
from test_api import client, anon
from test_kernel_drivers import driver, elf


def test_review_1_boot_lock_order(client, monkeypatch):
    d = client.app.state.drivers
    held = []

    class Lock:
        def __init__(self, name):
            self.name = name

        def __enter__(self):
            if self.name == "drivers":
                assert held == ["overlays"]
            held.append(self.name)

        def __exit__(self, *args):
            assert held.pop() == self.name

    monkeypatch.setattr(d, "lock", Lock("drivers"))
    monkeypatch.setattr(d.overlays, "lock", Lock("overlays"))
    monkeypatch.setattr(d, "real", lambda: None)

    def plan(*args):
        raise RuntimeError("plan reached")

    monkeypatch.setattr(d, "_plan", plan)
    for fn, args in [(d.preview, ("J10-16", True)), (d.apply, ("J10-16", True, "hash"))]:
        with pytest.raises(RuntimeError, match="plan reached"):
            fn(*args)
    assert not held


def test_review_2_stopped_submission_rejects_immediately(client, monkeypatch):
    from pioneer.engine.types import Origin, RequestContext
    from pioneer.remote import Remote
    from pioneer.api.app import API

    pm = client.app.state.projects
    p = pm.create("Remote race")
    pm.apply_ops(
        p.id,
        [{"op": "add_node", "id": "cmd", "type": "builtin.command", "params": {"name": "race"}}],
    )
    pm.start(p.id)
    remote = Remote(pm)
    original = pm.engine.inject_call

    def race(*args, **kwargs):
        pm.stop(p.id)
        return original(*args, **kwargs)

    monkeypatch.setattr(pm.engine, "inject_call", race)
    start = time.monotonic()
    result, status = remote.execute_command("race", {"id": "one"}, "http")
    assert status == 409 and not result["ok"] and time.monotonic() - start < 1
    done = threading.Event()
    ctx = RequestContext()
    ctx.on_complete.append(lambda c: done.set())
    original(p.id, Origin("remote", "http"), lambda: pytest.fail("must not execute"), ctx=ctx)
    assert done.wait(1) and ctx.done and ctx.rejected


@pytest.mark.parametrize("crash", [False, True])
def test_review_3_script_output_safe_high(hw, tmp_path, monkeypatch, crash):
    from pioneer.scripts import ScriptManager
    from conftest import wait_for

    writes = []
    original = hw.hal.open_output

    def output(*args, **kwargs):
        line = original(*args, **kwargs)
        write = line.write
        line.write = lambda value: (writes.append(value), write(value))
        return line

    monkeypatch.setattr(hw.hal, "open_output", output)
    sm = ScriptManager(tmp_path, hw)
    try:
        code = (
            'from pioneer.sdk import Output, run\nOutput("J9-7", initial=True, safe=True).off()\n'
        )
        code += 'raise RuntimeError("test crash")\n' if crash else "run()\n"
        sid = sm.create("relay", code).id
        sm.start(sid)
        if crash:
            assert wait_for(lambda: sm.status(sid).state == "error", 5)
        else:
            assert wait_for(lambda: "J9-7" in hw.res.claims, 5)
            sm.stop(sid)
        assert writes[-1] is True and not hw.res.claims
        hw.acquire("other", "other", "J9-7", "gpio_out", safe=True)
        hw.close_all()
        assert writes[-1] is True
    finally:
        sm.stop_all()


def test_review_4_large_runtime_file_does_not_break_workspace(client):
    from pioneer.api.app import API
    from pioneer.codetree import CodeTree, FILE_LIMIT

    sm = client.app.state.scripts
    sid = sm.create("logger").id
    tree = sm.tree(sid)
    with (tree.root / "large.csv").open("wb") as f:
        f.truncate(FILE_LIMIT * 12)
    entry = next(e for e in tree.files() if e["path"] == "large.csv")
    assert entry["oversized"] and "sha256" not in entry
    tree.write("helper.py", b"value=1")
    tree.mkdir("folder")
    with pytest.raises(ValueError):
        tree.write("too_big", b"x" * (FILE_LIMIT + 1))
    tree.delete("large.csv")
    with (tree.root / "large.csv").open("wb") as f:
        f.truncate(FILE_LIMIT + 1)
    sm.delete(sid)
    assert not tree.root.exists()


def test_review_5_unexpected_rpc_error_responds_and_recovers(hw, tmp_path, monkeypatch):
    from pioneer.scripts import ScriptManager, _Runner
    from conftest import wait_for

    original = _Runner._handle

    def fail(self, op, **args):
        if op == "acquire":
            raise AssertionError("unexpected HAL failure")
        return original(self, op, **args)

    monkeypatch.setattr(_Runner, "_handle", fail)
    sm = ScriptManager(tmp_path, hw)
    try:
        sid = sm.create(
            "RPC",
            'from pioneer.sdk import Output, log\ntry:\n Output("J9-7")\nexcept Exception:\n log("RECOVERED")\n',
        ).id
        sm.start(sid)
        assert wait_for(lambda: sm.status(sid).state == "exited", 5)
        assert "RECOVERED" in str(sm.log_tail(sid))
    finally:
        sm.stop_all()


def test_review_5_sdk_rpc_timeout_cleans_pending(monkeypatch):
    import socket
    from pioneer.sdk import _rpc

    monkeypatch.setattr(_rpc, "RPC_TIMEOUT_S", 0.03)
    a, b = socket.socketpair()
    c = _rpc._Client(a)
    try:
        with pytest.raises(_rpc.PioneerError, match="core не ответил"):
            c.call("read")
        assert not c._pending
    finally:
        b.close()
        a.close()


def test_review_6_lifecycle_waits_for_actual_completion(client, monkeypatch):
    from pioneer.engine.runtime import ProjectRuntime

    pm = client.app.state.projects
    p = pm.create("slow lifecycle")
    original_call = pm.engine.call
    original_start = ProjectRuntime.start

    def call(fn, *args, timeout=0.01):
        return original_call(fn, *args, timeout=timeout)

    def start(rt):
        time.sleep(0.04)
        return original_start(rt)

    monkeypatch.setattr(pm.engine, "call", call)
    monkeypatch.setattr(ProjectRuntime, "start", start)
    pm.start(p.id)
    assert pm.engine.runtimes[p.id].running
    pm.stop(p.id)
    assert p.id not in pm.engine.runtimes


def test_review_7_watchdog_tolerates_transient_health_failure(tmp_path, monkeypatch):
    import asyncio
    from pioneer.watchdog import Watchdog
    from pioneer.config import Config

    w = Watchdog(Config(kernel_sys_dir=tmp_path))
    w.fd = 123
    w.timeout = 0.04
    results = iter([False, True, False, False, False])
    writes = []

    def health():
        if not next(results):
            raise TimeoutError("temporary engine stall")

    w.health = health
    monkeypatch.setattr("pioneer.watchdog.os.write", lambda fd, data: writes.append(data))
    asyncio.run(w._feed())
    assert len(writes) == 4 and "перезагрузка" in w.error and w.fd == 123


def test_review_8_dependencies_replace_rollback_and_delete(client, monkeypatch):
    from types import SimpleNamespace
    from pioneer.dependencies import install, site_for

    sm = client.app.state.scripts
    sid = sm.create("dependencies").id
    root = sm.tree(sid).root
    (root / "requirements.txt").write_text("")
    install(root, True)
    site = site_for(root)
    old = site.resolve()
    (root / "requirements.txt").write_text("fake-package==1")
    monkeypatch.setattr(
        "pioneer.dependencies.subprocess.run", lambda *a, **kw: SimpleNamespace(returncode=1)
    )
    with pytest.raises(ValueError):
        install(root, True)
    assert site.resolve() == old and old.exists()
    monkeypatch.setattr(
        "pioneer.dependencies.subprocess.run", lambda *a, **kw: SimpleNamespace(returncode=0)
    )
    install(root, True)
    assert site.resolve() != old and not old.exists()
    target = site.resolve()
    sm.delete(sid)
    assert not site.exists() and not target.exists()


def test_review_9_uenv_transaction_restores_all_targets(tmp_path, monkeypatch):
    import hashlib
    from pioneer.uenv import UEnv
    from pioneer.config import Config
    from pioneer.codetree import CodeTree

    boot = tmp_path / "uEnv.txt"
    boot.write_text("old\n")
    boot.chmod(0o644)
    config = tmp_path / "config.json"
    config.write_text("old config")
    config.chmod(0o600)
    overlay = tmp_path / "new.dtbo"
    service = UEnv(Config(uenv_file=boot, data_dir=tmp_path / "data"))
    original = CodeTree._write
    failed = False

    def write(path, data):
        nonlocal failed
        if path == boot and not failed:
            failed = True
            raise OSError("disk failure")
        return original(path, data)

    monkeypatch.setattr(CodeTree, "_write", staticmethod(write))
    with pytest.raises(OSError):
        service.commit(
            hashlib.sha256(boot.read_bytes()).hexdigest(),
            "new\n",
            {config: b"new config", overlay: b"new"},
        )
    assert (
        boot.read_text() == "old\n" and config.read_text() == "old config" and not overlay.exists()
    )
    assert boot.stat().st_mode & 0o777 == 0o644 and config.stat().st_mode & 0o777 == 0o600


def test_review_10_writes_and_batch_import_do_not_hash_tree(tmp_path, monkeypatch):
    import io, zipfile
    from pioneer.codetree import CodeTree

    root = tmp_path / "code"
    root.mkdir()
    t = CodeTree(root)
    t.set_meta({"entry": "main.py"})
    t.write("main.py", b"pass")

    def no_hash(*args):
        raise AssertionError("mutations must not hash existing files")

    monkeypatch.setattr("pioneer.codetree.hashlib.sha256", no_hash)
    t.write("main.py", b"value=1")
    t.mkdir("folder")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i in range(50):
            z.writestr(f"folder/f{i}.py", "value=1")
    original = CodeTree.files
    scans = []

    def files(self, **kw):
        scans.append(self.root)
        return original(self, **kw)

    monkeypatch.setattr(CodeTree, "files", files)
    t.import_zip(buf.getvalue())
    assert len(scans) == 2 and t.read("folder/f49.py") == b"value=1"


def test_review_11_boot_commit_does_not_hold_hardware_locks(driver, monkeypatch):
    driver.install(elf())
    plan = driver.preview("J10-16", True)
    original = driver.overlays.uenv.commit

    def commit(*args, **kwargs):
        assert not driver.hw._lock._is_owned()
        assert not driver.hw.res._lock.locked()
        return original(*args, **kwargs)

    monkeypatch.setattr(driver.overlays.uenv, "commit", commit)
    assert driver.apply("J10-16", True, plan["hash"])["changed"]


def test_review_12_duplicate_commands_share_one_execution(client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from pioneer.remote import Remote

    pm = client.app.state.projects
    p = pm.create("dedupe")
    pm.apply_ops(
        p.id,
        [{"op": "add_node", "id": "cmd", "type": "builtin.command", "params": {"name": "once"}}],
    )
    pm.start(p.id)
    node = pm.engine.runtimes[p.id].nodes["cmd"]
    original = node.deliver
    executions = []

    def deliver(*args):
        executions.append(1)
        time.sleep(0.06)
        return original(*args)

    monkeypatch.setattr(node, "deliver", deliver)
    remote = Remote(pm)
    gate = threading.Barrier(2)

    def send():
        gate.wait()
        return remote.execute_command("once", {"id": "same"}, "mqtt")

    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(send)
        b = pool.submit(send)
        assert a.result() == b.result()
    assert len(executions) == 1 and not remote._pending


def test_review_13_webhook_rejects_query_token_and_bounds_stream(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from fastapi import HTTPException
    from pioneer.api import hooks

    node = SimpleNamespace(expected_token=lambda: "secret")
    monkeypatch.setattr(hooks, "_find", lambda *args: ("project", node))
    request = SimpleNamespace(headers={}, query_params={"token": "secret"})
    assert asyncio.run(hooks.hook("test", request)).status_code == 401
    reads = []

    async def stream():
        for i in range(100):
            reads.append(i)
            yield b"x" * 65536

    request.headers = {"x-pioneer-token": "secret"}
    request.stream = stream
    with pytest.raises(HTTPException) as error:
        asyncio.run(hooks.hook("test", request))
    assert error.value.status_code == 413 and len(reads) == 5


def test_review_14_failed_mode_switch_retains_owner_and_restores_input(hw, monkeypatch):
    hw.acquire("owner", "Owner", "J9-7", "gpio_in")

    def fail(*args, **kwargs):
        raise OSError("HAL cannot open output")

    monkeypatch.setattr(hw.hal, "open_output", fail)
    with pytest.raises(OSError):
        hw.acquire("owner", "Owner", "J9-7", "gpio_out")
    claim = hw.res.claims["J9-7"]
    assert claim.mode == "gpio_in" and "owner" in claim.owners
    assert hw._open["J9-7"].mode == "gpio_in" and hw.value("J9-7") is not None
    with pytest.raises(OSError):
        hw.acquire("new", "New", "J9-12", "gpio_out")
    assert "J9-12" not in hw.res.claims
    hw.release("owner")


def test_review_16_low_frequency_pwm_closes_before_releasing():
    from pioneer.hal.softpwm import SoftPwm

    class Line:
        def __init__(self):
            self.high = threading.Event()
            self.closed = False
            self.values = []

        def write(self, value):
            assert not self.closed
            self.values.append(value)
            if value:
                self.high.set()

        def close(self):
            self.closed = True

    line = Line()
    pwm = SoftPwm(line, freq=0.01, duty=0.5)
    assert line.high.wait(1)
    start = time.monotonic()
    pwm.close(True)
    assert time.monotonic() - start < 0.5
    assert not pwm._thread.is_alive()
    assert line.closed and line.values[-1] is True


def test_review_17_blocked_worker_does_not_block_edge_dispatch():
    import queue
    import socket
    from types import SimpleNamespace
    from pioneer.scripts import _Runner, Status

    runner = _Runner.__new__(_Runner)
    runner.sock, peer = socket.socketpair()
    runner.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
    runner._send_lock = threading.Lock()
    runner._edges = queue.Queue(maxsize=4)
    runner._edge_stop = threading.Event()
    runner.status = Status()
    thread = threading.Thread(target=runner._send_edges)
    thread.start()
    try:
        event = SimpleNamespace(rising=True, timestamp_ns=1)
        start = time.monotonic()
        for _ in range(2000):
            runner._edge("J10-2", event)
        assert time.monotonic() - start < 0.5
        assert runner.status.edge_dropped > 0
        assert runner._edges.qsize() <= 4
        # Another listener on the same reader continues immediately.
        delivered = threading.Event()
        for cb in (runner._edge, lambda pin, ev: delivered.set()):
            cb("J10-2", event)
        assert delivered.is_set()
    finally:
        runner._edge_stop.set()
        runner.sock.shutdown(socket.SHUT_RDWR)
        thread.join(1)
        runner.sock.close()
        peer.close()
    assert not thread.is_alive()


def test_review_18_pwm_safe_high_stays_enabled_and_can_be_reused(tmp_path):
    from pioneer.hal.hardware_pwm import HardwarePwm
    from pioneer.hal.base import LineBusy

    path = tmp_path / "pwm0"
    path.mkdir()
    for key, value in {
        "period": "10000000",
        "duty_cycle": "0",
        "enable": "1",
        "polarity": "normal",
    }.items():
        (path / key).write_text(value)
    (tmp_path / "unexport").write_text("untouched")
    pwm = HardwarePwm.__new__(HardwarePwm)
    pwm.path, pwm.chip = path, tmp_path
    pwm.freq, pwm.duty, pwm.closed, pwm.lock = 100.0, 0.0, False, threading.RLock()
    pwm.close(True)
    assert (path / "enable").read_text() == "1"
    assert (path / "duty_cycle").read_text() == (path / "period").read_text()
    assert (tmp_path / "unexport").read_text() == "untouched"
    pwm.close(False)  # closing twice must not undo the safe level
    replacement = HardwarePwm(tmp_path)
    replacement.close(False)
    assert (tmp_path / "unexport").read_text() == "0"
    # Fake sysfs retains its directory: it is no longer a core-owned hold.
    with pytest.raises(LineBusy):
        HardwarePwm(tmp_path)
