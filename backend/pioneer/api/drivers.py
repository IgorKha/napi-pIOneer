"""Authenticated, explicit operations for the fixed kernel-driver catalog."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from pioneer.api.code import limited_body
from pioneer.kernel_drivers import DriverError, MODULE_LIMIT
from pioneer.kernel_packages import UPLOAD_LIMIT

router = APIRouter(prefix="/drivers")


def guard(fn, *args):
    try:
        return fn(*args)
    except DriverError as e:
        raise HTTPException(e.code, str(e)) from None
    except (OSError, ValueError):
        raise HTTPException(
            422, "драйвер недоступен: проверьте файлы, права и журнал сервиса"
        ) from None


@router.get("")
def catalog(request: Request):
    return {"drivers": [guard(request.app.state.drivers.status)]}


@router.post("/dht22/module")
async def install(request: Request, confirmed: bool = False):
    if not confirmed:
        raise HTTPException(422, "подтвердите установку доверенного модуля ядра")
    data = await limited_body(request, MODULE_LIMIT)
    return guard(request.app.state.drivers.install, data)


@router.post("/dht22/load")
def load(request: Request, confirmed: bool = False):
    if not confirmed:
        raise HTTPException(422, "подтвердите загрузку драйвера в ядро")
    return guard(request.app.state.drivers.load)


class Connection(BaseModel):
    pin: str = ""
    enabled: bool = True


class Apply(Connection):
    hash: str
    confirmed: bool = False


@router.post("/dht22/plan")
def plan(body: Connection, request: Request):
    return guard(request.app.state.drivers.preview, body.pin, body.enabled)


@router.post("/dht22/apply")
def apply(body: Apply, request: Request):
    if not body.confirmed:
        raise HTTPException(422, "подтвердите изменения uEnv после просмотра")
    return guard(request.app.state.drivers.apply, body.pin, body.enabled, body.hash)


@router.post("/dht22/read")
def read(request: Request):
    return guard(request.app.state.drivers.read)


@router.get("/packages")
def packages(request: Request):
    return guard(request.app.state.kernel_packages.catalog)


@router.post("/packages")
async def upload_package(request: Request, confirmed: bool = False):
    if not confirmed:
        raise HTTPException(
            422, "подтвердите установку доверенного пакета: код будет исполняться в ядре"
        )
    data = await limited_body(request, UPLOAD_LIMIT)
    return guard(request.app.state.kernel_packages.install, data)


@router.post("/packages/{ident}/load")
def load_package(ident: str, request: Request, confirmed: bool = False):
    if not confirmed:
        raise HTTPException(422, "подтвердите загрузку пакета в ядро")
    return guard(request.app.state.kernel_packages.load, ident)


@router.post("/packages/{ident}/unload")
def unload_package(ident: str, request: Request, confirmed: bool = False):
    if not confirmed:
        raise HTTPException(422, "подтвердите остановку потребителей и выгрузку пакета")
    return guard(request.app.state.kernel_packages.unload, ident)


class PackageAutoload(BaseModel):
    enabled: bool
    confirmed: bool = False


@router.post("/packages/{ident}/autoload")
def autoload_package(ident: str, body: PackageAutoload, request: Request):
    if not body.confirmed:
        raise HTTPException(422, "подтвердите изменение автозагрузки драйвера")
    return guard(request.app.state.kernel_packages.set_autoload, ident, body.enabled)


class PackageConnection(BaseModel):
    enabled: bool = True


@router.post("/packages/{ident}/plan")
def plan_package(ident: str, body: PackageConnection, request: Request):
    return guard(request.app.state.kernel_packages.plan, ident, body.enabled)


class PackageApply(PackageConnection):
    hash: str
    confirmed: bool = False


@router.post("/packages/{ident}/apply")
def apply_package(ident: str, body: PackageApply, request: Request):
    if not body.confirmed:
        raise HTTPException(422, "подтвердите изменение uEnv после просмотра")
    return guard(request.app.state.kernel_packages.apply, ident, body.enabled, body.hash)


class Bias(BaseModel):
    pin: str
    bias: str


class BiasApply(Bias):
    hash: str
    confirmed: bool = False


@router.get("/gpio-bias")
def bias_config(request: Request):
    return {"values": request.app.state.pulls.config()}


@router.post("/gpio-bias/plan")
def bias_plan(body: Bias, request: Request):
    return guard(request.app.state.pulls.preview, body.pin, body.bias)


@router.post("/gpio-bias/apply")
def bias_apply(body: BiasApply, request: Request):
    return guard(request.app.state.pulls.apply, body.pin, body.bias, body.hash, body.confirmed)
