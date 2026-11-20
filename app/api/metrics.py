import contextlib

from fastapi import APIRouter, Request, Response

from app.core import metrics
from app.core.killswitch import KillSwitchUnavailable

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
async def metrics_endpoint(request: Request) -> Response:
    gateway_router = getattr(request.app.state, "router", None)
    if gateway_router is not None:
        # Breaker state changes on the clock, so read it at scrape time.
        metrics.update_breaker_states(gateway_router.breaker_states())

    switch = getattr(request.app.state, "kill_switch", None)
    if switch is not None:
        # Unknown state: keep the last value rather than guess.
        with contextlib.suppress(KillSwitchUnavailable):
            metrics.KILL_SWITCH_ON.set(1 if await switch.is_on() else 0)

    scoreboard = getattr(request.app.state, "eval_scoreboard", None)
    if scoreboard is not None:
        scores = await scoreboard.latest()
        if scores is not None:  # never read yet: show nothing rather than zeros
            metrics.update_eval_scores(scores)

    return Response(metrics.render(), media_type=metrics.CONTENT_TYPE)
