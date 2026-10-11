import io
import json
import stat
import zipfile

import pytest

from conftest import wait_for
from pioneer.codetree import CodeTree, TreeError, migrate
from pioneer.scripts import ScriptManager
from pioneer.userblocks import BlockManager, TEMPLATE
from pioneer.nodes import registry
from pioneer.blockhost import Host
from test_api import client, anon  # noqa: F401
from test_scripts import sm  # noqa: F401
from pioneer.api.app import API


def zip_data(entries):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for name, data in entries:
            z.writestr(name, data)
    return b.getvalue()


@pytest.fixture
def tree(tmp_path):
    t = CodeTree(tmp_path / "project")
    t.set_meta({"entry": "main.py", "name": "Проект"})
    t.write("main.py", b"print('main')")
    return t


@pytest.mark.parametrize("path", ["../secret", "/tmp/x", "a/../../x", "a/../x", "a\\b", "pioneer.json", "a/pioneer.json", "./x", "", "a//b"])
def test_unsafe_paths(tree, path):
    with pytest.raises(TreeError):
        tree.write(path, b"oops")


def test_links_limits_and_zip_validation(tree, tmp_path):
    outside = tmp_path / "secret"
    outside.write_text("safe")
    (tree.root / "link").symlink_to(outside)
    with pytest.raises(TreeError):
        tree.read("link")
    with pytest.raises(TreeError):
        tree.write("link", b"bad")
    (tree.root / "link").unlink()
    with pytest.raises(TreeError):
        tree.write("large", b"x" * (5 * 1024 * 1024 + 1))
    with pytest.raises(TreeError):
        tree.import_zip(zip_data([("main.py", "bad"), ("../secret", "bad")]))
    assert tree.read("main.py") == b"print('main')" and outside.read_text() == "safe"
    info = zipfile.ZipInfo("evil")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(TreeError):
        tree.import_zip(zip_data([(info, "../secret")]))
    tree.write("drivers", b"a file")
    with pytest.raises(OSError):
        tree.import_zip(zip_data([("main.py", "bad"), ("drivers/sensor.py", "bad")]))
    assert tree.read("main.py") == b"print('main')"


def test_roundtrip_metadata_and_entry_protection(tree, tmp_path):
    tree.write("drivers/a.py", b"VALUE = 42")
    tree.write("assets/picture.bin", b"\x00\xff")
    other = CodeTree(tmp_path / "other")
    other.set_meta({"name": "Other", "entry": "main.py"})
    other.import_zip(tree.export())
    assert other.read("assets/picture.bin") == b"\x00\xff"
    assert other.meta()["name"] == "Other"
    other.move("drivers", "hardware")
    assert other.read("hardware/a.py") == b"VALUE = 42"
    for action in (lambda: other.move("main.py", "x.py"), lambda: other.delete("main.py")):
        with pytest.raises(TreeError):
            action()


def test_migration_preserves_data_and_is_repeatable(tmp_path, hw):
    base = tmp_path / "scripts"
    base.mkdir()
    (base / "abcdef.py").write_text("print(42)")
    meta = {"id": "abcdef", "name": "Old", "autostart": True, "created": 1, "updated": 2}
    (base / "abcdef.json").write_text(json.dumps(meta))
    manager = ScriptManager(tmp_path, hw)
    assert manager.code("abcdef") == "print(42)"
    assert manager.meta("abcdef").autostart is True
    assert manager.meta("abcdef").entry == "main.py"
    migrate(base, "main.py")
    assert not (base / "abcdef.py").exists()
    blocks = tmp_path / "blocks"
    blocks.mkdir()
    (blocks / "abc123.py").write_text(TEMPLATE)
    (blocks / "abc123.json").write_text(json.dumps({"name": "Old block", "nodes": []}))
    bm = BlockManager(tmp_path, registry().copy())
    assert bm.get("abc123")["code"] == TEMPLATE
    assert bm.save("abc123", TEMPLATE)["error"] is None


def test_script_multifile_asset_lib_and_isolation(sm, tmp_path):
    lib = tmp_path / "lib" / "filters"
    lib.mkdir(parents=True)
    (lib / "__init__.py").write_text("OFFSET = 2")
    a = sm.create("A", 'from drivers.sensor import VALUE\nfrom filters import OFFSET\nfrom pathlib import Path\nprint(VALUE + OFFSET, Path("assets/data").read_text())\n')
    b = sm.create("B", "import neighbor\n")
    sm.tree(a.id).write("drivers/sensor.py", b"VALUE = 40")
    sm.tree(a.id).write("assets/data", b"asset")
    sm.tree(a.id).write("neighbor.py", b"VALUE = 1")
    sm.start(a.id)
    assert wait_for(lambda: sm.status(a.id).state == "exited", 5)
    assert wait_for(lambda: any("42 asset" in l["text"] for l in sm.log_tail(a.id)), 2)
    sm.start(b.id)
    assert wait_for(lambda: sm.status(b.id).state == "error", 5)
    assert "neighbor" in sm.status(b.id).error
    sm.tree(a.id).write("tools/check.py", b'raise ValueError("check")')
    sm.start(a.id, "tools/check.py")
    assert wait_for(lambda: sm.status(a.id).state == "error", 5)
    assert wait_for(lambda: any('File "tools/check.py", line 1' in l["text"] for l in sm.log_tail(a.id)), 2)


