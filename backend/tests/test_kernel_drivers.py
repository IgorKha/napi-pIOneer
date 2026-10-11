import json
import struct
from pathlib import Path
from types import SimpleNamespace as NS
import time

import pytest

from pioneer.api.app import API
from pioneer.codetree import CodeTree
from pioneer.kernel_drivers import DriverError, OVERLAY
from test_api import anon, client  # noqa: F401


@pytest.fixture
def driver(client, tmp_path, monkeypatch):
    d = client.app.state.drivers
    monkeypatch.setattr(d.hw.hal, "name", "linux")
    d.cfg.uenv_file = tmp_path / "uEnv.txt"
    d.cfg.uenv_file.write_text("fdtfile=rk3308-napi-c.dtb\noverlays=rk3308-uart1\nuser_overlays=existing\nextraargs=console-test\n")
    d.cfg.user_overlays_dir = tmp_path / "boot-user"
    d.cfg.modules_dir = tmp_path / "modules"
    d.cfg.kernel_sys_dir = tmp_path / "sys"
    d.cfg.iio_dir = tmp_path / "iio"
    reference = d.cfg.modules_dir / d.release / "reference.ko"
    reference.parent.mkdir(parents=True)
    reference.write_bytes(b"reference")
    def command(args):
        if args == ["modinfo", "dht11"]:
            raise DriverError("not installed")
        if args[:3] == ["modinfo", "-F", "name"]:
            return "dht11"
        if args[:3] == ["modinfo", "-F", "vermagic"]:
            return d.release + " SMP preempt mod_unload aarch64"
        if args[0] == "insmod":
            (d.cfg.kernel_sys_dir / "module" / "dht11").mkdir(parents=True)
            return ""
        raise AssertionError(args)
    monkeypatch.setattr(d, "command", command)
    monkeypatch.setattr(d, "compile", lambda source, text: b"dtbo-verified")
    return d


