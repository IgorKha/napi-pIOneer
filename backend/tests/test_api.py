from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pioneer.api.app import API, create_app
from pioneer.config import Config


@pytest.fixture
def client(tmp_path):
    cfg = Config(hal="mock", uenv_file=Path("/nonexistent"), data_dir=tmp_path)
    with TestClient(create_app(cfg)) as c:
        yield c


def test_board(client):
    r = client.get(f"{API}/board").json()
    assert r["hal"] == "mock"
    pins = {p["id"]: p for p in r["pins"]}
    assert pins["J9-7"]["runtime_modes"] == ["gpio_in", "gpio_out", "soft_pwm"]
    assert pins["J9-11"]["function"] == "i2c3_m0"
    assert {o["id"] for o in pins["J9-11"]["interface_options"]} == {"i2c3_m0", "pwm2"}
    ifaces = {i["id"]: i for i in r["interfaces"]}
    assert ifaces["uart4"]["enabled"] is False and ifaces["i2c1"]["enabled"] is True


def test_manual_out_and_mock_in(client):
    r = client.post(f"{API}/board/pins/J9-7/manual", json={"mode": "gpio_out", "value": True})
    assert r.status_code == 200 and r.json()["value"] is True

    assert client.post(f"{API}/board/pins/J9-12/manual", json={"mode": "gpio_in"}).status_code == 200
    client.post(f"{API}/mock/pins/J9-12", json={"value": True})
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
