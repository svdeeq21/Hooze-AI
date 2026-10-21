import hmac
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request

from hooze.api.deps import get_container
from hooze.runtime.scheduler import run_tick

router = APIRouter()


@router.post("/internal/tick")
async def tick(request: Request, x_cron_secret: Optional[str] = Header(None)):
    c = get_container(request)
    expected = c.settings.internal_cron_secret
    if not expected or not x_cron_secret or not hmac.compare_digest(x_cron_secret, expected):     # fail closed if unset
        raise HTTPException(401, "unauthorized")
    out = await run_tick(pdb=c.pdb, configs=c.configs, adapters=c.adapters, events=c.events, llm=c.llm, clock=c.clock, extras_for=c.extras_for)
    if c.maintenance:
        out["maintenance"] = await c.maintenance.run_once()           # an external cron can drive recovery too (belt and braces)
    return out