def test_two_block_roots_isolate_imports_and_late_callbacks(tmp_path):
    import socket
    p, c = socket.socketpair()
    host = Host(p)
    code = '''from pioneer.block import node, Node, Out, Number
from drivers import sensor
@node(id="example", title="Example")
class Example(Node):
    out = Out(Number)
    def on_change(self, changed):
        from drivers.sensor import VALUE
        self.out.set(VALUE + sensor.VALUE)
'''
    try:
        for i, value in enumerate((10, 20)):
            root = tmp_path / "blocks" / str(i)
            (root / "drivers").mkdir(parents=True)
            (root / "drivers" / "sensor.py").write_text(f"VALUE = {value}")
            (root / "block.py").write_text(code)
            host.handle({"op": "load", "file": str(root / "block.py"), "block": "example", "node": str(i)})
        for i, value in enumerate((20, 40, 20)):
            actions = host.handle({"op": "call", "method": "on_change", "node": str(i % 2)})
            assert actions == [["set", "out", value]]
    finally:
        p.close()
        c.close()


def test_file_api_and_code_key(client):
    sid = client.post(f"{API}/scripts", json={"name": "Multi", "code": "print(1)"}).json()["id"]
    base = f"{API}/code/scripts/{sid}"
    assert client.put(base + "/file", json={"path": "drivers/a.py", "text": "VALUE=1"}).status_code == 200
    assert client.get(base + "/file", params={"path": "drivers/a.py"}).json()["text"] == "VALUE=1"
    assert client.put(base + "/file", json={"path": "../x", "text": "bad"}).status_code == 400
    assert client.post(base + "/delete", json={"path": "main.py"}).status_code == 400
    assert client.post(base + "/upload", params={"path": "assets/x.bin"}, content=b"\x00\xff").status_code == 200
    assert client.get(base + "/download", params={"path": "assets/x.bin"}).content == b"\x00\xff"
    archive = client.get(base + "/export.zip")
    assert archive.status_code == 200
    assert client.post(base + "/import.zip", content=archive.content).status_code == 200
    assert client.put(base + "/file", headers={"X-Pioneer": "0"}, json={"path": "a", "text": "b"}).status_code == 403
    client.app.state.secrets.update({"code_api_key": "code-key"})
    client.cookies.clear()
    assert client.get(base + "/tree").status_code == 401
    assert client.get(base + "/tree", headers={"X-Pioneer-Key": "wrong"}).status_code == 401
    assert client.get(base + "/tree", headers={"X-Pioneer-Key": "code-key"}).status_code == 200


def test_cli_push_pull_and_deletions(client, tmp_path):
    import importlib.util
    from pioneer.config import REPO_ROOT
    spec = importlib.util.spec_from_file_location("pioneer_cli", REPO_ROOT / "tools/pioneer-cli/pioneer.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    sid = client.post(f"{API}/scripts", json={"name": "CLI", "code": "print(1)"}).json()["id"]
    remote = cli.Client("http://testserver", "unused", "scripts", sid)
    def request(path, method="GET", data=None, binary=False):
        options = {"content": data} if isinstance(data, bytes) else {"json": data} if data is not None else {}
        r = client.request(method, f"{API}/code/scripts/{sid}/" + path, **options)
        assert r.is_success, r.text
        return r.content if binary else r.json()
    remote.request = request
    root = tmp_path / "local"
    root.mkdir()
    (root / "main.py").write_text("from drivers.sensor import VALUE\nprint(VALUE)")
    (root / "drivers").mkdir()
    (root / "drivers/sensor.py").write_text("VALUE = 42")
    assert cli.push(remote, root) == 2
    assert cli.push(remote, root) == 0
    dest = tmp_path / "pulled"
    assert cli.pull(remote, dest) == 2
    assert (dest / "drivers/sensor.py").read_text() == "VALUE = 42"
    (root / "drivers/sensor.py").unlink()
    assert cli.push(remote, root, delete=True) == 1
    assert all(f["path"] != "drivers/sensor.py" for f in request("tree")["files"])
    (root / "evil").symlink_to(dest / "main.py")
    with pytest.raises(ValueError, match="симлинк"):
        cli.push(remote, root, delete=True)


def test_block_helpers_revalidate_through_file_api(client):
    b = client.post(f"{API}/blocks", json={"name": "Multi"}).json()
    base = f"{API}/code/blocks/{b['id']}"
    assert client.put(base + "/file", json={"path": "drivers/limits.py", "text": "LOW = 12"}).status_code == 200
    code = TEMPLATE.replace("from pioneer.block import", "from drivers.limits import LOW\nfrom pioneer.block import").replace('In(Number, 30,', 'In(Number, LOW,')
    r = client.put(base + "/file", json={"path": "block.py", "text": code}).json()
    assert r["error"] is None
    schema = client.app.state.blocks.registry.get("user.hysteresis@1")
    assert schema.inputs["low"].default == 12
    r = client.put(base + "/file", json={"path": "drivers/limits.py", "text": "LOW = 99"}).json()
    assert r["error"] is None
    assert client.app.state.blocks.registry.get("user.hysteresis@1").inputs["low"].default == 99