def elf():
    data = bytearray(64)
    data[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<HH", data, 16, 1, 183)
    return bytes(data)


def test_module_architecture_abi_and_idempotent_load(driver, monkeypatch):
    with pytest.raises(DriverError):
        driver.install(b"not ELF")
    assert not driver.module.exists()
    original = driver.command
    monkeypatch.setattr(driver, "command", lambda args: "wrong-kernel" if args[:3] == ["modinfo", "-F", "vermagic"] and args[-1].endswith("dht11.ko") else original(args))
    with pytest.raises(DriverError, match="другого ядра"):
        driver.install(elf())
    assert not driver.module.exists()
    monkeypatch.setattr(driver, "command", original)
    assert driver.install(elf())["loaded"] is False
    assert driver.module.stat().st_mode & 0o777 == 0o600
    assert driver.load() == {"loaded": True, "changed": True}
    assert driver.load()["changed"] is False
    with pytest.raises(DriverError, match="уже загружен"):
        driver.install(elf())


def test_overlay_pin_stale_busy_backup_and_disable(driver):
    before = driver.cfg.uenv_file.read_text()
    p = driver.preview("J10-16", True)
    assert "нужен совместимый модуль" in p["blockers"][0]
    assert "gpios = <&gpio3 10 0>" in p["source"]
    assert "rockchip,pins = <3 10 0 &pcfg_pull_up>" in p["source"]
    assert driver.cfg.uenv_file.read_text() == before
    with pytest.raises(DriverError):
        driver.preview("J9-1", True)
    driver.install(elf())
    p = driver.preview("J10-16", True)
    driver.hw.res.claims["J10-16"] = NS(owners={"manual": "Manual"})
    with pytest.raises(DriverError, match="используется|занят"):
        driver.apply("J10-16", True, p["hash"])
    driver.hw.res.claims.clear()
    driver.cfg.uenv_file.write_text(before + "other=value\n")
    with pytest.raises(DriverError, match="изменились"):
        driver.apply("J10-16", True, p["hash"])
    p = driver.preview("J10-16", True)
    result = driver.apply("J10-16", True, p["hash"])
    backup = Path(result["backup"])
    assert result["needs_reboot"] and (backup / "uEnv.txt").stat().st_mode & 0o777 == 0o600
    text = driver.cfg.uenv_file.read_text()
    assert "user_overlays=existing pioneer-dht22" in text and "extraargs=console-test" in text and "other=value" in text
    assert (driver.cfg.user_overlays_dir / (OVERLAY + ".dtbo")).read_bytes() == b"dtbo-verified"
    off = driver.preview("J10-16", False)
    driver.apply("J10-16", False, off["hash"])
    assert "user_overlays=existing\n" in driver.cfg.uenv_file.read_text()
    assert driver.config() == {"pin": "J10-16", "enabled": False}


def test_overlay_commit_rolls_back_failed_boot_write(driver, monkeypatch):
    driver.install(elf())
    before = driver.cfg.uenv_file.read_bytes()
    p = driver.preview("J10-16", True)
    write = CodeTree._write
    failed = False
    def fail_once(path, data):
        nonlocal failed
        if path == driver.cfg.uenv_file and not failed:
            failed = True
            raise OSError("test write failure")
        return write(path, data)
    monkeypatch.setattr(CodeTree, "_write", staticmethod(fail_once))
    with pytest.raises(OSError):
        driver.apply("J10-16", True, p["hash"])
    assert driver.cfg.uenv_file.read_bytes() == before
    assert not driver.file.exists()
    assert not (driver.cfg.user_overlays_dir / (OVERLAY + ".dtbo")).exists()


def test_iio_conversions_shared_cache_and_errors(driver):
    p = driver.cfg.iio_dir / "iio:device1"
    p.mkdir(parents=True)
    owned = p.parent / OVERLAY
    owned.mkdir()
    (p / "of_node").symlink_to(owned)  # actual Linux IIO layout: no device/ link
    (p / "name").write_text(OVERLAY)  # upstream uses pdev->name, not necessarily dht11
    (p / "in_temp_input").write_text("-4500")
    (p / "in_humidityrelative_input").write_text("62300")
    r = driver.read()
    assert r["ok"] and r["temperature"] == -4.5 and r["humidity"] == 62.3
    (p / "in_temp_input").write_text("invalid")
    assert driver.read() == r
    driver.sample_at -= 3
    failed = driver.read()
    assert not failed["ok"] and "temperature" not in failed
    assert driver.read() == failed
    (p / "name").write_text("saradc")
    driver.sample_at -= 3
    with pytest.raises(DriverError, match="не зарегистрирован"):
        driver.read()


def test_driver_routes_confirmation_and_bench_refusal(client):
    r = client.get(API + "/drivers")
    assert r.status_code == 200 and not r.json()["drivers"][0]["real"]
    for path in ("/drivers/dht22/load", "/drivers/dht22/module"):
        assert client.post(API + path).status_code == 422
        assert client.post(API + path + "?confirmed=true", content=b"ELF").status_code == 422
    assert client.post(API + "/drivers/dht22/read").status_code == 422
    assert client.post(API + "/drivers/dht22/apply", json={"pin": "J10-16", "hash": "x", "confirmed": True}).status_code == 422
    client.post(API + "/auth/logout")
    assert client.get(API + "/drivers").status_code == 401


def test_script_sdk_reads_iio_only(client, monkeypatch):
    d = client.app.state.drivers
    monkeypatch.setattr(d, "read", lambda: {"ok": True, "temperature": 24.5, "humidity": 51.0})
    s = client.post(API + "/scripts", json={"name": "DHT SDK", "code": "from pioneer.sdk import DHT22\nprint(DHT22().read())\n"}).json()
    client.post(API + f"/scripts/{s['id']}/start")
    for _ in range(100):
        state = client.get(API + f"/scripts/{s['id']}").json()
        if state["status"]["state"] == "exited":
            break
        time.sleep(.02)
    assert state["status"]["state"] == "exited"
    assert any("24.5" in line["text"] for line in state["log"])
    assert not client.app.state.hw.res.claims


def test_dht_node_reads_async_without_gpio_claims(client, monkeypatch):
    from conftest import wait_for
    from test_engine import graph, out
    d = client.app.state.drivers
    monkeypatch.setattr(d, "read", lambda: {"ok": True, "temperature": 22.5, "humidity": 42.0})
    e = client.app.state.projects.engine
    e.start_project("dht", "DHT test", graph(("sensor", "builtin.dht22")))
    wait_for(lambda: out(e, "dht", "sensor.valid") is True)
    assert out(e, "dht", "sensor.temperature") == 22.5
    assert out(e, "dht", "sensor.humidity") == 42
    assert not client.app.state.hw.res.claims
    e.stop_project("dht")
