"""The kill switch as a request dependency: chat requests are refused while it is on."""

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.core import metrics
from app.core.killswitch import KillSwitch, KillSwitchUnavailable


def get_kill_switch(request: Request) -> KillSwitch | None:
    """None without a database: there is then nowhere to keep the flag."""
    switch: KillSwitch | None = request.app.state.kill_switch
    return switch


async def require_gateway_enabled(
    switch: Annotated[KillSwitch | None, Depends(get_kill_switch)],
) -> None:
    if switch is None:
        return
    try:
        paused = await switch.is_on()
    except KillSwitchUnavailable:
        # Cannot tell whether we are paused, so assume we are.
        metrics.KILL_SWITCH_BLOCKS.labels("unavailable").inc()
        raise HTTPException(
            status_code=503, detail="Service temporarily unavailable"
        ) from None
    if paused:
        metrics.KILL_SWITCH_BLOCKS.labels("disabled").inc()
        raise HTTPException(
            status_code=503,
            detail="Service temporarily disabled",
            headers={"Retry-After": "60"},
        )
