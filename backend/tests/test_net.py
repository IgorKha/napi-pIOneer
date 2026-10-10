"""Сеть: секреты, HTTP-запрос, входящий webhook и «Ответ» через контекст запроса."""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from conftest import wait_for
from pioneer.api.app import API
from pioneer.engine import Engine
from pioneer.nodes import registry
from pioneer.secrets_store import SecretStore
from test_api import anon, client  # noqa: F401
from test_engine import graph


@pytest.fixture
def http_server():
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": json.loads(body)})
            out = json.dumps({"echo": json.loads(body), "n": len(seen)}).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):
            self.send_response(404)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", seen
    srv.shutdown()


def test_secret_store(tmp_path):
    s = SecretStore(tmp_path)
    assert s.update({"token": "abc", "x": "1"}) == ["token", "x"]
    assert oct(os.stat(tmp_path / "secrets.json").st_mode & 0o777) == "0o600"
    assert s.resolve("Bearer {{secret:token}}") == "Bearer abc"
    s.update({"x": None})
    assert s.names() == ["token"]
    with pytest.raises(Exception, match="нет секрета «nope»"):
        s.resolve("{{secret:nope}}")
    with pytest.raises(ValueError):
        s.update({"bad name": "1"})


def test_http_request_node(hw, tmp_path, http_server):
    url, seen = http_server
    e = Engine(hw, registry())
    e.secrets = SecretStore(tmp_path)
    e.secrets.update({"tok": "s3cret"})
    try:
        g = graph(("b", "builtin.ui_button"), ("n", "builtin.ui_number"),
                  ("h", "builtin.http_request", {"method": "POST", "url": url + "/api",
                                                 "headers": "Authorization: Bearer {{secret:tok}}"}),
                  ("c", "builtin.counter"),
                  edges=[("b.click", "h.send"), ("n.value", "h.body"), ("h.done", "c.up")])
        e.start_project("p", "HTTP", g)
        e.ui("p", "n", 21)
        e.ui("p", "b", "click")
        rt = e.runtimes["p"]
        assert wait_for(lambda: rt.sample("c", "count").value == 1, 3)
        assert seen == [{"path": "/api", "auth": "Bearer s3cret", "body": 21.0}]
        assert rt.sample("h", "status").value == 201 and rt.sample("h", "ok").value is True
        assert rt.sample("h", "response").value == {"echo": 21.0, "n": 1}
        assert str(rt.sample("h", "response").origin) == "ui:b"  # ответ — продолжение волны кнопки
    finally:
        e.close()


def test_webhook_reply_and_auth(client):
    client.put(f"{API}/secrets", json={"hook": "t0k"})
    assert client.get(f"{API}/secrets").json() == {"names": ["hook"]}  # значения не отдаются
    pid = client.post(f"{API}/projects", json={"name": "Хук"}).json()["id"]
    r = client.post(f"{API}/projects/{pid}/ops", json={"ops": [
        {"op": "add_node", "id": "w", "type": "builtin.webhook_in", "params": {"path": "temp", "token": "{{secret:hook}}"}},
        {"op": "add_node", "id": "j", "type": "builtin.json_get", "params": {"path": "t"}},
        {"op": "add_node", "id": "m", "type": "builtin.math", "params": {"op": "mul"}, "inputs": {"b": 10}},
        {"op": "add_node", "id": "d", "type": "builtin.delay", "params": {"mode": "both"}, "inputs": {"delay_ms": 50}},
        {"op": "add_node", "id": "e", "type": "builtin.edge", "params": {"mode": "both"}},
        {"op": "add_node", "id": "r", "type": "builtin.reply", "params": {"status": 202}},
        {"op": "connect", "from": ["w", "data"], "to": ["j", "data"]},
        {"op": "connect", "from": ["j", "number"], "to": ["m", "a"]},
        {"op": "connect", "from": ["m", "result"], "to": ["r", "body"]},
        {"op": "connect", "from": ["j", "found"], "to": ["d", "value"]},
        {"op": "connect", "from": ["d", "out"], "to": ["e", "value"]},
        {"op": "connect", "from": ["e", "event"], "to": ["r", "send"]},
    ]})
    assert r.status_code == 200, r.json()
    assert client.post(f"{API}/projects/{pid}/start").json()["state"] == "running"
    hooks = "/pioneer/api/hooks"
    assert client.post(f"{hooks}/temp", json={"t": 4}).status_code == 401
    assert client.post(f"{hooks}/nope", json={"t": 4}).status_code == 404
    t0 = time.monotonic()
    r = client.post(f"{hooks}/temp?token=t0k", json={"t": 4})
    # ответ пришёл после Задержки: контекст запроса проехал через Delay и Фронт к «Ответу»
    assert r.status_code == 202 and r.json() == 40 and time.monotonic() - t0 >= 0.04
    live = client.get(f"{API}/projects/{pid}/live").json()
    assert live["values"]["w.data"]["o"] == "remote:http"
    m = client.get(f"{API}/projects/{pid}").json()["manifest"]
    assert m == [{"kind": "webhook", "name": "temp", "schema": {"method": "POST"}, "node": "w"}]


