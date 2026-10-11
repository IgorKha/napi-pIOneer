import asyncio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from pioneer.api.code import tree
from pioneer.adafruit_templates import PROFILES, snippet, block_template, package

router = APIRouter(prefix="/adafruit")


@router.get("")
async def catalog(request: Request, refresh: bool = False):
    result = await asyncio.to_thread(request.app.state.adafruit.catalog, refresh)
    result["profiles"] = PROFILES
    return result


@router.get("/job")
def job(request: Request):
    return request.app.state.adafruit.job


@router.get("/uses/{package}")
def uses(package: str, request: Request):
    return request.app.state.adafruit.uses(package)


@router.post("/example/{package}")
def example(package: str, request: Request):
    try:
        manager = request.app.state.adafruit
        source = manager.example(package)
        script = request.app.state.scripts.create(source["name"], source["code"])
        manager.bind(request.app.state.scripts.tree(script.id).root, package)
        return {"id": script.id, "source": source["source"]}
    except (ValueError, OSError, StopIteration) as e:
        raise HTTPException(422, str(e)) from e


class Change(BaseModel):
    package: str = Field(max_length=150)
    version: str | None = Field(default=None, max_length=64)
    remove: bool = False
    confirmed: bool = False


@router.post("/change")
async def change(body: Change, request: Request):
    try:
        return await asyncio.to_thread(request.app.state.adafruit.start, **body.model_dump())
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


class Connect(BaseModel):
    chip: str
    interface: str = "i2c1"
    address: int = 0x76
    columns: int = 20
    rows: int = 4
    kind: str | None = None
    id: str | None = None
    block: bool = False
    bind: bool = False


class Bundle(Connect):
    module_id: str = Field(max_length=40)
    version: int = Field(default=1, ge=1, le=9999)
    code: str | None = Field(default=None, max_length=200000)


@router.post("/module")
def module(body: Bundle, request: Request):
    from pioneer.adafruit_bundle import export

    try:
        data = export(
            request.app.state.adafruit,
            body.chip,
            body.interface,
            body.address,
            body.module_id,
            body.version,
            body.code,
        )
        return Response(
            data,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{body.module_id}-v{body.version}.zip"'
            },
        )
    except (ValueError, KeyError) as e:
        raise HTTPException(422, str(e)) from e


@router.post("/connect")
async def connect(body: Connect, request: Request):
    try:
        hw = request.app.state.hw
        hw.bus(body.interface, "i2c")
        from pioneer.resources import i2c_id

        iface = hw.board.interfaces[body.interface]
        claim = hw.res.claims.get(i2c_id(iface.controller, body.address))
        own = f"script:{body.id}" if body.kind == "scripts" else None
        if claim and not any(o == own or (own and o.startswith(own + ":")) for o in claim.owners):
            raise ValueError("Адрес занят: " + ", ".join(claim.owners.values()))
        manager = request.app.state.adafruit
        pkg = package(body.chip)
        packages = [pkg]
        if body.chip == "lcd":
            packages.append("adafruit-circuitpython-charlcd")
        if body.chip == "ssd1306":
            packages.append("adafruit-circuitpython-framebuf")
        if any(p not in manager.state()["versions"] for p in packages):
            raise ValueError("Сначала установите драйвер")
        if body.block:
            code = block_template(body.chip, body.interface, body.address)
            import secrets

            code = code.replace(f'id="{body.chip}"', f'id="{body.chip}_{secrets.token_hex(3)}"')
            result = await asyncio.to_thread(
                request.app.state.blocks.create, PROFILES[body.chip]["title"], code
            )
            manager.bind(request.app.state.blocks.dir / result["id"], packages)
            return {"block": result}
        code = snippet(body.chip, body.interface, body.address, body.columns, body.rows)
        if body.bind and body.kind and body.id:
            manager.bind(tree(request, body.kind, body.id).root, packages)
        return {"code": code, "package": pkg}
    except (ValueError, KeyError) as e:
        raise HTTPException(422, str(e)) from e
