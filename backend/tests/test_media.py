"""Image → POST → JSON на настоящем тестовом сервере; лимиты и защищённое превью."""
import importlib.util
import json
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from conftest import wait_for
from pioneer.api.app import API
from pioneer.config import REPO_ROOT
from pioneer.engine import Engine, Origin
from pioneer.engine.node import NodeError
from pioneer.images import MAX_IMAGE, ImageStore, image_type
from pioneer.nodes import registry
from pioneer.secrets_store import SecretStore
from test_api import anon, client  # noqa: F401
from test_engine import graph

spec = importlib.util.spec_from_file_location("media_test_server", REPO_ROOT / "tools/media-test-server.py")
server_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server_module)
PNG = server_module.PNG


@pytest.fixture
def server():
    seen = []

    class Handler(server_module.Handler):
        def do_GET(self):
            if self.path == "/bad":
                self.reply(200, b"<html>no image</html>", "text/html")
            elif self.path == "/large":
                self.reply(200, PNG + b"x" * MAX_IMAGE, "image/png")
            elif self.path == "/slow":
                time.sleep(.8)
                self.reply(200, PNG, "image/png")
            else:
                super().do_GET()

        def do_POST(self):
            if self.path == "/process":
                seen.append({"ctype": self.headers.get("Content-Type"), "token": self.headers.get("Authorization")})
                return super().do_POST()
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/html":
                self.reply(200, b"<html>not json</html>", "text/html")
            elif self.path == "/oversize":
                self.reply(200, b"x" * (256 * 1024 + 1))
            elif self.path == "/error":
                self.reply(503, b'{"value":99}')
            else:
                self.reply(200, json.dumps({"value": True}).encode())

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as srv:
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        yield f"http://127.0.0.1:{srv.server_port}", seen
        srv.shutdown()
        t.join()


def test_image_process_save_chain(hw, tmp_path, server):
    url, seen = server
    e = Engine(hw, registry())
    e.images = ImageStore(tmp_path)
    e.secrets = SecretStore(tmp_path)
    e.secrets.update({"test": "token-from-store"})
    try:
        e.start_project("abcdef", "Картинка", graph(
            ("b", "builtin.ui_button"), ("i", "builtin.image_url", {"url": url + "/image.png"}),
            ("h", "builtin.http_process", {"url": url + "/process", "headers": "Authorization: {{secret:test}}"}),
            ("s", "builtin.save_image"), ("c", "builtin.counter"),
            edges=[("b.click", "i.fetch"), ("i.image", "h.image"), ("i.ready", "h.send"),
                   ("i.image", "s.image"), ("i.ready", "s.save"), ("h.done", "c.up")]))
        rt = e.runtimes["abcdef"]
        e.ui("abcdef", "b", "click")
        assert wait_for(lambda: rt.sample("c", "count").value == 1, 3)
        assert rt.sample("i", "image").value == PNG
        assert rt.sample("h", "response").value == {"ok": True, "value": 42, "bytes": len(PNG), "content_type": "image/png"}
        assert rt.sample("h", "value").value == 42 and rt.sample("h", "found").value is True
        assert str(rt.sample("h", "response").origin) == "ui:b"
        assert seen == [{"ctype": "image/png", "token": "token-from-store"}]
        assert wait_for(lambda: rt.sample("s", "file") is not None, 2)
        assert (tmp_path / rt.sample("s", "file").value).read_bytes() == PNG
        assert rt.snapshot()["values"]["i.image"]["v"] == {"image": len(PNG)}
    finally:
        e.close()


@pytest.mark.parametrize("path,timeout,message", [("bad", 5, "PNG"), ("large", 5, "больше"), ("slow", .5, "загрузить")])
def test_bad_image_never_triggers_ready(hw, server, path, timeout, message):
    url, _ = server
    e = Engine(hw, registry())
    try:
        e.start_project("p", "Ошибка", graph(("b", "builtin.ui_button"),
            ("i", "builtin.image_url", {"url": url + "/" + path, "timeout_s": timeout}),
            ("c", "builtin.counter"), edges=[("b.click", "i.fetch"), ("i.ready", "c.up")]))
        rt = e.runtimes["p"]
        e.ui("p", "b", "click")
        assert wait_for(lambda: rt.node_state("i")["state"] == "error", 2)
        assert message in rt.node_state("i")["error"]
        assert rt.sample("i", "image").value is None
        assert rt.sample("c", "count").value == 0
    finally:
        e.close()


