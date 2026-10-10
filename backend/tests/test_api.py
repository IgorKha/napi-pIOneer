from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pioneer.api.app import API, create_app
from pioneer.auth import Auth
from pioneer.config import Config


PASSWORD = "secret-pw"


@pytest.fixture
def anon(tmp_path):
    Auth(tmp_path).set_password(PASSWORD)
    cfg = Config(hal="bench", uenv_file=Path("/nonexistent"), data_dir=tmp_path)
    with TestClient(create_app(cfg), headers={"X-Pioneer": "1"}) as c:
        yield c


@pytest.fixture
def client(anon):
    assert anon.post(f"{API}/auth/login", json={"password": PASSWORD}).status_code == 200
    return anon


def test_board(client):
    r = client.get(f"{API}/board").json()
    assert r["hal"] == "bench"
    pins = {p["id"]: p for p in r["pins"]}
    assert pins["J9-7"]["runtime_modes"] == ["gpio_in", "gpio_out", "soft_pwm"]
    assert pins["J9-11"]["function"] == "i2c3_m0"
    assert {o["id"] for o in pins["J9-11"]["interface_options"]} == {"i2c3_m0", "pwm2"}
    ifaces = {i["id"]: i for i in r["interfaces"]}
    assert ifaces["uart4"]["enabled"] is False and ifaces["i2c1"]["enabled"] is True


def test_manual_out_and_bench_in(client):
    r = client.post(f"{API}/board/pins/J9-7/manual", json={"mode": "gpio_out", "value": True})
    assert r.status_code == 200 and r.json()["value"] is True

    assert client.post(f"{API}/board/pins/J9-12/manual", json={"mode": "gpio_in"}).status_code == 200
    r = client.post(f"{API}/bench/models", json={"kind": "level", "id": "ext", "config": {"pin": "J9-12"}})
    assert r.status_code == 200
    client.post(f"{API}/bench/models/ext/actions/set", json={"value": True})
    pins = {p["id"]: p for p in client.get(f"{API}/board").json()["pins"]}
    assert pins["J9-12"]["value"] is True
    assert pins["J9-7"]["claim"]["mode"] == "gpio_out"

    client.post(f"{API}/board/pins/J9-7/manual", json={"mode": "release"})
    pins = {p["id"]: p for p in client.get(f"{API}/board").json()["pins"]}
    assert pins["J9-7"]["claim"] is None


def test_manual_interface_pin_conflict(client):
    r = client.post(f"{API}/board/pins/J9-3/manual", json={"mode": "gpio_out"})
    assert r.status_code == 409 and "I2C1" in r.json()["detail"]


def test_interface_plan(client):
    r = client.post(f"{API}/board/interfaces/plan", json={"enable": ["uart4"]})
    assert r.status_code == 200
    assert r.json()["pins_to_interface"] == {"J10-5": "uart4", "J10-7": "uart4"}
    r = client.post(f"{API}/board/interfaces/plan", json={"enable": ["spi2"]})
    assert r.status_code == 422


def test_ws_snapshot_and_updates(client):
    with client.websocket_connect("/pioneer/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "pins" and first["pins"]["J9-7"]["claim"] is None
        client.post(f"{API}/board/pins/J9-7/manual", json={"mode": "gpio_out", "value": True})
        msg = ws.receive_json()
        assert msg["pins"]["J9-7"] == {"value": True,
                                       "claim": {"mode": "gpio_out", "owners": {"manual": "Ручное управление"}}}


def test_bench_api(client, tmp_path):
    r = client.get(f"{API}/bench").json()
    assert r["enabled"] is True and {k["kind"] for k in r["kinds"]} >= {"level", "wire", "button", "led"}
    assert client.post(f"{API}/bench/models", json={"kind": "button", "id": "b",
                                                     "config": {"pin": "J10-2"}}).status_code == 200
    assert client.post(f"{API}/bench/models", json={"kind": "led", "config": {"pin": "J9-1"}}).status_code == 422
    r = client.post(f"{API}/bench/models/b/actions/press", json={})
    assert r.status_code == 200 and r.json()["pressed"] is True
    assert client.post(f"{API}/bench/models/b/actions/fly", json={}).status_code == 422
    assert client.post(f"{API}/bench/models/zz/actions/press", json={}).status_code == 404
    assert (tmp_path / "bench.json").exists()
    assert client.delete(f"{API}/bench/models/b").status_code == 200
    assert client.get(f"{API}/bench").json()["models"] == []


# ---- авторизация
def test_requires_login(anon):
    assert anon.get(f"{API}/health").status_code == 200
    assert anon.get(f"{API}/board").status_code == 401
    assert anon.get(f"{API}/auth/me").json() == {"authenticated": False, "password_set": True}
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as e:
        with anon.websocket_connect("/pioneer/ws") as ws:
            ws.receive_json()
    assert e.value.code == 4401


def test_login_logout(anon):
    assert anon.post(f"{API}/auth/login", json={"password": "wrong-pw"}).status_code == 401
    r = anon.post(f"{API}/auth/login", json={"password": PASSWORD})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/pioneer" in cookie
    assert anon.get(f"{API}/board").status_code == 200
    anon.post(f"{API}/auth/logout")
    assert anon.get(f"{API}/board").status_code == 401


def test_csrf_header_required(client):
    r = client.post(f"{API}/board/pins/J9-7/manual", json={"mode": "gpio_out"}, headers={"X-Pioneer": ""})
    assert r.status_code == 403
    assert client.get(f"{API}/board", headers={"X-Pioneer": ""}).status_code == 200  # чтение — без заголовка


def test_ws_rejects_foreign_origin(client):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/pioneer/ws", headers={"origin": "http://evil.example"}) as ws:
            ws.receive_json()


def test_login_throttled(anon, monkeypatch):
    import pioneer.api.auth as api_auth
    monkeypatch.setattr(api_auth, "FAIL_DELAY_S", 0)
    for _ in range(5):
        anon.post(f"{API}/auth/login", json={"password": "wrong-pw"})
    assert anon.post(f"{API}/auth/login", json={"password": PASSWORD}).status_code == 429


def test_no_password_set(tmp_path):
    cfg = Config(hal="bench", uenv_file=Path("/nonexistent"), data_dir=tmp_path)
    with TestClient(create_app(cfg), headers={"X-Pioneer": "1"}) as c:
        assert c.post(f"{API}/auth/login", json={"password": "x"}).status_code == 503


def test_sessions_survive_restart_hashed(tmp_path):
    a = Auth(tmp_path)
    a.set_password(PASSWORD)
    token = a.create_session()
    assert token not in (tmp_path / "sessions.json").read_text()
    assert Auth(tmp_path).valid(token)
    import os, stat
    assert stat.S_IMODE(os.stat(tmp_path / "auth.json").st_mode) == 0o600
    a.set_password("another-pw")
    assert not Auth(tmp_path).valid(token)
