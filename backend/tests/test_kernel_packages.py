import io
import json
import shutil
import struct
from pathlib import Path
import zipfile

import pytest

from pioneer.api.app import API
from pioneer.codetree import CodeTree
from pioneer.kernel_drivers import DriverError
from test_kernel_drivers import driver, elf  # noqa: F401
from test_api import anon, client  # noqa: F401


@pytest.fixture
def packages(client, driver, monkeypatch):
    p = client.app.state.kernel_packages
    original = driver.command
    def command(args):
        if args[:2] == ["modinfo", "-F"]:
            key = args[2]
            if key == "name":
                return Path(args[3]).stem
            if key == "description":
                return "Example sensor"
            if key == "license":
                return "GPL v2"
            if key == "depends":
                return ""
        if args[0] == "insmod":
            (driver.cfg.kernel_sys_dir / "module" / Path(args[1]).stem).mkdir(parents=True)
            return ""
        if args[0] == "rmmod":
            shutil.rmtree(driver.cfg.kernel_sys_dir / "module" / args[1])
            return ""
        if args[0] == "fdtoverlay":
            return ""
        if args[0] == "dtc":
            return "/dts-v1/; / { example-sensor {}; };"
        return original(args)
    monkeypatch.setattr(driver, "command", command)
    return p


def package(manifest=None, files=None):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest or {"id": "example", "modules": ["sensor.ko"]}))
        for name, data in (files or {"sensor.ko": elf()}).items():
            z.writestr(name, data)
    return b.getvalue()


def dtbo():
    b = bytearray(40)
    struct.pack_into(">II", b, 0, 0xd00dfeed, 40)
    return bytes(b)


def test_universal_module_lifecycle_and_autoload(packages):
    m = packages.install(elf())
    assert m["id"] == "driver" and m["modules"][0]["name"] == "driver"
    assert (packages.path("driver") / "driver.ko").stat().st_mode & 0o777 == 0o600
    assert packages.load("driver")["changed"]
    assert not packages.load("driver")["changed"]
    packages.set_autoload("driver", True)
    assert packages.catalog()["packages"][0]["autoload"]
    with pytest.raises(DriverError, match="автозагрузку"):
        packages.unload("driver")
    with pytest.raises(DriverError, match="загружен"):
        packages.install(elf())
    packages.set_autoload("driver", False)
    packages.unload("driver")
    packages.set_autoload("driver", True)
    packages.autoload()
    assert packages.loaded("driver")


def test_package_validation_rejects_paths_abi_and_bad_manifest(packages, monkeypatch):
    for data in (b"not ELF", package(files={"../escape.ko": elf()}),
                 package({"id": "../bad", "modules": ["sensor.ko"]}),
                 package({"modules": ["sensor.ko"], "pins": [[]]}),
                 package(files={"sensor.ko": elf(), "unexpected.txt": b"x"})):
        with pytest.raises((DriverError, OSError)):
            packages.install(data)
    assert not packages.catalog()["packages"]
    original = packages.drivers.command
    monkeypatch.setattr(packages.drivers, "command", lambda args: "wrong-kernel" if args[:3] == ["modinfo", "-F", "vermagic"] and Path(args[-1]).name == "driver.ko" else original(args))
    with pytest.raises(DriverError, match="ядру/ABI"):
        packages.install(elf())


def test_package_collision_changed_file_and_load_rollback(packages, monkeypatch):
    packages.install(package())
    with pytest.raises(DriverError, match="другому пакету"):
        packages.install(package({"id": "another", "modules": ["sensor.ko"]}))
    (packages.path("example") / "sensor.ko").write_bytes(elf() + b"modified")
    with pytest.raises(DriverError, match="изменился"):
        packages.load("example")
    assert not packages.loaded("sensor")
    packages.install(package({"id": "bundle", "modules": ["one.ko", "two.ko"]}, {"one.ko": elf(), "two.ko": elf()}))
    original = packages.drivers.command
    def fail(args):
        if args[0] == "insmod" and args[1].endswith("two.ko"):
            raise DriverError("test load error")
        return original(args)
    monkeypatch.setattr(packages.drivers, "command", fail)
    with pytest.raises(DriverError, match="test load error"):
        packages.load("bundle")
    assert not packages.loaded("one")


def test_overlay_plan_backup_stale_and_write_rollback(packages, monkeypatch):
    d = packages.drivers
    manifest = {"id": "example", "modules": ["sensor.ko"], "overlay": "sensor.dtbo", "pins": ["J10-16"], "device_nodes": ["/example-sensor"]}
    packages.install(package(manifest, {"sensor.ko": elf(), "sensor.dtbo": dtbo()}))
    before = d.cfg.uenv_file.read_bytes()
    plan = packages.plan("example", True)
    assert plan["ready"] and plan["compiled"] and "pioneer-driver-example" in plan["diff"]
    assert d.cfg.uenv_file.read_bytes() == before
    d.cfg.uenv_file.write_bytes(before + b"other=value\n")
    with pytest.raises(DriverError, match="устарел"):
        packages.apply("example", True, plan["hash"])
    plan = packages.plan("example", True)
    result = packages.apply("example", True, plan["hash"])
    assert Path(result["backup"]).joinpath("uEnv.txt").stat().st_mode & 0o777 == 0o600
    assert "existing pioneer-driver-example" in d.cfg.uenv_file.read_text()
    off = packages.plan("example", False)
    packages.apply("example", False, off["hash"])
    assert "user_overlays=existing\n" in d.cfg.uenv_file.read_text()
    before = d.cfg.uenv_file.read_bytes()
    plan = packages.plan("example", True)
    write = CodeTree._write
    failed = False
    def fail_once(path, data):
        nonlocal failed
        if path == d.cfg.uenv_file and not failed:
            failed = True
            raise OSError("test failure")
        write(path, data)
    monkeypatch.setattr(CodeTree, "_write", staticmethod(fail_once))
    with pytest.raises(OSError):
        packages.apply("example", True, plan["hash"])
    assert d.cfg.uenv_file.read_bytes() == before


def test_package_routes_confirmation_and_auth(client, packages):
    assert client.post(API + "/drivers/packages", content=elf()).status_code == 422
    r = client.post(API + "/drivers/packages?confirmed=true", content=elf())
    assert r.status_code == 200 and r.json()["id"] == "driver"
    assert client.get(API + "/drivers/packages").json()["packages"][0]["id"] == "driver"
    for action in ("load", "unload"):
        assert client.post(API + "/drivers/packages/driver/" + action).status_code == 422
    assert client.post(API + "/drivers/packages/driver/load?confirmed=true").status_code == 200
    assert client.post(API + "/drivers/packages/driver/autoload", json={"enabled": True}).status_code == 422
    assert client.post(API + "/drivers/packages/driver/autoload", json={"enabled": True, "confirmed": True}).status_code == 200
    client.post(API + "/auth/logout")
    assert client.get(API + "/drivers/packages").status_code == 401


def test_invalid_autoload_does_not_stop_core(packages):
    packages.settings.write_text('{"autoload": [{"not": "an id"}]}')
    packages.autoload()  # logs the failure, does not raise or start modules
    with pytest.raises(DriverError, match="конфигурация"):
        packages.catalog()
