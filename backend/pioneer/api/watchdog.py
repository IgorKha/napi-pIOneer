from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
router = APIRouter(prefix='/watchdog')

@router.get('')
def status(request: Request):return request.app.state.watchdog.status()

class Arm(BaseModel):
    timeout: int = 60
    confirmed: bool = False

@router.post('/arm')
async def arm(body: Arm, request: Request):
    try:return await request.app.state.watchdog.arm(body.timeout, body.confirmed)
    except (OSError, ValueError) as e:raise HTTPException(422, str(e)) from None

@router.post('/disarm')
async def disarm(request: Request):
    try:return await request.app.state.watchdog.disarm()
    except OSError:raise HTTPException(503, 'не удалось отключить watchdog') from None