def test_webhook_without_reply_acks(client):
    client.put(f"{API}/secrets", json={"hook": "t0k"})
    pid = client.post(f"{API}/projects", json={"name": "Хук2"}).json()["id"]
    client.post(f"{API}/projects/{pid}/ops", json={"ops": [
        {"op": "add_node", "id": "w", "type": "builtin.webhook_in", "params": {"path": "ping", "token": "{{secret:hook}}"}},
        {"op": "add_node", "id": "l", "type": "builtin.log"},
        {"op": "connect", "from": ["w", "data"], "to": ["l", "value"]}]})
    client.post(f"{API}/projects/{pid}/start")
    r = client.post("/pioneer/api/hooks/ping", headers={"X-Pioneer-Token": "t0k"}, json={"hello": 1})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert wait_for(lambda: any("hello" in line["text"] for line in client.get(f"{API}/projects/{pid}/log").json()), 1)


def test_json_get(hw):
    e = Engine(hw, registry())
    try:
        e.start_project("p", "J", graph(("w", "builtin.ui_number"), ("j", "builtin.json_get", {"path": "a.1.v"})))
        rt = e.runtimes["p"]
        e.inject("p", __import__("pioneer.engine", fromlist=["Origin"]).Origin("test"),
                 {("w", "value"): 0})
        node = rt.nodes["j"]
        rt.graph.nodes["j"].inputs["data"] = {"a": [{}, {"v": "3,5"}]}
        e.call(lambda: rt.run_wave(__import__("pioneer.engine", fromlist=["Origin"]).Origin("test"), None,
                                    lambda: node.on_change({"data"})))
        assert rt.sample("j", "number").value == 3.5 and rt.sample("j", "found").value is True
        assert rt.sample("j", "text").value == "3,5" and rt.sample("j", "flag").value is True
    finally:
        e.close()


def test_remote_command_arbiter_state(client):
    """idea 003, критерий: крутилка ↔ команда servo.set через Арбитр, ответ и состояние."""
    client.post(f"{API}/bench/models", json={"kind": "voltage", "config": {"pin": "J9-26", "volts": 0.9}})
    remote = "/pioneer/api/remote"
    assert client.post(f"{remote}/cmd/servo.set", json={}).status_code == 403  # ключ не задан
    client.put(f"{API}/secrets", json={"remote_api_key": "k3y"})
    K = {"X-Pioneer-Key": "k3y"}
    pid = client.post(f"{API}/projects", json={"name": "Серва"}).json()["id"]
    r = client.post(f"{API}/projects/{pid}/ops", json={"ops": [
        {"op": "add_node", "id": "adc", "type": "builtin.analog_in", "params": {"pin": "J9-26", "interval_ms": 20, "average": 1}},
        {"op": "add_node", "id": "map", "type": "builtin.map_range", "inputs": {"from_max": 1.8, "to_max": 180}},
        {"op": "add_node", "id": "cmd", "type": "builtin.command",
         "params": {"name": "servo.set", "args_spec": "angle: Number 0..180"}},
        {"op": "add_node", "id": "arb", "type": "builtin.arbiter"},
        {"op": "add_node", "id": "st", "type": "builtin.state", "params": {"name": "servo.angle"}},
        {"op": "add_node", "id": "rep", "type": "builtin.reply"},
        {"op": "connect", "from": ["adc", "volts"], "to": ["map", "value"]},
        {"op": "connect", "from": ["map", "result"], "to": ["arb", "a"]},
        {"op": "connect", "from": ["cmd", "number"], "to": ["arb", "b"]},
        {"op": "connect", "from": ["arb", "value"], "to": ["st", "value"]},
        {"op": "connect", "from": ["arb", "value"], "to": ["rep", "body"]},
        {"op": "connect", "from": ["cmd", "received"], "to": ["rep", "send"]},
    ]})
    assert r.status_code == 200, r.json()
    assert client.post(f"{API}/projects/{pid}/start").json()["state"] == "running"
    assert wait_for(lambda: client.get(f"{remote}/state/servo.angle", headers=K).json().get("value") == 90.0, 2)

    r = client.post(f"{remote}/cmd/servo.set", headers=K, json={"id": "1", "args": {"angle": 30}})
    assert r.json() == {"id": "1", "ok": True, "result": 30.0}
    st = client.get(f"{remote}/state/servo.angle", headers=K).json()
    assert st["value"] == 30.0 and st["source"] == "remote:http"
    # тот же id — ответ из кэша, граф не трогается
    assert client.post(f"{remote}/cmd/servo.set", headers=K, json={"id": "1", "args": {"angle": 99}}).json()["result"] == 30.0
    bad = client.post(f"{remote}/cmd/servo.set", headers=K, json={"args": {"angle": 500}})
    assert bad.status_code == 422 and "0…180" in bad.json()["error"]
    assert client.post(f"{remote}/cmd/nope", headers=K, json={}).status_code == 404
    assert client.post(f"{remote}/cmd/servo.set", headers={"X-Pioneer-Key": "x"}, json={}).status_code == 401
    # крутилку повернули — управление вернулось к ней
    bench = client.get(f"{API}/bench").json()["models"][0]["id"]
    client.post(f"{API}/bench/models/{bench}/actions/set", json={"volts": 1.8})
    assert wait_for(lambda: client.get(f"{remote}/state/servo.angle", headers=K).json()["value"] > 179, 2)  # 10 бит: 1.8 В читается как ~1.798 В
    d = client.get(f"{remote}/describe", headers=K).json()
    assert d["commands"] == [{"name": "servo.set", "args": {"angle": {"type": "Number", "min": 0.0, "max": 180.0}},
                              "project": "Серва"}]
    assert d["states"][0]["name"] == "servo.angle" and d["states"][0]["source"].startswith("adc")