@pytest.mark.parametrize("endpoint,ok", [("process", True), ("html", False), ("oversize", False), ("error", False), ("bool", True)])
def test_process_json_and_error_outputs(hw, server, endpoint, ok):
    url, _ = server
    e = Engine(hw, registry())
    try:
        e.start_project("p", "JSON", graph(("b", "builtin.ui_button"),
            ("h", "builtin.http_process", {"url": url + "/" + endpoint, "mode": "json"}, {"body": {"x": 7}}),
            edges=[("b.click", "h.send")]))
        rt = e.runtimes["p"]
        e.ui("p", "b", "click")
        assert wait_for(lambda: not rt.nodes["h"]._in_flight, 3)
        assert rt.sample("h", "ok").value is ok
        assert rt.sample("h", "found").value is (endpoint == "process")
        assert rt.sample("h", "value").value == (42 if endpoint == "process" else 0)
        if endpoint == "process":
            assert rt.sample("h", "response").value["echo"] == {"x": 7}
        if not ok:
            assert "h.done" not in rt.snapshot()["events"]
            assert rt.snapshot()["events"]["h.failed"]["n"] == 1
    finally:
        e.close()


def test_image_store_limits_and_paths(tmp_path, monkeypatch):
    import pioneer.images as images
    store = ImageStore(tmp_path)
    monkeypatch.setattr(images, "MAX_SAVED", 1)
    name = store.save("abcdef", PNG)
    assert store.path("abcdef", name).read_bytes() == PNG
    with pytest.raises(NodeError, match="заполнено"):
        store.save("abcdef", PNG)
    for pid, bad_name in [("../escape", name), ("abcdef", "../file.png")]:
        with pytest.raises(NodeError):
            store.path(pid, bad_name)
    with pytest.raises(NodeError, match="PNG"):
        image_type(b"<svg></svg>")
    with pytest.raises(NodeError, match="2 МБ"):
        image_type(PNG + b"x" * MAX_IMAGE)
    store.path("abcdef", name).unlink()
    (store.root / "abcdef" / name).symlink_to(tmp_path / "anything")
    with pytest.raises(FileNotFoundError):
        store.path("abcdef", name)


def test_image_preview_and_saved_download_auth(client, anon):
    pid = client.post(f"{API}/projects", json={"name": "Превью"}).json()["id"]
    client.post(f"{API}/projects/{pid}/ops", json={"ops": [
        {"op": "add_node", "id": "b", "type": "builtin.ui_button"},
        {"op": "add_node", "id": "i", "type": "builtin.image_url", "params": {"url": "http://fixture/image.png"}},
        {"op": "connect", "from": ["b", "click"], "to": ["i", "fetch"]}]})
    assert client.post(f"{API}/projects/{pid}/start").status_code == 200
    pm = client.app.state.projects
    rt = pm.engine.runtimes[pid]
    pm.engine.call(lambda: rt.run_wave(Origin("test"), None,
                                     lambda: rt.nodes["i"].image.set(PNG)))
    r = client.get(f"{API}/projects/{pid}/image", params={"node": "i"})
    assert r.status_code == 200 and r.content == PNG
    assert r.headers["content-type"] == "image/png" and r.headers["cache-control"] == "no-store"
    name = pm.engine.images.save(pid, PNG)
    r = client.get(f"{API}/projects/{pid}/images/{name}")
    assert r.content == PNG and "attachment" in r.headers["content-disposition"]
    assert client.get(f"{API}/projects/{pid}/image", params={"node": "missing"}).status_code == 404
    client.post(f"{API}/auth/logout")
    assert anon.get(f"{API}/projects/{pid}/image", params={"node": "i"}).status_code == 401
    assert anon.get(f"{API}/projects/{pid}/images/{name}").status_code == 401
