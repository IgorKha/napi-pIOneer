import asyncio
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from pioneer.api.code import access, tree
router = APIRouter(prefix='/code', dependencies=[Depends(access)])

class Query(BaseModel):
    path: str
    code: str = Field(max_length=200000)
    line: int = Field(ge=1, le=20000)
    column: int = Field(ge=0, le=200000)
    action: str = Field(pattern='^(complete|goto|hover|signatures|diagnostics)$')

@router.post('/{kind}/{ident}/language')
async def analyze(kind:str, ident:str, body:Query, request:Request):
    t=tree(request,kind,ident)
    try:t.path(body.path)
    except ValueError:raise HTTPException(422,'неверный путь') from None
    lines=body.code.splitlines() or ['']
    if body.line>len(lines) or body.column>len(lines[body.line-1]):
        raise HTTPException(422,'позиция за пределами файла')
    lock=request.app.state.language_lock
    if lock.locked():raise HTTPException(429,'анализатор занят')
    async with lock:
        try:
            return await request.app.state.language.analyze({**body.model_dump(),'root':str(t.root)})
        except TimeoutError:raise HTTPException(503,'анализ Python превысил 12 с') from None
        except (ValueError,OSError):raise HTTPException(503,'анализ Python недоступен; проверьте Jedi') from None
