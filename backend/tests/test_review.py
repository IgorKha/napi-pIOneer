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


def test_review_6_lifecycle_waits_for_actual_completion(client, monkeypatch):
    from pioneer.engine.runtime import ProjectRuntime
    pm=client.app.state.projects;p=pm.create("slow lifecycle")
    original_call=pm.engine.call;original_start=ProjectRuntime.start
    def call(fn,*args,timeout=.01):return original_call(fn,*args,timeout=timeout)
    def start(rt):time.sleep(.04);return original_start(rt)
    monkeypatch.setattr(pm.engine,"call",call);monkeypatch.setattr(ProjectRuntime,"start",start)
    pm.start(p.id);assert pm.engine.runtimes[p.id].running
    pm.stop(p.id);assert p.id not in pm.engine.runtimes


def test_review_7_watchdog_tolerates_transient_health_failure(tmp_path, monkeypatch):
    import asyncio
    from pioneer.watchdog import Watchdog
    from pioneer.config import Config
    w=Watchdog(Config(kernel_sys_dir=tmp_path));w.fd=123;w.timeout=.04
    results=iter([False, True, False, False, False]);writes=[]
    def health():
        if not next(results):raise TimeoutError("temporary engine stall")
    w.health=health
    monkeypatch.setattr("pioneer.watchdog.os.write",lambda fd,data:writes.append(data))
    asyncio.run(w._feed())
    assert len(writes)==4 and "перезагрузка" in w.error and w.fd==123


def test_review_8_dependencies_replace_rollback_and_delete(client, monkeypatch):
    from types import SimpleNamespace
    from pioneer.dependencies import install, site_for
    sm=client.app.state.scripts;sid=sm.create("dependencies").id;root=sm.tree(sid).root
    (root/"requirements.txt").write_text("")
    install(root,True);site=site_for(root);old=site.resolve()
    (root/"requirements.txt").write_text("fake-package==1")
    monkeypatch.setattr("pioneer.dependencies.subprocess.run",lambda *a,**kw:SimpleNamespace(returncode=1))
    with pytest.raises(ValueError):install(root,True)
    assert site.resolve()==old and old.exists()
    monkeypatch.setattr("pioneer.dependencies.subprocess.run",lambda *a,**kw:SimpleNamespace(returncode=0))
    install(root,True);assert site.resolve()!=old and not old.exists()
    target=site.resolve();sm.delete(sid)
    assert not site.exists() and not target.exists()


def test_review_9_uenv_transaction_restores_all_targets(tmp_path, monkeypatch):
    import hashlib
    from pioneer.uenv import UEnv
    from pioneer.config import Config
    from pioneer.codetree import CodeTree
    boot=tmp_path/'uEnv.txt';boot.write_text('old\n');boot.chmod(0o644)
    config=tmp_path/'config.json';config.write_text('old config');config.chmod(0o600)
    overlay=tmp_path/'new.dtbo'
    service=UEnv(Config(uenv_file=boot,data_dir=tmp_path/'data'))
    original=CodeTree._write;failed=False
    def write(path,data):
        nonlocal failed
        if path==boot and not failed:failed=True;raise OSError('disk failure')
        return original(path,data)
    monkeypatch.setattr(CodeTree,'_write',staticmethod(write))
    with pytest.raises(OSError):service.commit(hashlib.sha256(boot.read_bytes()).hexdigest(),'new\n',{config:b'new config',overlay:b'new'})
    assert boot.read_text()=='old\n' and config.read_text()=='old config' and not overlay.exists()
    assert boot.stat().st_mode&0o777==0o644 and config.stat().st_mode&0o777==0o600


def test_review_10_writes_and_batch_import_do_not_hash_tree(tmp_path, monkeypatch):
    import io, zipfile
    from pioneer.codetree import CodeTree
    root=tmp_path/"code";root.mkdir();t=CodeTree(root);t.set_meta({"entry":"main.py"})
    t.write("main.py",b"pass")
    def no_hash(*args):raise AssertionError("mutations must not hash existing files")
    monkeypatch.setattr("pioneer.codetree.hashlib.sha256",no_hash)
    t.write("main.py",b"value=1");t.mkdir("folder")
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,"w") as z:
        for i in range(50):z.writestr(f"folder/f{i}.py","value=1")
    original=CodeTree.files;scans=[]
    def files(self,**kw):scans.append(self.root);return original(self,**kw)
    monkeypatch.setattr(CodeTree,"files",files)
    t.import_zip(buf.getvalue())
    assert len(scans)==2 and t.read("folder/f49.py")==b"value=1"
